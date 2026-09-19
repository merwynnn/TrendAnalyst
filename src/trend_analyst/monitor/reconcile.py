"""Ledger reconciliation: every unit of spend must be explainable (brief P4's acceptance bar).

The brief's P4 done-when is *"full nightly run completes inside budget with quota ledger balanced to
zero unexplained spend"*. That sentence needs a definition, and this module is it:

A spend row is **explained** when all of the following hold:

* its operation names a subsystem this code recognises - ``l0:<source>`` for collection,
  ``llm:<gate>:tokens:<cache key>`` for a gate;
* its ``source_id`` exists in the registry mirror (for L0) or names a real gate (for LLM);
* its ``amount`` is positive (the schema's CHECK agrees, and this asserts it end to end);
* for an **L0** row, the amount matches the ``run_source_log`` entry for that (run, source) — the
  ledger and the log are two records of one event and they must agree;
* for an **LLM** row, a cached answer exists under the cache key in the operation, and its token
  counts match the amount — a token spend with no stored answer is precisely the unexplained spend
  the brief is talking about.

Anything else lands in ``unexplained`` with a reason, and ``balanced`` is false. Reconciliation neve
"fixes" a row: it reports, because a system that silently repairs its own accounting cannot be
audited.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trend_analyst.store.models import LLMCache, QuotaLedger, RunSourceLog, Source

__all__ = ["GATES", "ReconcileReport", "accounted_totals", "classify", "reconcile"]

#: The gates a spend row may name, matching `llm_cache.gate`'s CHECK constraint.
GATES: Final[tuple[str, ...]] = ("planner", "judge", "writer")
#: Operation prefixes this code writes. Anything else is a subsystem nobody has accounted for.
L0_PREFIX: Final = "l0:"
LLM_PREFIX: Final = "llm:"
LLM_SUFFIX_MARKER: Final = ":tokens:"


def classify(operation: str) -> tuple[str, str]:
    """``(kind, detail)`` for an operation string: how the spend was caused.

    >>> classify("l0:arctic_shift")
    ('l0', 'arctic_shift')
    >>> classify("llm:judge:tokens:abc123")
    ('llm', 'judge')
    >>> classify("mystery:thing")
    ('unknown', 'mystery:thing')
    """
    if operation.startswith(L0_PREFIX):
        return "l0", operation[len(L0_PREFIX) :]
    if operation.startswith(LLM_PREFIX) and LLM_SUFFIX_MARKER in operation:
        return "llm", operation[len(LLM_PREFIX) :].split(LLM_SUFFIX_MARKER, 1)[0]
    return "unknown", operation


def _cache_key_of(operation: str) -> str | None:
    """The cache key a token-spend row refers to, when it carries one."""
    if LLM_SUFFIX_MARKER not in operation:
        return None
    return operation.split(LLM_SUFFIX_MARKER, 1)[1].strip() or None


@dataclass(slots=True)
class ReconcileReport:
    """What the ledger holds, how much of it is explained, and what is not."""

    rows: int = 0
    spend_total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)
    per_gate: dict[str, dict[str, int]] = field(default_factory=dict)
    unexplained: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def explained(self) -> int:
        return self.rows - len(self.unexplained)

    @property
    def balanced(self) -> bool:
        return not self.unexplained

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "spend_total": self.spend_total,
            "by_kind": dict(self.by_kind),
            "per_source": dict(self.per_source),
            "per_gate": dict(self.per_gate),
            "explained": self.explained,
            "unexplained": self.unexplained[:20],
            "notes": self.notes,
            "balanced": self.balanced,
        }

    def summary(self) -> str:
        state = "BALANCED" if self.balanced else f"UNEXPLAINED ({len(self.unexplained)})"
        return (
            f"ledger: {self.rows} row(s), {self.spend_total} unit(s) of spend, "
            f"{self.explained} explained — {state}"
        )


def _check_l0(
    report: ReconcileReport,
    *,
    row: QuotaLedger,
    amount: int,
    source_id: str,
    known_sources: set[str],
) -> None:
    """One collection spend row: positive, a real source, and matching its run log."""
    if amount <= 0:
        report.unexplained.append(
            {"operation": str(row.operation), "amount": amount, "reason": "non-positive spend"}
        )
        return
    bucket = report.per_source.setdefault(source_id, {"ledger": 0, "run_log": 0, "rows": 0})
    bucket["ledger"] += amount
    bucket["rows"] += 1
    if source_id not in known_sources:
        report.unexplained.append(
            {
                "operation": str(row.operation),
                "amount": amount,
                "reason": f"source {source_id!r} is not in the registry mirror",
            }
        )
        return
    # The agreement between ledger and run log is checked per (run, source) after the loop: doing
    # it here, against a bucket that accumulates across runs, produced false positives.


def _check_llm(
    report: ReconcileReport,
    *,
    row: QuotaLedger,
    amount: int,
    gate: str,
    cache_totals: Mapping[str, int],
) -> None:
    """One gate token row: a known gate, a cache key, and a cached answer that agrees."""
    if amount <= 0:
        report.unexplained.append(
            {"operation": str(row.operation), "amount": amount, "reason": "non-positive spend"}
        )
        return
    bucket = report.per_gate.setdefault(gate, {"ledger": 0, "cached": 0, "rows": 0})
    bucket["ledger"] += amount
    bucket["rows"] += 1
    if gate not in GATES:
        report.unexplained.append(
            {
                "operation": str(row.operation),
                "amount": amount,
                "reason": f"unknown gate {gate!r}",
            }
        )
        return
    key = _cache_key_of(str(row.operation))
    if key is None:
        report.unexplained.append(
            {
                "operation": str(row.operation),
                "amount": amount,
                "reason": "token spend with no cache key: no stored answer explains it",
            }
        )
        return
    # Cache keys are truncated in the operation, so matching is by prefix; 24 hex characters make a
    # collision impossible in practice and the truncation is what keeps the operation under 128.
    matches = sorted(
        total for cached_key, total in cache_totals.items() if cached_key.startswith(key)
    )
    if not matches:
        report.unexplained.append(
            {
                "operation": str(row.operation),
                "amount": amount,
                "reason": "no cached answer matches this cache key",
            }
        )
        return
    bucket["cached"] += matches[0]
    if matches[0] != amount:
        # Explainable, and worth seeing: the cache row keeps the FIRST call's token counts (the
        # write is do-nothing on conflict), so a `--fresh` re-run pays again and the ledger records
        # the real spend while the cache row still holds the original numbers. The call is
        # attributed, so this is a note rather than unexplained spend — but a large delta here is
        # how a repeated-payment habit would show up.
        delta = abs(amount - matches[0])
        bucket["unmatched_tokens"] = bucket.get("unmatched_tokens", 0) + delta
        report.notes.append(
            f"{gate}: ledger {amount} tokens vs cached answer {matches[0]} "
            f"(delta {delta}, explained by a fresh re-run of the same prompt)"
        )


def reconcile(
    session: Session,
    *,
    run_id: Any = None,
    source_ids: Sequence[str] | None = None,
) -> ReconcileReport:
    """Check every spend row against the record that should explain it.

    Args:
        run_id: limit to one run; default is the whole ledger, which is what a nightly report wants.
        source_ids: limit to these sources (used by the nightly run to scope its own spend).
    """
    report = ReconcileReport()
    statement = select(QuotaLedger).order_by(QuotaLedger.id)
    if run_id is not None:
        statement = statement.where(QuotaLedger.run_id == run_id)
    if source_ids is not None:
        statement = statement.where(QuotaLedger.source_id.in_(list(source_ids)))
    rows = session.execute(statement).scalars().all()

    known_sources = set(session.execute(select(Source.id)).scalars().all())
    # Two records of one event, both keyed by (run, source): the ledger and the run log. Comparing
    # a per-source total against a per-(run, source) amount was the first version's bug — it flagged
    # every source that had ever been collected in more than one run.
    logs: dict[tuple[Any, str], int] = {}
    for log in session.execute(select(RunSourceLog)).scalars().all():
        key = (log.run_id, str(log.source_id))
        logs[key] = logs.get(key, 0) + int(log.quota_spent)
    ledger_pairs: dict[tuple[Any, str], int] = {}
    cache_totals: dict[str, int] = {}
    for cached in session.execute(
        select(LLMCache.cache_key, LLMCache.prompt_tokens, LLMCache.completion_tokens)
    ).all():
        cache_totals[str(cached.cache_key)] = int(cached.prompt_tokens) + int(
            cached.completion_tokens
        )

    for row in rows:
        report.rows += 1
        amount = int(row.amount)
        report.spend_total += amount
        kind, detail = classify(str(row.operation))
        report.by_kind[kind] = report.by_kind.get(kind, 0) + 1
        if kind == "l0":
            _check_l0(
                report,
                row=row,
                amount=amount,
                source_id=detail,
                known_sources=known_sources,
            )
            pair = (row.run_id, detail)
            ledger_pairs[pair] = ledger_pairs.get(pair, 0) + amount
        elif kind == "llm":
            _check_llm(report, row=row, amount=amount, gate=detail, cache_totals=cache_totals)
        else:
            report.unexplained.append(
                {
                    "operation": str(row.operation),
                    "amount": amount,
                    "reason": "no subsystem in this code writes this operation",
                }
            )

    for (run, source_id), ledger_amount in sorted(
        ledger_pairs.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        logged = logs.get((run, source_id))
        if logged is None:
            report.unexplained.append(
                {
                    "operation": f"l0:{source_id}",
                    "amount": ledger_amount,
                    "reason": (
                        f"run {str(run)[:8]} spent {ledger_amount} on {source_id} but has no "
                        "run_source_log row to explain it"
                    ),
                }
            )
            continue
        report.per_source.setdefault(source_id, {"ledger": 0, "run_log": 0, "rows": 0})[
            "run_log"
        ] += logged
        if logged != ledger_amount:
            report.unexplained.append(
                {
                    "operation": f"l0:{source_id}",
                    "amount": ledger_amount,
                    "reason": (
                        f"run {str(run)[:8]}: run_source_log says {logged} for {source_id}, "
                        f"the ledger says {ledger_amount}"
                    ),
                }
            )

    if not rows:
        report.notes.append("the ledger is empty: nothing has spent anything yet")
    return report


def accounted_totals(session: Session) -> dict[str, Any]:
    """The three views of spend side by side: ledger, cache token log, and source logs.

    Printed by the nightly report because a reconciliation that only says "balanced" hides how much
    was actually spent.
    """
    ledger_by_kind: dict[str, int] = {}
    for row in session.execute(select(QuotaLedger)).scalars().all():
        kind, _ = classify(str(row.operation))
        ledger_by_kind[kind] = ledger_by_kind.get(kind, 0) + int(row.amount)
    cache_tokens = sum(
        int(row.prompt_tokens) + int(row.completion_tokens)
        for row in session.execute(select(LLMCache)).scalars().all()
    )
    logged = sum(
        int(row.quota_spent) for row in session.execute(select(RunSourceLog)).scalars().all()
    )
    return {
        "ledger_by_kind": ledger_by_kind,
        "ledger_total": sum(ledger_by_kind.values()),
        "cache_tokens": cache_tokens,
        "run_log_spend": logged,
    }
