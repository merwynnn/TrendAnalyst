"""L3 — decide: features, MGS, fad flag, revenue triple for the candidates L1 kept.

The layer is a straight pipeline over pure functions, which is the point: mining hands over
phrases, this module reads their evidence out of the lake, and `scoring/*` turns it into
numbers. Nothing here talks to the network, and nothing here writes unless the caller asks.

**One design decision that is easy to miss and expensive to get wrong.** A signal's value is
in its source's own unit — Hacker News upvotes, Reddit score, Wikipedia daily pageviews.
Summing them would produce a number with no unit at all. So each signal is first converted
to a **percentile within its own source** (over the window being scored), and the series a
phrase is scored on is the sum of those percentiles: an *attention index*, where 100 means
"as notable as the most notable thing that source has seen in this window". The index is
comparable across phrases and across runs; the raw sum would not have been.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.categories import Taxonomy, normalize_phrase
from trend_analyst.pipeline.layers.l1 import MinedPhrase
from trend_analyst.scoring.fad import FAD_WEIGHTS_V1, FadAssessment, FadWeights, classify
from trend_analyst.scoring.features import EntityFeatures, SignalPoint, extract_features
from trend_analyst.scoring.mgs import WEIGHTS_V1, MgSWeights, ScoreResult, score_all
from trend_analyst.scoring.normalize import percentile_ranks
from trend_analyst.scoring.revenue import MODEL_V1, RevenueModel, RevenueTriple, estimate_revenue
from trend_analyst.store.models import SignalRow

__all__ = [
    "DEFAULT_FEATURE_WINDOW_DAYS",
    "L3Report",
    "ScoredCandidate",
    "load_attention_points",
    "score_phrases",
]

#: The fad classifier reads a 365-day frame, and the features need it even when the lake is
#: young: missing days are zeros, which is the truth about a pipeline three nights old.
DEFAULT_FEATURE_WINDOW_DAYS: Final = 365


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One candidate, fully scored: the snapshot in memory, before it is written."""

    phrase: str
    category_id: str
    result: ScoreResult
    fad: FadAssessment
    revenue: RevenueTriple
    mentions: int

    @property
    def mgs(self) -> float:
        return self.result.mgs

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.result.as_dict(),
            "fad": self.fad.as_dict(),
            "revenue": self.revenue.as_dict(),
            "mentions": self.mentions,
        }


@dataclass(slots=True)
class L3Report:
    """What the decide layer did: how many phrases, how they spread, what could not be scored."""

    scored: int = 0
    skipped_no_evidence: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    small_sample_categories: tuple[str, ...] = ()
    fad_labels: dict[str, int] = field(default_factory=dict)
    candidates: tuple[ScoredCandidate, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "skipped_no_evidence": self.skipped_no_evidence,
            "by_category": dict(sorted(self.by_category.items())),
            "small_sample_categories": list(self.small_sample_categories),
            "fad_labels": dict(sorted(self.fad_labels.items())),
        }

    def summary(self) -> str:
        """One line for the run's ledger note."""
        labels = ", ".join(f"{label}={count}" for label, count in sorted(self.fad_labels.items()))
        small = (
            f"; small sample: {', '.join(self.small_sample_categories)}"
            if self.small_sample_categories
            else ""
        )
        return f"L3: scored {self.scored} candidates ({labels or 'no labels'}){small}"


def load_attention_points(
    session: Session,
    *,
    as_of: datetime,
    window_days: int = DEFAULT_FEATURE_WINDOW_DAYS,
    source_ids: Sequence[str] | None = None,
) -> list[SignalPoint]:
    """Read the lake window and convert every value into a per-source attention percentile.

    One query, then plain Python: the alternative (a LIKE query per phrase) would issue one
    statement per candidate and still need the population to normalize against.
    """
    cutoff = as_of - timedelta(days=window_days)
    statement = select(
        SignalRow.source_id,
        SignalRow.entity,
        SignalRow.metric,
        SignalRow.value,
        SignalRow.ts,
        SignalRow.quote,
    ).where(SignalRow.ts >= cutoff)
    if source_ids is not None:
        statement = statement.where(SignalRow.source_id.in_(list(source_ids)))
    rows = session.execute(statement).all()

    # Percentiles are computed per (source, metric): the same source can publish different
    # units, and a pageview and an upvote must never share a population.
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.source_id), str(row.metric))].append(float(row.value))
    ranks: dict[tuple[str, str], list[float]] = {
        key: percentile_ranks(values) for key, values in grouped.items()
    }
    offsets: dict[tuple[str, str], int] = defaultdict(int)

    points: list[SignalPoint] = []
    for row in rows:
        key = (str(row.source_id), str(row.metric))
        index = offsets[key]
        offsets[key] += 1
        text = str(row.entity)
        if row.quote:
            text = f"{text} | {row.quote}"
        points.append(
            SignalPoint(
                ts=row.ts,
                value=ranks[key][index],
                text=text,
                source_id=str(row.source_id),
                metric=str(row.metric),
            )
        )
    return points


