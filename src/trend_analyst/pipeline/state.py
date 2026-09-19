"""Run state: the ledger, watermarks, checkpoints and the quota CAS (spec §5).

Three rules from the spec live here, and each has a failure mode that would be expensive:

* **Every run writes a ledger row per (run, layer, source)** (§5.1) — including the ones
  that failed or were skipped, with the reason in words. A source that silently vanishes
  from a run is indistinguishable from a source with no new data, and the second one is
  the whole point of the pipeline.
* **A resume executes only unfinished work** (§5.4) — so "which sources are done" is a
  query against the ledger, not a flag in memory that dies with the process.
* **Side effects live outside the rollback surface** (§5.4) — quota spend is recorded in
  `quota_ledger` with a compare-and-swap key, and it is never written by whatever
  transaction happens to be open when a checkpoint is taken.

The budgets are rehydrated from that ledger on resume. Without it, a crashed run that
resumes would start each source's daily budget from zero and could spend it twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trend_analyst.sources.base import Clock, SourceBudget, SystemClock
from trend_analyst.sources.registry import Registry, SourceEntry
from trend_analyst.store.models import QuotaLedger, Run, RunSourceLog, Source

__all__ = [
    "FINISHED_STATUSES",
    "RunHandle",
    "advance_watermark",
    "completed_sources",
    "finish_run",
    "pending_sources",
    "read_watermark",
    "record_source_result",
    "record_spend",
    "restore_budgets",
    "spent_today",
    "start_run",
]

#: Statuses that mean "this source is done for this run". A failed source is *not* done:
#: spec §5.4 wants a resume to retry it, and a skipped one *is* done — it was a decision,
#: not an accident, and retrying it would defeat the back-off it was skipped for.
FINISHED_STATUSES: tuple[str, ...] = ("ok", "degraded", "skipped")


@dataclass(frozen=True, slots=True)
class RunHandle:
    """The run a call is working on, and whether it was picked up from a previous crash."""

    run_id: str
    resumed: bool
    trigger: str


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def _latest_running(session: Session) -> Run | None:
    statement: Select[tuple[Run]] = (
        select(Run).where(Run.status == "running").order_by(Run.started_at.desc()).limit(1)
    )
    return session.execute(statement).scalar_one_or_none()


def start_run(session: Session, *, trigger: str = "nightly", resume: bool = True) -> RunHandle:
    """Open a run, or adopt the one a crash left behind.

    Adopting matters because the alternative is worse in both directions: a new run would
    re-do work the ledger already paid for, and refusing to start would need a human at
    3 a.m. for something the checkpoint system can handle alone.
    """
    if resume:
        existing = _latest_running(session)
        if existing is not None:
            return RunHandle(run_id=str(existing.id), resumed=True, trigger=existing.trigger)

    run = Run(status="running", trigger=trigger, layer_status={})
    session.add(run)
    session.flush()
    return RunHandle(run_id=str(run.id), resumed=False, trigger=trigger)


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    layer_status: dict[str, object] | None = None,
    notes: str | None = None,
) -> None:
    """Close a run with its outcome. `status` must be one the model allows."""
    values: dict[str, object] = {
        "status": status,
        "finished_at": datetime.now(UTC),
        "notes": notes,
    }
    if layer_status is not None:
        values["layer_status"] = layer_status
    session.execute(update(Run).where(Run.id == run_id).values(**values))
    session.flush()


# ---------------------------------------------------------------------------
# Per-source results
# ---------------------------------------------------------------------------
def record_source_result(
    session: Session,
    *,
    run_id: str,
    source_id: str,
    status: str,
    items_fetched: int = 0,
    items_new: int = 0,
    quota_spent: int = 0,
    rate_limit_hits: int = 0,
    reason: str | None = None,
) -> None:
    """Write (or update) this run's row for one source.

    Upsert rather than insert: a resumed run records the same source a second time when it
    retries a failure, and keeping both rows would make "what happened in this run" depend
    on which row you read. The `quota_ledger` is where spend history accumulates; this
    table is the run's current verdict.
    """
    table = RunSourceLog.metadata.tables["run_source_log"]
    statement = pg_insert(table).values(
        run_id=run_id,
        source_id=source_id,
        status=status,
        items_fetched=items_fetched,
        items_new=items_new,
        quota_spent=quota_spent,
        rate_limit_hits=rate_limit_hits,
        reason=reason,
        finished_at=datetime.now(UTC),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["run_id", "source_id"],
        set_={
            "status": statement.excluded.status,
            "items_fetched": statement.excluded.items_fetched,
            "items_new": statement.excluded.items_new,
            "quota_spent": statement.excluded.quota_spent,
            "rate_limit_hits": statement.excluded.rate_limit_hits,
            "reason": statement.excluded.reason,
            "finished_at": statement.excluded.finished_at,
        },
    )
    session.execute(statement)
    session.flush()


def completed_sources(session: Session, run_id: str) -> set[str]:
    """Sources this run has already finished (see :data:`FINISHED_STATUSES`)."""
    rows = session.execute(
        select(RunSourceLog.source_id).where(
            RunSourceLog.run_id == run_id, RunSourceLog.status.in_(FINISHED_STATUSES)
        )
    ).scalars()
    return set(rows)


def pending_sources(session: Session, run_id: str, registry: Registry) -> tuple[str, ...]:
    """Enabled L0 sources this run has not finished, in registry (execution) order."""
    done = completed_sources(session, run_id)
    return tuple(entry.id for entry in registry.for_layer("L0") if entry.id not in done)


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------
def read_watermark(session: Session, source_id: str) -> str | None:
    """The source's cursor, or None if it has never run."""
    return session.execute(
        select(Source.watermark).where(Source.id == source_id)
    ).scalar_one_or_none()


