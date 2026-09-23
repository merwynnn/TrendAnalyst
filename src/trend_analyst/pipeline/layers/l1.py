"""L1 — rank what the Extractor gate found, keep the risers, drop the rest.

The layer does three things, in this order:

1. **Resolve** each extracted product to its texts: the Extractor cites chunk-local ids,
   and ``points_by_id`` maps them back to the lake points they came from. A product with
   no surviving points is dropped and counted — the same class of inconsistency the old
   miner counted as ``skipped_no_evidence``.
2. **Score velocity** per category as an EWMA z-score: the smoothed current level of the
   product's daily mentions, expressed in units of how much EWMA levels vary among its
   category peers. Absolute volume finds incumbents, velocity finds what is starting.
3. **Prune 95%**: keep the top 5% by velocity, with the floor below.

What this layer deliberately does *not* do is decide what a product is: the model reads
the texts, code counts and ranks. The deterministic miner this replaced (n-grams, stop
lists, head-noun rule) produced fragments more often than products; a word list cannot
keep up with language, so it was deleted rather than tuned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final

from trend_analyst.scoring.features import SignalPoint
from trend_analyst.scoring.normalize import daily_series, ewma, percentile_ranks, zscore

__all__ = [
    "DEFAULT_CHUNK_EXCLUDED_SOURCES",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MIN_KEEP",
    "DEFAULT_PRUNE_FRACTION",
    "MIN_DOCUMENTS_FOR_NEWS",
    "MinedPhrase",
    "MiningReport",
    "RankedProducts",
    "rank_products",
]

#: Brief: "prune 95%" — keep the top 5%.
DEFAULT_PRUNE_FRACTION: Final = 0.05
#: The floor that keeps a small lake useful, reported in the run's note.
DEFAULT_MIN_KEEP: Final = 10
#: Documents a product must appear in to count as "shared" rather than one person's remark.
#: Used for the reported single-document share and as the primary sort key in the prune —
#: *not* as a hard gate, because the measurement said otherwise: the first live night's best
#: find ("chisel storage") came from exactly one document, and a two-document gate would have
#: deleted it.
MIN_DOCUMENTS_FOR_NEWS: Final = 2
#: Staleness horizon: a product last mentioned this long ago is not news.
DEFAULT_MAX_AGE_DAYS: Final = 90

#: Sources whose text is not minable. Wikipedia's entities are article titles — proper nouns
#: by construction ("Cat Jarman" is a person). The source still earns its place: its pageviews
#: are an absolute demand baseline for entities other sources surface (`l3` reads them), which
#: is the opposite of an extraction input.
DEFAULT_CHUNK_EXCLUDED_SOURCES: Final[frozenset[str]] = frozenset({"wiki_pageviews"})


@dataclass(frozen=True, slots=True)
class MinedPhrase:
    """An extracted product that survived ranking, with the evidence behind its velocity."""

    phrase: str
    category_id: str
    mentions: int
    source_ids: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    texts: tuple[str, ...]
    #: EWMA z-score within the category, and its percentile (the prune's sort key).
    ewma_zscore: float = 0.0
    velocity_percentile: float = 0.0
    #: Daily mention series over the mining window, oldest first (for the scorer's features).
    series: tuple[float, ...] = ()
    #: Populated by `rank_products` for everything that survived the prune.
    kept: bool = False
    #: Distinct documents the product appeared in. The single-document share is the honest
    #: measure of how much of a night's output rests on one post.
    documents: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "phrase": self.phrase,
            "category": self.category_id,
            "mentions": self.mentions,
            "documents": self.documents,
            "sources": list(self.source_ids),
            "ewma_z": round(self.ewma_zscore, 4),
            "velocity_percentile": round(self.velocity_percentile, 2),
            "kept": self.kept,
        }


@dataclass(slots=True)
class MiningReport:
    """What L1 did, in numbers, so the prune and the drops are visible in the run note."""

    texts_scanned: int = 0
    duplicate_texts: int = 0
    chunks: int = 0
    attempted: int = 0
    calls: int = 0
    cached: int = 0
    failed_chunks: int = 0
    unknown_refs: int = 0
    empty_chunks: int = 0
    stale: int = 0
    mined: int = 0
    pruned: int = 0
    kept: int = 0
    prune_fraction: float = DEFAULT_PRUNE_FRACTION
    min_keep: int = DEFAULT_MIN_KEEP
    #: Products that appeared in a single document. Reported, not rejected (see
    #: MIN_DOCUMENTS_FOR_NEWS): with a small lake the candidate floor makes a hard
    #: >=2-document gate throw away findable products.
    single_document: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    phrases: tuple[MinedPhrase, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "texts_scanned": self.texts_scanned,
            "duplicate_texts": self.duplicate_texts,
            "chunks": self.chunks,
            "attempted": self.attempted,
            "calls": self.calls,
            "cached": self.cached,
            "failed_chunks": self.failed_chunks,
            "unknown_refs": self.unknown_refs,
            "empty_chunks": self.empty_chunks,
            "stale": self.stale,
            "mined": self.mined,
            "pruned": self.pruned,
            "kept": self.kept,
            "prune_fraction": self.prune_fraction,
            "min_keep": self.min_keep,
            "single_document": self.single_document,
            "by_category": dict(sorted(self.by_category.items())),
        }

    def summary(self) -> str:
        """One line for the run's note."""
        coverage = (
            f"{self.attempted}/{self.chunks} chunks attempted"
            if self.attempted != self.chunks
            else f"{self.chunks} chunks"
        )
        return (
            f"L1: {self.texts_scanned} texts ({self.duplicate_texts} duplicates skipped) "
            f"-> {coverage} "
            f"({self.calls} calls, {self.cached} cached, {self.failed_chunks} failed) -> "
            f"{self.mined} products ({self.unknown_refs} invented refs dropped, "
            f"stale {self.stale}), pruned {self.pruned}, "
            f"kept {self.kept} ({self.single_document} from a single document)"
        )


