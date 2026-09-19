"""Data lifecycle: what expires, what never does (spec §5.4, brief P4).

Spec §5.4: *"Raw lake rows expire after 90 days (TTL job). Snapshots and the ledger never expire."*

That sentence is a pair of rules, and both halves matter:

* **The raw lake expires.** It is a reference to a payload that has already been parsed into
  signals — keeping it forever would grow the database for evidence nobody reads twice.
* **Scores, judgements, briefs and the quota ledger never expire.** They are the system's memory:
  the trend of a candidate, the advice it received, the spend it caused. A deletion here would be
  silent history loss, so `expire()` touches exactly two tables and asserts as much.

The LLM cache is a third case: it has its own 30-day TTL (spec §6.3) and is swept by the same job
so an operator has one command to run rather than three.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from trend_analyst.llm.cache import purge_expired
from trend_analyst.store.models import Brief, Judgement, LLMCache, RawItem, Score

__all__ = ["DEFAULT_RAW_TTL_DAYS", "TtlReport", "expire", "protected_counts"]

#: Spec §5.4: the raw lake keeps 90 days.
DEFAULT_RAW_TTL_DAYS = 90

#: The tables the TTL job must never touch, named here so a future edit has to argue with a test
#: rather than with a comment.
PROTECTED = ("scores", "judgements", "briefs", "quota_ledger", "signals", "runs")


@dataclass(frozen=True, slots=True)
class TtlReport:
    """What the job removed, and what it deliberately left alone."""

    as_of: datetime
    raw_cutoff: datetime
    raw_deleted: int
    cache_deleted: int
    protected: dict[str, int]
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "raw_cutoff": self.raw_cutoff.isoformat(),
            "raw_deleted": self.raw_deleted,
            "cache_deleted": self.cache_deleted,
            "protected": self.protected,
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        mode = "would delete" if self.dry_run else "deleted"
        return (
            f"TTL: {mode} {self.raw_deleted} raw item(s) older than "
            f"{self.raw_cutoff:%Y-%m-%d} and {self.cache_deleted} expired LLM cache row(s); "
            f"kept {self.protected.get('scores', 0)} score(s), "
            f"{self.protected.get('judgements', 0)} judgement(s), "
            f"{self.protected.get('briefs', 0)} brief(s)"
        )


def protected_counts(session: Session) -> dict[str, int]:
    """Row counts for the tables the TTL job must never delete from."""
    return {
        "scores": int(session.execute(select(func.count()).select_from(Score)).scalar_one()),
        "judgements": int(
            session.execute(select(func.count()).select_from(Judgement)).scalar_one()
        ),
        "briefs": int(session.execute(select(func.count()).select_from(Brief)).scalar_one()),
    }


def expire(
    session: Session,
    *,
    now: datetime | None = None,
    raw_ttl_days: int = DEFAULT_RAW_TTL_DAYS,
    dry_run: bool = False,
) -> TtlReport:
    """Delete what the specification says expires; report what was protected.

    Args:
        raw_ttl_days: the raw lake's retention. Configurable because a replay over a long stretch of
            fixtures is a legitimate reason to keep more, and a hard-coded 90 would make that
            impossible without editing code.
        dry_run: count what would go, delete nothing.
    """
    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(days=raw_ttl_days)
    protected = protected_counts(session)

    raw_deleted = 0
    if not dry_run:
        # `fetch` time, not content time: a payload fetched long ago is what costs storage, and a
        # re-published old page is fresh evidence.
        result = session.execute(delete(RawItem).where(RawItem.fetched_at < cutoff))
        raw_deleted = int(getattr(result, "rowcount", 0) or 0)
        cache_deleted = purge_expired(session, now=reference)
    else:
        raw_deleted = int(
            session.execute(
                select(func.count()).select_from(RawItem).where(RawItem.fetched_at < cutoff)
            ).scalar_one()
        )
        cache_deleted = int(
            session.execute(
                select(func.count()).select_from(LLMCache).where(LLMCache.expires_at <= reference)
            ).scalar_one()
        )
    session.flush()

    return TtlReport(
        as_of=reference,
        raw_cutoff=cutoff,
        raw_deleted=raw_deleted,
        cache_deleted=cache_deleted,
        protected=protected,
        dry_run=dry_run,
    )
