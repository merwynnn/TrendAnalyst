"""Drift detection: the two slow failures a nightly pipeline dies of quietly (spec §9).

An outage announces itself. Drift does not: a judge that starts keeping everything, a source whose
watermark stopped moving, an eval baseline that has slid five points over three weeks. Each of those
looks healthy in any single night's output, and each is only visible as a *comparison* — this module
is where the comparisons live.

Spec §9's runbooks ask for two of these by name:

* **judge keep-rate outside band** — a gate that keeps 95% of candidates has stopped discriminating;
* **eval baseline drop** — §8's release rule: no rubric may drop more than 2 points from baseline.

Both are computed from tables that already exist (`judgements`, `eval_cases`, `sources`), so drift
detection needs no new state: what it needs is a baseline and a band, which is exactly what it
computes and records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trend_analyst.store.models import Candidate, EvalCase, Judgement, Run, Source

__all__ = [
    "DEFAULT_KEEP_RATE_BAND",
    "DEFAULT_RUBRIC_DROP_TOLERANCE",
    "DriftFinding",
    "eval_baseline_drop",
    "keep_rate",
    "keep_rate_drift",
    "record_eval_run",
    "watermark_drift",
]

#: The band a healthy judge keep-rate lives in. Below it the gate is rejecting too much (and the
#: pipeline will starve); above it the gate has stopped discriminating, which is worse: a judge that
#: keeps everything makes every downstream cost — L2, briefs, tokens — unjustified.
DEFAULT_KEEP_RATE_BAND: Final[tuple[float, float]] = (0.05, 0.80)

#: How far the keep-rate may move against its baseline before it is worth reporting, while still
#: inside the band. Loose on purpose: a rate that jumps 25 points has a cause worth naming.
_MOVEMENT_TOLERANCE: Final = 0.25

#: Spec §8: "no rubric may drop more than 2 points from baseline".
DEFAULT_RUBRIC_DROP_TOLERANCE: Final = 2.0


@dataclass(slots=True)
class DriftFinding:
    """One drift observation: what moved, from what to what, and what a human should do."""

    kind: str
    severity: str
    runbook: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""
    reversible_with: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "runbook": self.runbook,
            "summary": self.summary,
            "detail": self.detail,
            "suggested_action": self.suggested_action,
            "reversible_with": self.reversible_with,
        }


def keep_rate(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[float, int]:
    """``(kept / (kept + dropped), decided)`` over a window of judge verdicts.

    Only keeper/dropper verdicts count: a verdict that was never applied (unknown phrase, degraded
    batch) is not a decision about a candidate, and including it would make the rate move when the
    *gate* hiccups rather than when its judgement changes.
    """
    statement = select(Judgement.decision, func.count()).where(Judgement.gate == "judge")
    if since is not None:
        statement = statement.where(Judgement.created_at >= since)
    if until is not None:
        statement = statement.where(Judgement.created_at < until)
    statement = statement.group_by(Judgement.decision)

    counts = {str(decision): int(count) for decision, count in session.execute(statement).all()}
    kept = counts.get("keep", 0)
    dropped = counts.get("drop", 0)
    total = kept + dropped
    if total == 0:
        return 0.0, 0
    return kept / total, total


def keep_rate_drift(
    session: Session,
    *,
    now: datetime | None = None,
    window_days: int = 7,
    baseline_days: int = 30,
    band: tuple[float, float] = DEFAULT_KEEP_RATE_BAND,
    min_decisions: int = 5,
) -> DriftFinding | None:
    """Compare the recent keep-rate with the longer baseline, and with a healthy band.

    Two ways to be wrong, reported separately because they mean different things:

    * **outside the band** — the gate's behaviour is unhealthy in absolute terms;
    * **moved against the baseline** — the gate is still inside the band but its judgement has
        shifted
      by more than the tolerance, which is how a prompt or model change shows up before it is bad
      enough to be obvious.
    """
    reference = now or datetime.now(UTC)
    recent, recent_n = keep_rate(
        session, since=reference - timedelta(days=window_days), until=reference
    )
    baseline, baseline_n = keep_rate(
        session, since=reference - timedelta(days=baseline_days), until=reference -
            timedelta(days=window_days)
    )

    if recent_n < min_decisions:
        return None  # too few verdicts to call anything a drift

    low, high = band
    if not (low <= recent <= high):
        side = "keeping almost everything" if recent > high else "rejecting almost everything"
        return DriftFinding(
            kind="keep_rate_out_of_band",
            severity="warn" if recent <= high else "critical",
            runbook="keep-rate-drift",
            summary=(
                f"judge keep-rate {recent:.0%} is outside the healthy band "
                f"{low:.0%}-{high:.0%} over {recent_n} verdict(s): {side}"
            ),
            detail={
                "recent": round(recent, 4),
                "recent_decisions": recent_n,
                "band": [low, high],
                "baseline": round(baseline, 4) if baseline_n else None,
                "baseline_decisions": baseline_n,
            },
            suggested_action=(
                "review the last judgements' reasons and the prompt; a gate that keeps everything "
                "makes every downstream cost unjustified"
                if recent > high
                else "check whether the sources changed: a gate that rejects everything usually "
                "means the mined phrases stopped being products"
            ),
            reversible_with="revert the prompt or weights commit (git revert <sha>)",
        )

    if baseline_n >= min_decisions and abs(recent - baseline) > _MOVEMENT_TOLERANCE:
        direction = "up" if recent > baseline else "down"
        return DriftFinding(
            kind="keep_rate_moved",
            severity="info",
            runbook="keep-rate-drift",
            summary=(
                f"judge keep-rate moved {direction} {baseline:.0%} -> {recent:.0%} over "
                f"{window_days} day(s), still inside the band"
            ),
            detail={
                "recent": round(recent, 4),
                "baseline": round(baseline, 4),
                "band": [low, high],
            },
            suggested_action=(
                "no action needed if a source or prompt change explains it; otherwise watch it for "
                "another night before touching anything"
            ),
            reversible_with="n/a (observation only)",
        )
    return None


def record_eval_run(
    session: Session,
    *,
    scores: dict[str, float],
    run_at: datetime | None = None,
    tolerance: float = DEFAULT_RUBRIC_DROP_TOLERANCE,
) -> list[DriftFinding]:
    """Store this run's eval scores and report any rubric that fell too far.

    §8's release rule is a *drop* rule, so the comparison is against the stored baseline rather than
    against the previous run: a baseline that creeps down one point at a time would pass a
    run-to-run check forever and still be a regression after a month. The first run records the
    baseline; later runs report.
    """
    reference = run_at or datetime.now(UTC)
    findings: list[DriftFinding] = []
    for case_id, score in scores.items():
        row = session.get(EvalCase, case_id)
        if row is None:
            continue
        if row.baseline_score is None:
            row.baseline_score = score
        elif score < float(row.baseline_score) - tolerance:
            findings.append(
                DriftFinding(
                    kind="eval_baseline_drop",
                    severity="critical",
                    runbook="eval-baseline-drop",
                    summary=(
                        f"case {case_id} scored {score:.1f}, more than {tolerance:.1f} below its "
                        f"baseline {float(row.baseline_score):.1f}"
                    ),
                    detail={
                        "case_id": case_id,
                        "score": score,
                        "baseline": float(row.baseline_score),
                        "tolerance": tolerance,
                    },
                    suggested_action=(
                        "diff what changed since the baseline run (scorer, prompt, or weights); do "
                        "not re-baseline until the cause is known"
                    ),
                    reversible_with="git revert <sha> of the change since the baseline run",
                )
            )
        row.last_score = score
        row.last_run_at = reference
    session.flush()
    return findings


def eval_baseline_drop(session: Session) -> list[DriftFinding]:
    """Report every case whose last score sits below its baseline beyond the tolerance."""
    findings: list[DriftFinding] = []
    for row in session.execute(select(EvalCase)).scalars().all():
        if row.baseline_score is None or row.last_score is None:
            continue
        if float(row.last_score) < float(row.baseline_score) - DEFAULT_RUBRIC_DROP_TOLERANCE:
            findings.append(
                DriftFinding(
                    kind="eval_baseline_drop",
                    severity="critical",
                    runbook="eval-baseline-drop",
                    summary=(
                        f"case {row.id} is at {float(row.last_score):.1f}, below the baseline "
                        f"{float(row.baseline_score):.1f}"
                    ),
                    detail={
                        "case_id": str(row.id),
                        "score": float(row.last_score),
                        "baseline": float(row.baseline_score),
                    },
                    suggested_action="find the change since the baseline run before re-baselining",
                    reversible_with="git revert <sha>",
                )
            )
    return findings


def watermark_drift(
    session: Session,
    *,
    now: datetime | None = None,
    stale_schedules: tuple[str, ...] = ("hourly", "nightly"),
) -> list[DriftFinding]:
    """Sources whose watermark has not moved in longer than their schedule promises.

    A stuck watermark is the quietest failure in the system: the source reports `ok`, the ledger
        shows
    requests, and no new signals arrive — because the cursor never advanced past the same page.
    """
    reference = now or datetime.now(UTC)
    limits = {"hourly": timedelta(hours=6), "nightly": timedelta(days=3)}
    findings: list[DriftFinding] = []
    sources = session.execute(
        select(Source).where(Source.enabled.is_(True), Source.schedule.in_(stale_schedules))
    ).scalars().all()
    for source in sources:
        if source.watermark_updated_at is None:
            continue  # never advanced: the health CLI already reports never_run
        updated = (
            source.watermark_updated_at
            if source.watermark_updated_at.tzinfo
            else source.watermark_updated_at.replace(tzinfo=UTC)
        )
        limit = limits.get(str(source.schedule), timedelta(days=3))
        age = reference - updated
        if age > limit:
            findings.append(
                DriftFinding(
                    kind="stale_watermark",
                    severity="warn",
                    runbook="source-schema-change",
                    summary=(
                        f"{source.id}: watermark has not moved for {age.days}d "
                        f"({source.schedule} expects < {limit})"
                    ),
                    detail={
                        "source_id": str(source.id),
                        "schedule": str(source.schedule),
                        "watermark_age_hours": round(age.total_seconds() / 3600, 1),
                    },
                    suggested_action=(
                        "check the source's recent payloads: an approval or schema change usually "
                        "shows up as a cursor that stops matching"
                    ),
                    reversible_with="re-enable the source once its plugin is fixed",
                )
            )
    return findings


def recent_run_health(session: Session, *, limit: int = 5) -> dict[str, Any]:
    """A small summary of the last few runs — the context a drift report needs to be readable."""
    runs = session.execute(
        select(Run).order_by(Run.started_at.desc()).limit(limit)
    ).scalars().all()
    decided = int(
        session.execute(
            select(func.count()).select_from(Candidate).where(Candidate.status.in_(("kept",
                "dropped")))
        ).scalar_one()
    )
    return {
        "runs": [
            {"id": str(run.id), "status": str(run.status), "started_at": run.started_at.isoformat()}
            for run in runs
        ],
        "decided_candidates": decided,
    }
