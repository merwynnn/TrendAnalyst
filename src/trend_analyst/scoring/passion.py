"""Passion as engagement depth: how much people talk *to each other* per niche.

A passionate market is not a big audience — it is a talking one. The signals already
carry the raw material: per-signal values for engagement metrics (comments, upvotes,
likes…) and per-item counts in `metadata_json` (Hacker News descendants, Reddit
comment counts). This module sums that chatter per category over the window and
min-max normalizes it to 0-100, so a niche where every post sparks a thread outranks
one with ten times the pageviews and no replies.

Pure code, no provider: the dashboard recomputes it live from the lake window, and a
niche with no engagement evidence scores 0 ("nothing measured") rather than a middle
value that would read as "mildly passionate".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trend_analyst.store.models import SignalRow

__all__ = ["ENGAGEMENT_KEYS", "category_passion"]

#: Metric names that count as talking, not viewing. Matched case-insensitively against
#: both the signal's metric and its metadata keys.
ENGAGEMENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "comments",
        "num_comments",
        "replies",
        "upvotes",
        "likes",
        "score",
        "favorites",
        "reblogs",
        "descendants",
    }
)


def _engagement_of(metric: str, value: float, metadata: Any) -> float:
    """One signal's chatter: its value when the metric is engagement, plus metadata counts."""
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


def category_passion(
    session: Session, *, as_of: datetime, window_days: int = 90
) -> dict[str, float]:
    """Engagement depth per category, min-max normalized to 0-100 over the window.

    A single passionate niche normalizes to 100 and every other niche to 0 — honest
    when the lake really has one talking market, and the dashboard shows the raw
    chatter beside the normalized bar so the normalization never misleads.
    """
    cutoff = as_of - timedelta(days=window_days)
    rows = session.execute(
        select(
            SignalRow.category, SignalRow.metric, SignalRow.value, SignalRow.metadata_json
        ).where(SignalRow.ts >= cutoff)
    ).all()
    chatter: dict[str, float] = defaultdict(float)
    for row in rows:
        if not row.category:
            continue
        chatter[str(row.category)] += _engagement_of(
            str(row.metric or ""), float(row.value or 0.0), row.metadata_json
        )
    if not chatter:
        return {}
    peak = max(chatter.values())
    if peak <= 0:
        return dict.fromkeys(chatter, 0.0)
    return {category: 100.0 * total / peak for category, total in chatter.items()}
