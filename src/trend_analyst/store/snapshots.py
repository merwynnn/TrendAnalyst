"""Append-only snapshot writes and the ranked view; history is never updated in place.

Spec §5.3: *"Scores are versioned rows (candidate, run_id, weights_version, five
sub-scores, fad probability, revenue triple). History is never updated in place. Trends,
backtests and weight learning all read the snapshot table."*

Three mechanics make that sentence true rather than aspirational:

* **The unique key is (candidate, run, weights_version)**, so a re-run of the same layer in
  the same run writes nothing — an inserted row is the idempotence proof, and
  `write_snapshot` returns whether it wrote rather than assuming it did.
* **A database trigger refuses UPDATE and DELETE** on `scores` (migration 0001). This
  module could not rewrite history if it wanted to; that guarantee lives in the schema,
  not in a code review.
* **A run's snapshot is hashable** (`snapshot_hash`), which is how the specification's
  determinism rule (§5.4: *"replaying a checkpoint with identical inputs MUST reproduce
  identical downstream scores (hash-asserted in CI)"*) becomes a check anyone can run.

Candidates are a different animal: they accumulate. Mining the same phrase twice updates
its mention count and its last-seen stamp, and never rewrites its first-seen one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Row, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_analyst.pipeline.layers.l1 import MinedPhrase
from trend_analyst.scoring.fad import FadAssessment
from trend_analyst.scoring.mgs import ScoreResult
from trend_analyst.scoring.revenue import RevenueTriple
from trend_analyst.store.models import Candidate, Score

__all__ = [
    "RankedScore",
    "ScoreRecord",
    "candidate_key",
    "history_for",
    "latest_ranked",
    "snapshot_hash",
    "upsert_candidates",
    "write_snapshot",
    "write_snapshots",
]

#: Separator for the composite key. A unit separator cannot appear in a phrase, so no two
#: (phrase, category) pairs can collide by concatenation.
_KEY_SEP: Final = "\x1f"


def candidate_key(phrase: str, category: str) -> str:
    """The composite key for looking a candidate up in an `upsert_candidates` result.

    It exists because the first draft built this key inline in two places with the fields in
    two different orders, and the mismatch failed silently: candidates were written, score
    snapshots were not, and the run reported success. One function, one order.
    """
    return f"{phrase}{_KEY_SEP}{category}"


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    """Everything one candidate's snapshot needs: the score, the flag, the money."""

    candidate_id: int
    result: ScoreResult
    fad: FadAssessment
    revenue: RevenueTriple


@dataclass(frozen=True, slots=True)
class RankedScore:
    """A score snapshot joined to its candidate — what the ranked table renders."""

    candidate_id: int
    phrase: str
    category: str
    mgs: float
    demand_velocity: float
    saturation: float
    buyer_pain: float
    money: float
    feasibility: float
    fad_probability: float
    fad_label: str
    revenue_p10: float
    revenue_p50: float
    revenue_p90: float
    weights_version: str
    scored_at: datetime
    run_id: uuid.UUID
    mentions: int

    @property
    def gap(self) -> float:
        """``100 - SS``: the headroom half of the formula, shown in the table."""
        return 100.0 - self.saturation

    def as_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "category": self.category,
            "mgs": round(self.mgs, 2),
            "dv": round(self.demand_velocity, 2),
            "ss": round(self.saturation, 2),
            "sp": round(self.buyer_pain, 2),
            "mp": round(self.money, 2),
            "fe": round(self.feasibility, 2),
            "fad_label": self.fad_label,
            "fad_probability": round(self.fad_probability, 4),
            "revenue_p10": round(self.revenue_p10, 2),
            "revenue_p50": round(self.revenue_p50, 2),
            "revenue_p90": round(self.revenue_p90, 2),
            "weights_version": self.weights_version,
            "mentions": self.mentions,
        }


