"""Feature extraction: turn one entity's signals into the numbers the scorer consumes.

This module knows about signals, windows and text markers. It does **not** know about
categories, percentiles or the gap score — those need the whole population, and mixing the
two is how a "per-category percentile" quietly becomes a global one.

The windows are the spec's (§6.1): 30 days is "now", 90 days is the baseline it is compared
against, and 365 days is the frame the fad classifier reads for anything that is not new.

Text markers are the honest part of P2. Saturation, money and feasibility are not yet
observable — the Tier-A sources that measure them (sold-versus-listed counts, review
counts, price distributions) arrive in P4 — so the features below are **proxies**, each
labelled with what it is standing in for. They are computed in deterministic code over
real text, never by a model, and the scorer's docstrings say where the upgrade lands.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from trend_analyst.scoring.normalize import (
    clamp,
    daily_series,
    ewma,
    growth_ratio,
    mean,
    median,
    normalized_slope,
    safe_ratio,
)

__all__ = [
    "COMPLEXITY_MARKERS",
    "INTENT_MARKERS",
    "PAIN_MARKERS",
    "EntityFeatures",
    "SignalPoint",
    "count_markers",
    "extract_features",
]

#: Words that say something is wrong with what exists. The Pain sub-score is built from
#: these: a market worth entering is one where the people in it are already complaining.
PAIN_MARKERS: Final[tuple[str, ...]] = (
    "wish", "hate", "hated", "broken", "broke", "breaks", "fell apart", "falling apart",
    "cheaply", "flimsy", "poor quality", "junk", "garbage", "useless", "worst", "terrible",
    "awful", "annoying", "frustrating", "frustrated", "disappointing", "regret",
    "doesn't work", "does not work", "stopped working", "stopped charging", "failed after",
    "refund", "returned it", "return policy", "waste of money", "overpriced", "ripped off",
    "scam", "recall", "defective", "leak", "leaks", "leaking", "cracked", "rust",
    "peeled", "wore out", "worn out", "won't stay", "won't hold", "can't find", "never lasts",
    "only lasted", "lasted a month", "lasted two", "no longer works", "problem with",
    "issue with", "issues with", "anyone else having",
)

#: Words that say someone is about to buy. Money potential is about intent, not volume.
INTENT_MARKERS: Final[tuple[str, ...]] = (
    "looking for", "i need", "need a", "need an", "where to buy", "where can i buy",
    "best", "recommend", "recommendations", "which one", "what do you use", "anyone know",
    "alternative", "alternatives", "replacement", "upgrade", "worth it", "should i buy",
    "in search of", "iso", "help me find", "is there a", "wish there was", "would buy",
    "take my money",
)

#: Words that make a product harder for one person to ship: radios, firmware, cells,
#: sensors, apps. Not fatal — just a reason to prefer the simpler idea next to it.
COMPLEXITY_MARKERS: Final[tuple[str, ...]] = (
    "app", "bluetooth", "wifi", "wireless", "sensor", "battery", "rechargeable", "ai",
    "camera", "microcontroller", "firmware", "cloud", "subscription", "solar", "motor",
    "heater", "pump", "laser",
)

#: Deadline marker for "as of": missing days are zeros, so a window is never sparse.
_DEFAULT_ALPHA: Final = 0.3
_MIN_DAYS_FOR_SEASONALITY: Final = 14
#: How many quoted texts to keep with the features, for the snapshot and the brief.
_SAMPLE_TEXTS: Final = 5
#: Monday=0 .. Friday=4; Saturday and Sunday are the weekend half of a seasonality split.
_FRIDAY: Final = 5


@dataclass(frozen=True, slots=True)
class SignalPoint:
    """One normalized fact about an entity: when, how much, and what was said."""

    ts: datetime
    value: float
    text: str = ""
    source_id: str = ""
    metric: str = ""

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(f"signal values are non-negative, got {self.value}")
        if self.ts.tzinfo is None:
            raise ValueError("signal timestamps must be timezone-aware (UTC)")


@dataclass(slots=True)
class EntityFeatures:
    """Everything the scorer can know about an entity, before any population statistics."""

    entity: str
    #: Windows (spec §6.1), as sums of signal values.
    volume_30d: float = 0.0
    volume_90d: float = 0.0
    volume_365d: float = 0.0
    #: Volume in the 90-30 day band: the baseline "now" is compared against.
    volume_prior_60d: float = 0.0
    #: Daily series, oldest first, for the EWMA and the slopes.
    series_30d: list[float] = field(default_factory=list)
    series_90d: list[float] = field(default_factory=list)
    series_365d: list[float] = field(default_factory=list)
    #: Smoothed current level (the EWMA of the 30-day series).
    ewma_30d: float = 0.0
    #: Slope divergence the fad classifier reads (per-step, scale-free).
    slope_30d: float = 0.0
    slope_90d: float = 0.0
    slope_365d: float = 0.0
    #: Activity shape.
    active_days_30d: int = 0
    active_days_365d: int = 0
    days_since_last: int = 365
    #: How tall is the tallest day compared with a normal day (fad shape).
    spike_ratio: float = 0.0
    #: Weekday-vs-weekend split, as a divergence from 1.0 (0.0 = no weekday pattern).
    seasonality: float = 0.0
    #: Share of the 365-day window in which the entity was active at all.
    evergreen_ratio: float = 0.0
    #: Text evidence.
    pain_ratio: float = 0.0
    pain_signals: int = 0
    intent_ratio: float = 0.0
    intent_signals: int = 0
    #: Corroboration: how many distinct sources saw this entity, and their weights.
    source_count: int = 0
    source_ids: tuple[str, ...] = ()
    #: Raw engagement (upvotes, views) — normalized per category later.
    engagement: float = 0.0
    #: The phrases that produced these features (for quotes and for debugging).
    sample_texts: tuple[str, ...] = ()

    @property
    def growth(self) -> float:
        """Growth of the last 30 days against the 60 days before it."""
        return growth_ratio(self.volume_30d, self.volume_prior_60d)

    @property
    def slope_divergence(self) -> float:
        """30-day slope minus 90-day slope: positive means "accelerating".

        The fad classifier's first feature (spec §6.1): a spike that is already in its
        30-day slope but not yet in its 90-day slope is the signature of a fad, while
        both rising together is a trend.
        """
        return self.slope_30d - self.slope_90d

    def as_dict(self) -> dict[str, float | int | str | list[float] | tuple[str, ...]]:
        """Plain-data view, for the score snapshot's feature blob and for the tests."""
        return {
            "entity": self.entity,
            "volume_30d": round(self.volume_30d, 4),
            "volume_90d": round(self.volume_90d, 4),
            "volume_365d": round(self.volume_365d, 4),
            "volume_prior_60d": round(self.volume_prior_60d, 4),
            "ewma_30d": round(self.ewma_30d, 4),
            "slope_30d": round(self.slope_30d, 4),
            "slope_90d": round(self.slope_90d, 4),
            "slope_365d": round(self.slope_365d, 4),
            "slope_divergence": round(self.slope_divergence, 4),
            "growth": round(self.growth, 4),
            "spike_ratio": round(self.spike_ratio, 4),
            "seasonality": round(self.seasonality, 4),
            "evergreen_ratio": round(self.evergreen_ratio, 4),
            "active_days_30d": self.active_days_30d,
            "active_days_365d": self.active_days_365d,
            "days_since_last": self.days_since_last,
            "pain_ratio": round(self.pain_ratio, 4),
            "pain_signals": self.pain_signals,
            "intent_ratio": round(self.intent_ratio, 4),
            "intent_signals": self.intent_signals,
            "source_count": self.source_count,
            "engagement": round(self.engagement, 4),
        }