@dataclass(frozen=True, slots=True)
class RankedProducts:
    """The ranking outcome: kept phrases plus the evidence map scoring needs."""

    kept: tuple[MinedPhrase, ...]
    #: Phrase -> the lake points behind it, so scoring never re-matches text.
    match_points: dict[str, list[SignalPoint]]
    stale: int
    mined: int


def rank_products(
    resolved: Sequence[tuple[str, str, Sequence[int]]],
    points_by_id: Mapping[int, SignalPoint],
    *,
    as_of: datetime,
    window_days: int = 90,
    prune_fraction: float = DEFAULT_PRUNE_FRACTION,
    min_keep: int = DEFAULT_MIN_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> RankedProducts:
    """Rank extracted products by velocity and prune to the survivors.

    Args:
        resolved: ``(phrase, category_id, point_ids)`` triples — the Extractor's output
            after grounding, with ids into ``points_by_id``.
        points_by_id: the night's texts by their chunk-global id.
        as_of: reference instant for the velocity series. Passed in, never read from the
            clock, so a replay reproduces the same prune.
        window_days: how far back the velocity series looks.
        prune_fraction: the share to keep, by velocity percentile (brief: 0.05).
        min_keep: the floor that keeps a small lake useful, reported in the run's note.
        max_age_days: staleness horizon for ranking.
    """
    cutoff = as_of - timedelta(days=min(max_age_days, window_days))
    candidates: list[MinedPhrase] = []
    # This product's own points, keyed exactly: the velocity series is built from what the
    # Extractor cited — never re-matched from text, so ranking cannot disagree with it.
    own_points: dict[tuple[str, str], list[SignalPoint]] = {}
    # What the scorer reads, keyed by phrase: a phrase filed under two categories in one
    # night merges its evidence rather than splitting it.
    match_points: dict[str, list[SignalPoint]] = {}
    stale = 0
    for phrase, category_id, point_ids in resolved:
        if (category_id, phrase) in own_points:
            continue
        points = [points_by_id[point_id] for point_id in point_ids if point_id in points_by_id]
        if not points:
            # Extracted, then lost: every ref pointed outside the night's texts. Counted
            # rather than scored as zero — a silent zero would read as "no interest".
            continue
        last_seen = max(point.ts for point in points)
        if last_seen < cutoff:
            stale += 1
            continue
        own_points[(category_id, phrase)] = points
        match_points.setdefault(phrase, []).extend(points)
        candidates.append(
            MinedPhrase(
                phrase=phrase,
                category_id=category_id,
                mentions=len(points),
                source_ids=tuple(sorted({point.source_id for point in points})),
                first_seen=min(point.ts for point in points),
                last_seen=last_seen,
                texts=tuple(dict.fromkeys(point.text for point in points))[:5],
                documents=len(points),
            )
        )

    # Group by category: velocity is a per-category statistic, always.
    by_category: dict[str, list[MinedPhrase]] = {}
    for candidate in candidates:
        by_category.setdefault(candidate.category_id, []).append(candidate)
    for category_id, phrases in by_category.items():
        series_by_phrase: dict[str, list[float]] = {}
        levels: list[float] = []
        for mined in phrases:
            series = daily_series(
                ((point.ts, 1.0) for point in own_points[(category_id, mined.phrase)]),
                as_of=as_of,
                days=window_days,
            )
            series_by_phrase[mined.phrase] = series
            levels.append(ewma(series))
        zscores = [zscore(level, levels) for level in levels]
        percentiles = percentile_ranks(zscores)
        by_category[category_id] = [
            replace(mined, ewma_zscore=zscore_value, velocity_percentile=percentile,
                    series=tuple(series_by_phrase[mined.phrase]))
            for mined, zscore_value, percentile in zip(phrases, zscores, percentiles, strict=True)
        ]

    everything = [mined for phrases in by_category.values() for mined in phrases]
    # The prune: top fraction by velocity percentile, with the documented floor. Ties break
    # on shared documents, then mentions, then the phrase itself, so two runs over the same
    # lake keep the same candidates — a prune that reshuffles on every run makes its own
    # snapshots incomparable.
    target = min(max(math.ceil(prune_fraction * len(everything)), min_keep), len(everything))
    ordered = sorted(
        everything,
        key=lambda mined: (
            -int(mined.documents >= MIN_DOCUMENTS_FOR_NEWS),
            -mined.velocity_percentile,
            -mined.mentions,
            mined.phrase,
        ),
    )
    kept = [replace(mined, kept=True) for mined in ordered[:target]]
    return RankedProducts(
        kept=tuple(kept),
        match_points={candidate.phrase: match_points[candidate.phrase] for candidate in kept},
        stale=stale,
        mined=len(everything),
    )