def upsert_candidates(session: Session, mined: Sequence[MinedPhrase]) -> dict[str, int]:
    """Insert or refresh the candidates extraction produced; returns ``phrase -> id``.

    Accumulating fields move forward only: ``mentions`` takes the new count (it is a
    measurement of now), ``first_seen_at`` the earlier of the two, ``last_seen_at`` the
    later. ``status`` is never touched here — a human or the Judge gate owns that, and an
    extraction run that could reactivate a rejected candidate would undo a decision.
    """
    if not mined:
        return {}

    rows = [
        {
            "phrase": item.phrase,
            "category": item.category_id,
            "mentions": item.mentions,
            "first_seen_at": item.first_seen,
            "last_seen_at": item.last_seen,
        }
        for item in mined
    ]
    insert_statement = pg_insert(Candidate).values(rows)
    statement = insert_statement.on_conflict_do_update(
        constraint="uq_candidates_phrase",
        set_={
            "mentions": insert_statement.excluded.mentions,
            "first_seen_at": func.least(
                Candidate.first_seen_at, insert_statement.excluded.first_seen_at
            ),
            "last_seen_at": func.greatest(
                Candidate.last_seen_at, insert_statement.excluded.last_seen_at
            ),
        },
    ).returning(Candidate.id, Candidate.phrase, Candidate.category)
    result = session.execute(statement).all()
    session.flush()
    return {candidate_key(str(row.phrase), str(row.category)): int(row.id) for row in result}


def write_snapshot(session: Session, run_id: uuid.UUID, record: ScoreRecord) -> bool:
    """Append one score snapshot. Returns True when a row was actually inserted.

    False means the (candidate, run, weights_version) row already existed — a re-run, not an
    error. The caller counts them, which is how "the second run wrote nothing new" becomes a
    number in the evidence instead of a claim.
    """
    statement = (
        pg_insert(Score)
        .values(
            candidate_id=record.candidate_id,
            run_id=run_id,
            weights_version=record.result.weights_version,
            demand_velocity=record.result.sub_scores.dv,
            saturation=record.result.sub_scores.ss,
            buyer_pain=record.result.sub_scores.sp,
            money=record.result.sub_scores.mp,
            feasibility=record.result.sub_scores.fe,
            mgs=record.result.mgs,
            fad_probability=record.fad.probability,
            fad_label=record.fad.label,
            revenue_p10=record.revenue.p10,
            revenue_p50=record.revenue.p50,
            revenue_p90=record.revenue.p90,
        )
        .on_conflict_do_nothing(constraint="uq_scores_candidate_id")
        .returning(Score.id)
    )
    inserted = session.execute(statement).scalar_one_or_none()
    session.flush()
    return inserted is not None


def write_snapshots(session: Session, run_id: uuid.UUID, records: Iterable[ScoreRecord]) -> int:
    """Append many snapshots; returns how many were new."""
    return sum(1 for record in records if write_snapshot(session, run_id, record))


def _ranked_from_row(row: Row[Any]) -> RankedScore:
    """One joined row into the dataclass the table renders."""
    score, candidate = row[0], row[1]
    return RankedScore(
        candidate_id=int(candidate.id),
        phrase=str(candidate.phrase),
        category=str(candidate.category),
        mgs=float(score.mgs),
        demand_velocity=float(score.demand_velocity),
        saturation=float(score.saturation),
        buyer_pain=float(score.buyer_pain),
        money=float(score.money),
        feasibility=float(score.feasibility),
        fad_probability=float(score.fad_probability),
        fad_label=str(score.fad_label),
        revenue_p10=float(score.revenue_p10),
        revenue_p50=float(score.revenue_p50),
        revenue_p90=float(score.revenue_p90),
        weights_version=str(score.weights_version),
        scored_at=score.scored_at,
        run_id=score.run_id,
        mentions=int(candidate.mentions),
    )