def _points_for_phrase(phrase: str, points: Sequence[SignalPoint]) -> list[SignalPoint]:
    """Signals whose text contains the phrase, matched in the same normalized space.

    Substring, word-boundary-free on purpose: the phrase was extracted *from* the text, so a
    title that says "standing desk mat" is a mention of "desk mat" even though "desk mat" is
    not a whitespace-delimited substring of anything shorter.
    """
    normalized = normalize_phrase(phrase)
    if not normalized:
        return []
    return [
        point
        for point in points
        if normalized in normalize_phrase(point.text)
        or normalized in normalize_phrase(point.metric)
    ]


def score_phrases(
    mined: Sequence[MinedPhrase],
    points: Sequence[SignalPoint],
    *,
    taxonomy: Taxonomy,
    as_of: datetime,
    weights: MgSWeights = WEIGHTS_V1,
    fad_weights: FadWeights = FAD_WEIGHTS_V1,
    revenue_model: RevenueModel = MODEL_V1,
    match_points: Mapping[str, Sequence[SignalPoint]] | None = None,
) -> L3Report:
    """Score every mined phrase: features, per-category percentiles, fad flag, revenue.

    Args:
        mined: the phrases L1 kept.
        points: the whole lake window (see `load_attention_points`).
        match_points: optional pre-computed phrase -> points mapping. `mine()` already knows
            which signals produced each phrase, and passing that in avoids a second match
            that could disagree with the first.
    """
    report = L3Report()
    features_by_category: dict[str, list[EntityFeatures]] = defaultdict(list)
    #: (category, phrase) -> features, so the scoring loop looks each one up once.
    feature_index: dict[tuple[str, str], EntityFeatures] = {}

    for item in mined:
        phrase_points = (
            list(match_points[item.phrase])
            if match_points is not None and item.phrase in match_points
            else _points_for_phrase(item.phrase, points)
        )
        if not phrase_points:
            # Mining saw it, scoring cannot find it: a real inconsistency, counted rather
            # than silently scored as zero (the same failure mode as the P1 leash bug).
            report.skipped_no_evidence += 1
            continue
        features = extract_features(item.phrase, phrase_points, as_of=as_of)
        features_by_category[item.category_id].append(features)
        feature_index[(item.category_id, item.phrase)] = features

    scored = score_all(features_by_category, taxonomy=taxonomy, as_of=as_of, weights=weights)
    mentions = {item.phrase: item.mentions for item in mined}
    candidates: list[ScoredCandidate] = []

    for result in scored:
        features = feature_index[(result.category_id, result.entity)]
        category = taxonomy.by_id(result.category_id)
        fad = classify(features, weights=fad_weights)
        revenue = estimate_revenue(features, category, model=revenue_model)
        candidates.append(
            ScoredCandidate(
                phrase=result.entity,
                category_id=result.category_id,
                result=result,
                fad=fad,
                revenue=revenue,
                mentions=mentions[result.entity],
            )
        )
        report.by_category[result.category_id] = report.by_category.get(result.category_id, 0) + 1
        report.fad_labels[fad.label] = report.fad_labels.get(fad.label, 0) + 1

    report.scored = len(candidates)
    report.candidates = tuple(candidates)
    report.small_sample_categories = tuple(
        sorted(
            {
                result.category_id
                for result in scored
                if result.small_sample
            }
        )
    )
    return report


def phrase_point_index(
    mined: Iterable[MinedPhrase], points: Sequence[SignalPoint]
) -> dict[str, list[SignalPoint]]:
    """Pre-compute the phrase -> points mapping once, for callers that score many phrases."""
    return {item.phrase: _points_for_phrase(item.phrase, points) for item in mined}