def _marker_pattern(markers: Sequence[str]) -> re.Pattern[str]:
    """One alternation, longest marker first so "doesn't work" wins over "work"."""
    ordered = sorted(markers, key=len, reverse=True)
    return re.compile("|".join(re.escape(marker) for marker in ordered))


_PAIN_PATTERN: Final = _marker_pattern(PAIN_MARKERS)
_INTENT_PATTERN: Final = _marker_pattern(INTENT_MARKERS)
_COMPLEXITY_PATTERN: Final = _marker_pattern(COMPLEXITY_MARKERS)


def count_markers(texts: Sequence[str], pattern: re.Pattern[str]) -> int:
    """How many of ``texts`` contain at least one marker (per-text, not per-match).

    Per-text counting is the honest unit: a single rant that says "broken" five times is
    one unhappy customer, not five.
    """
    return sum(1 for text in texts if text and pattern.search(text.lower()))


def complexity_hits(texts: Sequence[str]) -> int:
    """Which complexity markers appear anywhere in the texts (for the feasibility penalty)."""
    joined = " ".join(text.lower() for text in texts if text)
    return len({marker for marker in COMPLEXITY_MARKERS if _COMPLEXITY_PATTERN.search(joined)
                and marker in joined})


def _seasonality_ratio(
    timestamps: Sequence[datetime],
    values: Sequence[float],
    *,
    as_of: datetime,
    days: int,
) -> float:
    """Divergence between weekday and weekend activity, 0.0 when they are alike.

    Computed as ``|weekday_mean - weekend_mean| / overall_mean`` over the window — the
    "seasonal strength" the fad classifier reads. Under two weeks of history there is not
    enough data for a weekday split, so it reports no pattern rather than a noisy one.
    """
    if days < _MIN_DAYS_FOR_SEASONALITY or not values:
        return 0.0
    start_day = (as_of - timedelta(days=days - 1)).date()
    weekday_total = 0.0
    weekday_days = 0
    weekend_total = 0.0
    weekend_days = 0
    buckets: dict[object, float] = {}
    for timestamp, value in zip(timestamps, values, strict=True):
        bucket = timestamp.date()
        if bucket >= start_day:
            buckets[bucket] = buckets.get(bucket, 0.0) + value
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        total = buckets.get(day, 0.0)
        if day.weekday() < _FRIDAY:
            weekday_total += total
            weekday_days += 1
        else:
            weekend_total += total
            weekend_days += 1
    weekday_mean = safe_ratio(weekday_total, float(weekday_days))
    weekend_mean = safe_ratio(weekend_total, float(weekend_days))
    overall = mean([weekday_mean, weekend_mean])
    if overall == 0:
        return 0.0
    return clamp(abs(weekday_mean - weekend_mean) / overall, 0.0, 5.0)