def advance_watermark(
    session: Session, source_id: str, cursor: str | None, *, when: datetime | None = None
) -> bool:
    """Move a source's watermark forward. Returns whether anything changed.

    Two deliberate refusals: a ``None`` cursor never clears a stored watermark (a source
    that could not decide where it got to must not erase where it had got to before), and
    an identical cursor is not rewritten, so a no-op pass leaves the timestamp alone.
    """
    if cursor is None:
        return False

    current = read_watermark(session, source_id)
    if current == cursor:
        return False

    session.execute(
        update(Source)
        .where(Source.id == source_id, Source.watermark.is_distinct_from(cursor))
        .values(watermark=cursor, watermark_updated_at=when or datetime.now(UTC))
    )
    session.flush()
    return True


# ---------------------------------------------------------------------------
# Quota: the compare-and-swap (spec §5.4)
# ---------------------------------------------------------------------------
def record_spend(
    session: Session,
    *,
    source_id: str,
    run_id: str | None,
    operation: str,
    amount: int = 1,
    reason: str | None = None,
) -> bool:
    """Record quota spend. Returns False when this spend was already recorded.

    The unique key on (run_id, source_id, operation) *is* the compare-and-swap: a resumed
    run that re-issues ``fetch:page:3`` loses the race and gets a conflict, so the same
    request can never be charged twice. The caller treats False as "already counted".
    """
    try:
        with session.begin_nested():
            session.add(
                QuotaLedger(
                    source_id=source_id,
                    run_id=run_id,
                    operation=operation,
                    amount=amount,
                    reason=reason,
                )
            )
            session.flush()
    except IntegrityError:
        return False
    return True


def spent_today(session: Session, source_id: str, *, now: datetime | None = None) -> int:
    """What this source has already spent today, from the ledger (never from memory)."""
    day_start = (now or datetime.now(UTC)).astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    total = session.execute(
        select(func.coalesce(func.sum(QuotaLedger.amount), 0)).where(
            QuotaLedger.source_id == source_id, QuotaLedger.spend_date >= day_start
        )
    ).scalar_one()
    return int(total)


def restore_budgets(
    session: Session,
    registry: Registry,
    *,
    source_ids: Iterable[str] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    clock_for: Callable[[SourceEntry], Clock] | None = None,
) -> dict[str, SourceBudget]:
    """Rebuild each source's leash with today's spend already charged to it.

    This is what makes a resume safe rather than merely convenient: a source that spent
    1,400 of its 2,000 requests before the crash resumes with 600 left.

    The clock comes from the caller: a live run paces against the real clock, and a replay
    uses the pinned one, so the leash and the plugin always agree about what time it is.
    """
    wanted = set(source_ids) if source_ids is not None else {entry.id for entry in registry.sources}
    budgets: dict[str, SourceBudget] = {}
    for entry in registry.sources:
        if entry.id not in wanted:
            continue
        clock = clock_for(entry) if clock_for is not None else SystemClock()
        budget = SourceBudget(
            source_id=entry.id,
            budget_per_day=entry.budget_per_day,
            rps=entry.rps,
            clock=clock,
            sleep=sleep,
        )
        budget.charge(spent_today(session, entry.id, now=now))
        budgets[entry.id] = budget
    return budgets
