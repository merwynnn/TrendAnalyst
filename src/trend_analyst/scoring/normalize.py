"""Normalization primitives: percentiles, z-scores, EWMA, slopes, log compression.

Everything the scorer needs to turn raw counts into comparable numbers, and nothing that
knows about sources or the database. Pure functions over floats and datetimes, so a test
can pin an expected value instead of asserting "it ran".

Two deliberate choices:

* **Percentiles, not z-scores, for the sub-scores.** The spec (§6.1) says the MGS inputs
  are per-category percentiles. Percentiles are bounded (0-100, so no sub-score can run
  away with the total), robust to the heavy tails that view and upvote counts always have,
  and meaningful at the sample sizes this pipeline actually has — where a z-score on five
  observations is a fiction dressed as a statistic.
* **EWMA z-scores for velocity.** The brief asks for them by name, and they are the right
  tool there: an exponentially weighted mean is the "current level" of a noisy daily
  series, and dividing by the category's spread says whether that level is unusual *for
  that category* rather than just large in absolute terms.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Final

__all__ = [
    "DAY",
    "clamp",
    "daily_series",
    "ewma",
    "ewma_series",
    "ewma_z",
    "growth_ratio",
    "log_compress",
    "mean",
    "median",
    "normalized_slope",
    "percentile_ranks",
    "safe_ratio",
    "stddev",
    "summarize",
    "zscore",
]

DAY: Final = timedelta(days=1)

#: Two observations is the minimum for a spread or a direction to exist at all.
_TWO: Final = 2

#: Percentile of a value inside a population of one. Neither 0 nor 100: an only child is
#: not evidence of being either the best or the worst in its category, and saying so keeps
#: a category with a single candidate from producing a perfect score.
SINGLETON_PERCENTILE: Final = 50.0


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp into ``[low, high]`` — every sub-score leaves this module inside 0-100."""
    if math.isnan(value):
        return low
    return max(low, min(high, value))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def stddev(values: Sequence[float], *, sample: bool = True) -> float:
    """Population or sample standard deviation; 0.0 for fewer than two values."""
    if len(values) < _TWO:
        return 0.0
    average = mean(values)
    divisor = len(values) - 1 if sample else len(values)
    variance = sum((value - average) ** 2 for value in values) / divisor
    return math.sqrt(max(variance, 0.0))


def safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that returns ``default`` instead of raising or returning infinity."""
    if denominator == 0:
        return default
    result = numerator / denominator
    return result if math.isfinite(result) else default


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """Percentile rank of every value within ``values``, as 0-100 (ties averaged).

    The rank-based definition, not ``scipy.stats.rankdata``'s zero-based one: a value that
    beats every other observation scores 100, the worst scores 0, and a tie is shared.

    >>> percentile_ranks([1.0, 2.0, 3.0])
    [0.0, 50.0, 100.0]
    >>> percentile_ranks([5.0])
    [50.0]
    """
    count = len(values)
    if count == 0:
        return []
    if count == 1:
        return [SINGLETON_PERCENTILE]

    ordered = sorted(range(count), key=lambda index: values[index])
    ranks = [0.0] * count
    position = 0
    while position < count:
        end = position
        while end + 1 < count and values[ordered[end + 1]] == values[ordered[position]]:
            end += 1
        # Average rank of the tied block, converted to the 0-100 scale.
        average_rank = (position + end) / 2
        score = 100.0 * average_rank / (count - 1)
        for index in range(position, end + 1):
            ranks[ordered[index]] = score
        position = end + 1
    return ranks


def log_compress(value: float, scale: float) -> float:
    """Map a heavy-tailed count onto 0-1 with a saturating log curve.

    ``scale`` is the size that counts as *large*: a value twice the scale reaches 1.0, and
    growth beyond that changes nothing. Log saturation rather than min-max because a single
    outlier should not rescale every other observation — which is exactly what a plain
    min-max does to a distribution with a 10,000-view day in it.
    """
    if value <= 0:
        return 0.0
    if scale <= 0:
        return 0.0
    return clamp(math.log1p(value) / math.log1p(2 * scale), 0.0, 1.0)


def ewma(values: Sequence[float], alpha: float = 0.3) -> float:
    """Exponentially weighted moving average of the whole series (last value wins most).

    Returns 0.0 for an empty series. ``alpha`` is the weight of the newest point.
    """
    if not values:
        return 0.0
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    level = values[0]
    for value in values[1:]:
        level = alpha * value + (1 - alpha) * level
    return level


def ewma_series(values: Sequence[float], alpha: float = 0.3) -> list[float]:
    """The EWMA after each observation, for charting or for a slope over the smoothed tail."""
    if not values:
        return []
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def zscore(value: float, population: Sequence[float]) -> float:
    """Standard score against ``population``; 0.0 when the population has no spread.

    No spread means no information, and returning 0 says that instead of dividing by zero
    or inventing a large number from floating-point noise.
    """
    spread = stddev(population) if len(population) > 1 else 0.0
    if spread == 0:
        return 0.0
    return (value - mean(population)) / spread


def ewma_z(
    series: Sequence[float],
    population: Sequence[float],
    *,
    alpha: float = 0.3,
) -> float:
    """EWMA of ``series``, z-scored against the ``population`` of comparable EWMA levels.

    This is the brief's "per-category EWMA z-score": the entity's smoothed current level,
    expressed in units of how much EWMA levels vary inside its category.
    """
    return zscore(ewma(series, alpha), population)


def normalized_slope(values: Sequence[float]) -> float:
    """Least-squares slope per step, divided by the series mean (scale-free).

    A slope of +0.5 means "growing by half of its own average level per step", which is
    comparable across an entity with 4 views a day and one with 40,000. Needs at least two
    points; otherwise the slope is 0.0 because a single observation has no direction.
    """
    count = len(values)
    if count < _TWO:
        return 0.0
    xs = list(range(count))
    x_mean = mean([float(x) for x in xs])
    y_mean = mean(values)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values, strict=True))
    slope = numerator / denominator
    if y_mean == 0:
        return 0.0
    return slope / abs(y_mean)


def growth_ratio(recent: float, baseline: float, *, floor: float = 1.0) -> float:
    """``(recent - baseline) / max(baseline, floor)``, bounded to [-1, 9].

    The floor is what stops a single old mention from turning a quiet entity into a
    900% growth story: with ``baseline = 1``, going from 1 to 5 is +4.0 (a real signal),
    while ``baseline = 0`` would make the same numbers infinite.
    """
    denominator = max(baseline, floor)
    ratio = (recent - baseline) / denominator
    return clamp(ratio, -1.0, 9.0)


def daily_series(
    events: Iterable[tuple[datetime, float]],
    *,
    as_of: datetime,
    days: int,
) -> list[float]:
    """Sum ``events`` into a dense daily series of ``days`` values ending at ``as_of``.

    Dense, not sparse: a missing day is a real zero (nobody talked about it), and a sparse
    series would make the EWMA depend on how often the source happens to publish.
    """
    if days <= 0:
        return []
    start_day = (as_of - timedelta(days=days - 1)).date()
    buckets: dict[date, float] = {start_day + timedelta(days=offset): 0.0 for offset in range(days)}
    for timestamp, value in events:
        bucket = timestamp.date()
        if bucket in buckets:
            buckets[bucket] += value
    return [buckets[start_day + timedelta(days=offset)] for offset in range(days)]


def summarize(values: Sequence[float]) -> dict[str, float]:
    """Count, sum, mean, median, spread and extremes — the shape of a population."""
    if not values:
        return {"count": 0.0, "sum": 0.0, "mean": 0.0, "median": 0.0, "stddev": 0.0,
                "min": 0.0, "max": 0.0}
    return {
        "count": float(len(values)),
        "sum": sum(values),
        "mean": mean(values),
        "median": median(values),
        "stddev": stddev(values),
        "min": min(values),
        "max": max(values),
    }