def extract_features(
    entity: str,
    points: Sequence[SignalPoint],
    *,
    as_of: datetime,
    alpha: float = _DEFAULT_ALPHA,
) -> EntityFeatures:
    """Build the feature vector for one entity from its signals.

    Args:
        entity: the normalized phrase or entity name.
        points: every signal for this entity (any source, any time).
        as_of: the reference instant — must be tz-aware and is passed in, never read from
            the clock, so a replay reproduces the exact numbers (spec §5.4).
        alpha: EWMA weight of the newest day.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware (UTC)")

    texts = [point.text for point in points if point.text]
    events = [(point.ts, point.value) for point in points]

    series_30d = daily_series(events, as_of=as_of, days=30)
    series_90d = daily_series(events, as_of=as_of, days=90)
    series_365d = daily_series(events, as_of=as_of, days=365)

    volume_30d = sum(series_30d)
    volume_90d = sum(series_90d)
    volume_365d = sum(series_365d)
    # The 60 days before the recent 30: subtracting window sums keeps every window defined
    # by the same series, so no boundary can be counted twice.
    volume_prior_60d = max(volume_90d - volume_30d, 0.0)

    active_days_30d = sum(1 for value in series_30d if value > 0)
    active_days_365d = sum(1 for value in series_365d if value > 0)
    positive = [value for value in series_365d if value > 0]
    spike_ratio = (
        clamp(max(series_365d) / median(positive), 0.0, 100.0) if positive else 0.0
    )
    days_since_last = (
        min((as_of.date() - point.ts.date()).days for point in points) if points else 365
    )

    pain_signals = count_markers(texts, _PAIN_PATTERN)
    intent_signals = count_markers(texts, _INTENT_PATTERN)

    return EntityFeatures(
        entity=entity,
        volume_30d=volume_30d,
        volume_90d=volume_90d,
        volume_365d=volume_365d,
        volume_prior_60d=volume_prior_60d,
        series_30d=series_30d,
        series_90d=series_90d,
        series_365d=series_365d,
        ewma_30d=ewma(series_30d, alpha),
        slope_30d=normalized_slope(series_30d),
        slope_90d=normalized_slope(series_90d),
        slope_365d=normalized_slope(series_365d),
        active_days_30d=active_days_30d,
        active_days_365d=active_days_365d,
        days_since_last=days_since_last,
        spike_ratio=spike_ratio,
        seasonality=_seasonality_ratio(
            [point.ts for point in points], [point.value for point in points], as_of=as_of, days=90
        ),
        evergreen_ratio=active_days_365d / 365.0,
        pain_ratio=safe_ratio(float(pain_signals), float(len(points))),
        pain_signals=pain_signals,
        intent_ratio=safe_ratio(float(intent_signals), float(len(points))),
        intent_signals=intent_signals,
        source_count=len({point.source_id for point in points if point.source_id}),
        source_ids=tuple(sorted({point.source_id for point in points if point.source_id})),
        engagement=max((point.value for point in points), default=0.0),
        sample_texts=tuple(texts[:_SAMPLE_TEXTS]),
    )
