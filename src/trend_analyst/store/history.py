"""Snapshot history views: the append-only record read back (spec §5.3, brief P4).

Spec §5.3: *"Scores are versioned rows (candidate, run_id, weights_version, five sub-scores, fad
probability, revenue triple). History is never updated in place. Trends, backtests and weight
learning all read the snapshot table."*

This module is the "read the snapshot table" part, and it exists because the append-only design is
only useful if something can actually see the trend. Three views:

* **history** — every snapshot of one candidate, oldest first, so a score's movement is visible;
* **delta** — what changed between the first and the newest snapshot, per sub-score, which is how a
  weights change or a demand shift shows up;
* **version comparison** — the same candidate scored under two weights versions side by side, which
  is what makes a weights change reviewable rather than a leap of faith.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trend_analyst.store.models import Candidate, Score

__all__ = ["SnapshotDelta", "SnapshotPoint", "compare_versions", "history", "history_delta"]

#: A delta needs two points; with one, "nothing moved" would be a lie of omission.
_TWO_POINTS = 2

#: Sub-scores recorded per snapshot, in the order the specification lists them.
SUBSCORES: Final[tuple[str, ...]] = ("dv", "ss", "sp", "mp", "fe")


@dataclass(frozen=True, slots=True)
class SnapshotPoint:
    """One snapshot: when, which weights, what it said."""

    scored_at: str
    run_id: str
    weights_version: str
    mgs: float
    sub_scores: dict[str, float]
    fad_label: str
    fad_probability: float
    revenue: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored_at": self.scored_at,
            "run_id": self.run_id,
            "weights_version": self.weights_version,
            "mgs": round(self.mgs, 2),
            "sub_scores": {key: round(value, 2) for key, value in self.sub_scores.items()},
            "fad_label": self.fad_label,
            "fad_probability": round(self.fad_probability, 4),
            "revenue": {key: round(value, 2) for key, value in self.revenue.items()},
        }


@dataclass(frozen=True, slots=True)
class SnapshotDelta:
    """What moved between two snapshots, with the direction made explicit."""

    phrase: str
    from_version: str
    to_version: str
    points: int
    mgs_delta: float
    sub_score_deltas: dict[str, float]
    revenue_delta: float
    fad_moved: bool
    notes: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.mgs_delta > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "points": self.points,
            "mgs_delta": round(self.mgs_delta, 2),
            "sub_score_deltas": {
                key: round(value, 2) for key, value in self.sub_score_deltas.items()
            },
            "revenue_delta": round(self.revenue_delta, 2),
            "fad_moved": self.fad_moved,
            "improved": self.improved,
            "notes": self.notes,
        }

    def render(self) -> str:
        arrow = "+" if self.mgs_delta >= 0 else ""
        lines = [
            f"{self.phrase}: {self.points} snapshot(s) · {self.from_version} -> {self.to_version}",
            f"  MGS {arrow}{self.mgs_delta:.2f} · revenue P50 {self.revenue_delta:+,.0f}"
            + ("  (fad label changed)" if self.fad_moved else ""),
        ]
        for key, value in self.sub_score_deltas.items():
            lines.append(f"  {key.upper():>3} {value:+.2f}")
        lines.extend(f"  note: {note}" for note in self.notes)
        return "\n".join(lines)


def _point(row: Score) -> SnapshotPoint:
    return SnapshotPoint(
        scored_at=row.scored_at.isoformat() if row.scored_at else "",
        run_id=str(row.run_id),
        weights_version=str(row.weights_version),
        mgs=float(row.mgs),
        sub_scores={
            "dv": float(row.demand_velocity),
            "ss": float(row.saturation),
            "sp": float(row.buyer_pain),
            "mp": float(row.money),
            "fe": float(row.feasibility),
        },
        fad_label=str(row.fad_label),
        fad_probability=float(row.fad_probability),
        revenue={
            "p10": float(row.revenue_p10),
            "p50": float(row.revenue_p50),
            "p90": float(row.revenue_p90),
        },
    )


def history(
    session: Session,
    *,
    phrase: str,
    weights_version: str | None = None,
    limit: int = 200,
) -> list[SnapshotPoint]:
    """Every snapshot of one candidate, oldest first — the trend the ledger exists to show."""
    statement = (
        select(Score)
        .join(Candidate, Candidate.id == Score.candidate_id)
        .where(Candidate.phrase == phrase)
        .order_by(Score.scored_at.asc(), Score.id.asc())
        .limit(limit)
    )
    if weights_version is not None:
        statement = statement.where(Score.weights_version == weights_version)
    return [_point(row) for row in session.execute(statement).scalars().all()]


def history_delta(
    session: Session,
    *,
    phrase: str,
    weights_version: str | None = None,
) -> SnapshotDelta | None:
    """First-vs-newest movement for one candidate, or None when it has fewer than two snapshots.

    A delta needs two points; with one, the honest answer is None rather than a zero that reads as
    "nothing moved".
    """
    points = history(session, phrase=phrase, weights_version=weights_version)
    if len(points) < _TWO_POINTS:
        return None
    first, last = points[0], points[-1]
    notes: list[str] = []
    if first.weights_version != last.weights_version:
        notes.append(
            "the weights version changed between these snapshots, so some of this movement is the "
            "model rather than the market"
        )
    movements = (last.sub_scores[key] - first.sub_scores[key] for key in SUBSCORES)
    if not any(abs(value) > 0 for value in movements):
        notes.append("no sub-score moved: the score is stable across these runs")
    return SnapshotDelta(
        phrase=phrase,
        from_version=first.weights_version,
        to_version=last.weights_version,
        points=len(points),
        mgs_delta=last.mgs - first.mgs,
        sub_score_deltas={
            key: last.sub_scores[key] - first.sub_scores[key] for key in SUBSCORES
        },
        revenue_delta=last.revenue["p50"] - first.revenue["p50"],
        fad_moved=first.fad_label != last.fad_label,
        notes=notes,
    )


def compare_versions(
    session: Session,
    *,
    phrase: str,
    versions: Sequence[str],
) -> dict[str, SnapshotPoint | None]:
    """The newest snapshot per weights version, for reviewing a weights change.

    Returning None for a version with no snapshot is deliberate: "this candidate was never scored
    under v2" is a fact worth seeing, and an empty point would hide it.
    """
    out: dict[str, SnapshotPoint | None] = {}
    for version in versions:
        points = history(session, phrase=phrase, weights_version=version)
        out[version] = points[-1] if points else None
    return out


def render_history(points: Sequence[SnapshotPoint], *, limit: int = 20) -> str:
    """A console table of snapshots, newest last so the trend reads downward."""
    if not points:
        return "(no snapshots for that phrase)"
    header = f"{'scored at':<26} {'MGS':>6}  {'DV':>4} {'SS':>4} {'SP':>4} {'MP':>4} {'FE':>4}  fad"
    rows = [header, "-" * len(header)]
    for point in points[-limit:]:
        subs = point.sub_scores
        rows.append(
            f"{point.scored_at[:26]:<26} {point.mgs:6.2f}  {subs['dv']:4.0f} {subs['ss']:4.0f} "
            f"{subs['sp']:4.0f} {subs['mp']:4.0f} {subs['fe']:4.0f}  "
            f"{point.fad_label}@{point.fad_probability:.2f} [{point.weights_version}]"
        )
    return "\n".join(rows)
