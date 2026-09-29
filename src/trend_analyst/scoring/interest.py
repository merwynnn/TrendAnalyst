"""Current interest: how much attention an idea commands right now, in raw counts.

Velocity (DV) asks whether attention is *accelerating*; interest asks how much of it
there *is* — mentions plus the raw engagement the lake already stores (views, likes,
upvotes, comments). Both matter: a fast riser from zero is a different bet than a
giant everyone already argues about, and MGS v1 could not tell them apart.

Raw counts, one lake query, plain-Python matching in the scorer's normalized space —
then a percentile across the night's candidates to 0-100, so a big-data night and a
quiet night both use the whole scale. The percentile is deliberately *global*, not
per-category like the other sub-scores: heat is absolute, and normalizing it away
per niche would hide exactly the giant everyone is talking about.

Deeper per-product numbers (sold listings, review volumes) are L2's job on the top
candidates — this is the cheap whole-lake pass that decides what deserves that spend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.categories import normalize_phrase
from trend_analyst.scoring.normalize import percentile_ranks
from trend_analyst.scoring.passion import ENGAGEMENT_KEYS
from trend_analyst.store.models import SignalRow

__all__ = ["phrase_interest"]

#: How far back "current" looks. Shorter than the mining window on purpose: interest
#: is now, velocity already covers the trend.
INTEREST_WINDOW_DAYS: Final = 30


def _raw_engagement(metric: str, value: float, metadata: Any) -> float:
    """One signal's countable chatter: engagement-metric values plus metadata counts.

    Mirrors the passion scorer's definition of talking (same key set, same fallback):
    the two must never disagree about what counts as engagement.
    """
    total = value if metric.lower() in ENGAGEMENT_KEYS else 0.0
    if isinstance(metadata, dict):
        for key, item in metadata.items():
            if str(key).lower() not in ENGAGEMENT_KEYS:
                continue
            try:
                total += max(0.0, float(item))
            except (TypeError, ValueError):
                continue
    return total


def phrase_interest(
    session: Session,
    phrases: Sequence[str],
    *,
    as_of: datetime,
    window_days: int = INTEREST_WINDOW_DAYS,
) -> dict[str, float]:
    """Raw interest per phrase: mention count plus raw engagement, over the window.

    Returns 0-100 percentiles across the given phrases (0 when a phrase has no signal
    in the window — absent interest, not missing data). Unknown phrases are not in
    the map at all: the scorer defaults them, the dashboard does not invent them.
    """
    needles = {phrase: normalize_phrase(phrase) for phrase in phrases}
    cutoff = as_of - timedelta(days=window_days)
    rows = session.execute(
        select(
            SignalRow.entity,
            SignalRow.quote,
            SignalRow.metric,
            SignalRow.value,
            SignalRow.metadata_json,
        ).where(SignalRow.ts >= cutoff)
    ).all()
    mentions: dict[str, int] = dict.fromkeys(phrases, 0)
    chatter: dict[str, float] = dict.fromkeys(phrases, 0.0)
    for row in rows:
        haystack = normalize_phrase(f"{row.entity or ''} {row.quote or ''}")
        engagement = _raw_engagement(
            str(row.metric or ""), float(row.value or 0.0), row.metadata_json
        )
        for phrase, needle in needles.items():
            if needle and needle in haystack:
                mentions[phrase] += 1
                chatter[phrase] += engagement
    present = [phrase for phrase in phrases if mentions[phrase] > 0]
    if not present:
        return {}
    # Mentions dominate, engagement breaks ties: a commented-on giant outranks a
    # quietly-mentioned one, but ten silent mentions still beat one loud thread.
    raw = [float(mentions[phrase]) + chatter[phrase] / 100.0 for phrase in present]
    ranks = percentile_ranks(raw)
    return dict(zip(present, ranks, strict=True))


def interest_for(
    interest: Mapping[str, float] | None, phrase: str
) -> float:
    """One phrase's interest, defaulting to 0 when the map does not know it."""
    if not interest:
        return 0.0
    return float(interest.get(phrase, 0.0))