def latest_ranked(
    session: Session,
    *,
    run_id: uuid.UUID | None = None,
    limit: int = 50,
    weights_version: str | None = None,
    category: str | None = None,
) -> list[RankedScore]:
    """The ranked table: the newest snapshot per candidate, highest MGS first.

    "Newest per candidate" (not "all of this run") is what makes the table safe to read
    after two nights: a candidate scored both nights appears once, at its latest score, and
    the history stays in the table for the trend queries that need it.
    """
    newest = (
        select(
            Score.candidate_id.label("candidate_id"),
            func.max(Score.scored_at).label("scored_at"),
        )
        .group_by(Score.candidate_id)
        .subquery()
    )
    statement = (
        select(Score, Candidate)
        .join(Candidate, Candidate.id == Score.candidate_id)
        .join(
            newest,
            (newest.c.candidate_id == Score.candidate_id)
            & (newest.c.scored_at == Score.scored_at),
        )
        .order_by(Score.mgs.desc(), Candidate.phrase.asc())
        .limit(limit)
    )
    if run_id is not None:
        statement = statement.where(Score.run_id == run_id)
    if weights_version is not None:
        statement = statement.where(Score.weights_version == weights_version)
    if category is not None:
        statement = statement.where(Candidate.category == category)
    return [_ranked_from_row(row) for row in session.execute(statement).all()]


def history_for(session: Session, phrase: str, category: str | None = None) -> list[RankedScore]:
    """Every snapshot for one candidate, oldest first — the trend line, not the table."""
    statement = (
        select(Score, Candidate)
        .join(Candidate, Candidate.id == Score.candidate_id)
        .where(Candidate.phrase == phrase)
        .order_by(Score.scored_at.asc())
    )
    if category is not None:
        statement = statement.where(Candidate.category == category)
    return [_ranked_from_row(row) for row in session.execute(statement).all()]


def snapshot_hash(session: Session, run_id: uuid.UUID) -> str:
    """A SHA-256 over a run's scores — the determinism check of spec §5.4.

    Covers the numbers that a replay must reproduce exactly (the five sub-scores, MGS, the
    fad flag and the revenue triple) and deliberately not ``scored_at`` or the row id, which
    are allowed to differ between two runs of the same inputs.
    """
    rows = session.execute(
        select(
            Candidate.phrase,
            Candidate.category,
            Score.weights_version,
            Score.demand_velocity,
            Score.saturation,
            Score.buyer_pain,
            Score.money,
            Score.feasibility,
            Score.mgs,
            Score.fad_probability,
            Score.fad_label,
            Score.revenue_p10,
            Score.revenue_p50,
            Score.revenue_p90,
        )
        .join(Candidate, Candidate.id == Score.candidate_id)
        .where(Score.run_id == run_id)
        .order_by(Candidate.phrase.asc(), Candidate.category.asc())
    ).all()

    payload = [
        {
            "phrase": str(row.phrase),
            "category": str(row.category),
            "weights_version": str(row.weights_version),
            "sub_scores": [round(float(value), 6) for value in row[3:8]],
            "mgs": round(float(row.mgs), 6),
            "fad_probability": round(float(row.fad_probability), 6),
            "fad_label": str(row.fad_label),
            "revenue": [round(float(value), 6) for value in (row.revenue_p10, row.revenue_p50,
                                                             row.revenue_p90)],
        }
        for row in rows
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def count_scores(session: Session, run_id: uuid.UUID | None = None) -> int:
    """How many snapshot rows exist (optionally for one run) — used by the gate."""
    statement = select(func.count()).select_from(Score)
    if run_id is not None:
        statement = statement.where(Score.run_id == run_id)
    return int(session.execute(statement).scalar_one())


def count_candidates(session: Session, status: str = "active") -> int:
    """How many candidates the table holds in a given status."""
    return int(
        session.execute(
            select(func.count()).select_from(Candidate).where(Candidate.status == status)
        ).scalar_one()
    )


def age_of_newest_snapshot(session: Session) -> Any:  # pragma: no cover - used by monitors
    """``now() - max(scored_at)``, or None when nothing has been scored yet."""
    return session.execute(select(text("now() - max(scored_at)")).select_from(Score)).scalar()
