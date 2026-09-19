"""L1 — mine phrases from the lake, keep the risers, drop the rest (spec §4.1, brief P2).

The layer does four things, in this order:

1. **Extract** n-grams (2-4 words) from the text the sources actually collected — post
   titles, story titles, article names. Not from a keyword list: the phrases have to come
   from what people wrote.
2. **Filter** them: stop words at either end, stop phrases (weekly threads, megathreads,
   meta posts), pure numbers, and anything that does not match a category. A phrase with no
   category has no per-category percentile, so it cannot be scored — and it is dropped
   loudly, as a count, rather than filed under a guess.
3. **Score velocity** per category as an EWMA z-score: the smoothed current level of the
   phrase's daily mentions, expressed in units of how much EWMA levels vary among its
   category peers. The brief asks for exactly this, and it is the right statistic here —
   absolute volume finds incumbents, velocity finds what is starting.
4. **Prune 95%**: keep the top 5% by velocity.

Two honest notes about the prune and the staleness filter, both of which are deviations
from a literal reading of the brief in favour of the same result:

* The prune keeps a **floor** of candidates (``min_keep``) even when 5% of the mined set is
  smaller. With three of fifteen sources collected, 5% of a real mining run is one or two
  phrases, and a pipeline that returns one candidate teaches nothing. Both numbers are
  returned (``mined``, ``pruned``, ``kept``) and written into the run's note, so the prune
  is visible rather than quietly disabled.
* A phrase nobody has mentioned in ``max_age_days`` is **not mined at all**. Staleness is
  not a shape a classifier should have to name (an earlier draft did, and the database's
  ``fad_label`` CHECK constraint rejected it) — it is a reason to leave something out of a
  discovery run. Candidates already in the table are left alone; this filters new mining,
  not history.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final

from config.categories import Taxonomy, tokens_of
from trend_analyst.scoring.normalize import daily_series, ewma, percentile_ranks, zscore

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MIN_KEEP",
    "DEFAULT_PRUNE_FRACTION",
    "MAX_NGRAM",
    "MIN_NGRAM",
    "STOP_WORDS",
    "MinedPhrase",
    "MiningReport",
    "extract_phrases",
    "mine",
]

#: Brief: "prune 95%" — keep the top 5%.
DEFAULT_PRUNE_FRACTION: Final = 0.05
#: The floor described in the module docstring.
DEFAULT_MIN_KEEP: Final = 10
#: Staleness horizon for *new* mining: a phrase last mentioned this long ago is not news.
DEFAULT_MAX_AGE_DAYS: Final = 90

#: Sources whose text is not minable. Wikipedia's entities are article titles — proper nouns
#: by construction, so "Cat Jarman" (a person) matches the word "cat" and "Clancy" matches
#: nothing anyone can buy. The source still earns its place in the pipeline: its pageviews are
#: an absolute demand baseline for entities other sources surface (`l3` reads them), which is
#: the opposite of a phrase source.
DEFAULT_EXCLUDED_SOURCES: Final[frozenset[str]] = frozenset({"wiki_pageviews"})

MIN_NGRAM: Final = 2
MAX_NGRAM: Final = 4

#: Words that cannot start or end a phrase. Not a linguist's stop list — a product
#: researcher's: these are the words that make a phrase a sentence fragment rather than a
#: thing someone could buy or want.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    # A single long word list is more readable than a 130-line tuple literal.
    """
    a about after again against all also am an and any are around as at back be because been
    before being below best better between both but by can cannot could did do does doing done
    down during each even ever every few for from further get got had has have having he her
    here hers him his how however i if in into is it its itself just like made make many may me
    might more most much must my no nor not now of off on once one only or other our out over own
    same she should since so some such than that the their them then there these they this those
    through to too under until up upon us use used using very was we were what when where which
    while who why will with would yet you your yours
    actually basically still means mean important thing things stuff really quite pretty
    even far due let lets making takes take comes come goes go knows know thats thats
    """.split()  # noqa: SIM905 - a literal list of this length reads worse than the string
)

#: Noise specific to the sources in the registry: thread furniture and subreddit boilerplate.
NOISE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(weekly|daily|monthly|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b(thread|megathread|sticky|mod post|announcement|psa|ama|tldr|edit|update)\b",
        r"\b(reddit|subreddit|r/\w+|u/\w+|op|mods?)\b",
        r"\b(upvote|downvote|karma|repost|x-?post)\b",
        r"\b(http|https|www|com|html|amp)\b",
        r"\d{4,}",
    )
)

_WORD_SPLIT: Final = re.compile(r"\s+")

#: Field separators inside a signal's text. `l3.load_attention_points` joins an entity to its
#: quote with " | ", and n-grams must not cross that join: the first mining run produced
#: "cat jarman cat jarman" from a joined field, which is not a phrase anybody said.
_SEGMENT_SPLIT: Final = re.compile(r"\s*[|•]\s*|\s+--\s+")


@dataclass(frozen=True, slots=True)
class MinedPhrase:
    """A phrase that survived filtering, with the evidence behind its velocity."""

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
    #: Populated by `mine()` for everything that survived the prune.
    kept: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "phrase": self.phrase,
            "category": self.category_id,
            "mentions": self.mentions,
            "sources": list(self.source_ids),
            "ewma_z": round(self.ewma_zscore, 4),
            "velocity_percentile": round(self.velocity_percentile, 2),
            "kept": self.kept,
        }


@dataclass(slots=True)
class MiningReport:
    """What mining did, in numbers, so the prune and the drops are visible in the ledger."""

    texts_scanned: int = 0
    ngrams: int = 0
    filtered: int = 0
    unmatched: int = 0
    stale: int = 0
    mined: int = 0
    collapsed: int = 0
    pruned: int = 0
    kept: int = 0
    prune_fraction: float = DEFAULT_PRUNE_FRACTION
    min_keep: int = DEFAULT_MIN_KEEP
    by_category: dict[str, int] = field(default_factory=dict)
    phrases: tuple[MinedPhrase, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "texts_scanned": self.texts_scanned,
            "ngrams": self.ngrams,
            "filtered": self.filtered,
            "unmatched": self.unmatched,
            "stale": self.stale,
            "mined": self.mined,
            "collapsed": self.collapsed,
            "pruned": self.pruned,
            "kept": self.kept,
            "prune_fraction": self.prune_fraction,
            "min_keep": self.min_keep,
            "by_category": dict(sorted(self.by_category.items())),
        }

    def summary(self) -> str:
        """One line for the run's ledger note."""
        return (
            f"L1: {self.texts_scanned} texts -> {self.ngrams} n-grams -> "
            f"{self.mined} candidates (unmatched {self.unmatched}, stale {self.stale}, "
            f"collapsed {self.collapsed}), pruned {self.pruned}, kept {self.kept}"
        )


def _is_noise(phrase: str) -> bool:
    """True when a phrase is thread furniture, markup or a number rather than a thing."""
    return any(pattern.search(phrase) for pattern in NOISE_PATTERNS)


def _trim_stopwords(tokens: Sequence[str]) -> tuple[str, ...]:
    """Drop leading and trailing stop words; interior ones stay ("mat for standing desk")."""
    start = 0
    end = len(tokens)
    while start < end and tokens[start] in STOP_WORDS:
        start += 1
    while end > start and tokens[end - 1] in STOP_WORDS:
        end -= 1
    return tuple(tokens[start:end])


def extract_phrases(text: str, *, min_n: int = MIN_NGRAM, max_n: int = MAX_NGRAM) -> list[str]:
    """Every candidate n-gram in one text, normalized, filtered and de-duplicated.

    >>> extract_phrases("My standing desk mat is cracked and flimsy")
    ['standing desk', 'desk mat', 'standing desk mat', 'mat is cracked', ...]
    Interior stop words are kept ("mat is cracked"), because trimming them changes meaning;
    they rarely survive the category match, which is the filter that matters.
    """
    seen: set[str] = set()
    out: list[str] = []
    for segment in _SEGMENT_SPLIT.split(text):
        for phrase in _phrases_in_segment(segment, min_n=min_n, max_n=max_n):
            if phrase not in seen:
                seen.add(phrase)
                out.append(phrase)
    return out


def _phrases_in_segment(text: str, *, min_n: int, max_n: int) -> list[str]:
    """The n-grams of one field, with stop words trimmed off both ends."""
    tokens = tokens_of(text)
    if len(tokens) < min_n:
        return []

    out: list[str] = []
    for size in range(min_n, max_n + 1):
        for start in range(len(tokens) - size + 1):
            window = _trim_stopwords(tokens[start : start + size])
            # Trimming can shorten a window below the minimum: "mat is cracked" -> "mat
            # cracked" is a bigram, but "the desk" -> "desk" is not a phrase at all.
            if len(window) < min_n:
                continue
            phrase = " ".join(window)
            if _is_noise(phrase):
                continue
            # A phrase must not be one repeated token ("desk desk").
            if len(set(window)) == 1:
                continue
            out.append(phrase)
    return out


def collapse_substrings(phrases: Sequence[MinedPhrase]) -> tuple[list[MinedPhrase], int]:
    """Drop phrases that are noisy extensions of a more frequent shorter phrase.

    Overlapping n-grams are the same title seen three ways: "circ saw", "blade circ saw",
    "circ saw it still". The specific one is not more informative — it is the same evidence
    with an extra word attached, and it splits that evidence across three rows of the ranked
    table. So a phrase is dropped when a *kept* phrase is a substring of it and has at least
    as many mentions; the shorter, more frequent phrase is the one worth ranking.

    Returns the survivors and how many were collapsed, because a silent drop is the kind of
    thing that later looks like a mining bug.
    """
    survivors: list[MinedPhrase] = []
    collapsed = 0
    for candidate in sorted(
        phrases, key=lambda item: (-item.mentions, len(item.phrase), item.phrase)
    ):
        if any(
            kept.phrase in candidate.phrase and kept.mentions >= candidate.mentions
            for kept in survivors
        ):
            collapsed += 1
            continue
        survivors.append(candidate)
    return survivors, collapsed


def mine(
    texts: Iterable[tuple[str, datetime, str]],
    *,
    taxonomy: Taxonomy,
    as_of: datetime,
    window_days: int = 90,
    prune_fraction: float = DEFAULT_PRUNE_FRACTION,
    min_keep: int = DEFAULT_MIN_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    excluded_sources: frozenset[str] = DEFAULT_EXCLUDED_SOURCES,
) -> MiningReport:
    """Mine phrases from ``(text, timestamp, source_id)`` triples.

    Args:
        texts: the lake's text-bearing signals. The caller decides which sources count as
            text (see `pipeline/orchestrator.py`): a pageview has an entity name but no
            sentence to mine.
        taxonomy: decides each phrase's category; an unmatched phrase is dropped.
        as_of: reference instant for the velocity series. Passed in, never read from the
            clock, so a replay reproduces the same prune.
        window_days: how far back the velocity series looks.
        prune_fraction: the share to keep, by velocity percentile (brief: 0.05).
        min_keep: the floor that keeps a small lake useful, reported in the run's note.
        max_age_days: staleness horizon for new mining.
        excluded_sources: sources whose text is not minable (see `DEFAULT_EXCLUDED_SOURCES`).
    """
    report = MiningReport(prune_fraction=prune_fraction, min_keep=min_keep)
    occurrences: dict[str, list[tuple[datetime, str, str]]] = {}
    cutoff = as_of - timedelta(days=min(max_age_days, window_days))

    for text, timestamp, source_id in texts:
        if not text or source_id in excluded_sources:
            continue
        report.texts_scanned += 1
        for phrase in extract_phrases(text):
            report.ngrams += 1
            category_id = taxonomy.match(phrase)
            if category_id is None:
                report.unmatched += 1
                continue
            occurrences.setdefault(f"{category_id}\x1f{phrase}", []).append(
                (timestamp, source_id, text)
            )

    # Group by category: velocity is a per-category statistic, always.
    by_category: dict[str, list[MinedPhrase]] = {}
    for key, hits in occurrences.items():
        category_id, phrase = key.split("\x1f", 1)
        last_seen = max(hit[0] for hit in hits)
        if last_seen < cutoff:
            report.stale += 1
            continue
        by_category.setdefault(category_id, []).append(
            MinedPhrase(
                phrase=phrase,
                category_id=category_id,
                mentions=len(hits),
                source_ids=tuple(sorted({hit[1] for hit in hits})),
                first_seen=min(hit[0] for hit in hits),
                last_seen=last_seen,
                texts=tuple(dict.fromkeys(hit[2] for hit in hits))[:5],
            )
        )

    kept: list[MinedPhrase] = []
    for category_id, phrases in by_category.items():
        # The brief's per-category EWMA z-score, computed exactly as mgs.py computes it: build
        # each phrase's daily mention series, take its EWMA (the smoothed current level), then
        # express that level in units of how much EWMA levels vary inside the category.
        series_by_phrase: dict[str, list[float]] = {}
        levels: list[float] = []
        for mined in phrases:
            hits = occurrences[f"{category_id}\x1f{mined.phrase}"]
            series = daily_series(
                ((timestamp, 1.0) for timestamp, _, _ in hits), as_of=as_of, days=window_days
            )
            series_by_phrase[mined.phrase] = series
            levels.append(ewma(series))
        zscores = [zscore(level, levels) for level in levels]
        percentiles = percentile_ranks(zscores)
        by_category[category_id] = [
            replace(
                mined,
                ewma_zscore=zscore_value,
                velocity_percentile=percentile,
                series=tuple(series_by_phrase[mined.phrase]),
            )
            for mined, zscore_value, percentile in zip(phrases, zscores, percentiles, strict=True)
        ]

    everything = [mined for phrases in by_category.values() for mined in phrases]
    everything, report.collapsed = collapse_substrings(everything)
    report.mined = len(everything)
    report.by_category = {
        category_id: sum(1 for mined in everything if mined.category_id == category_id)
        for category_id in by_category
    }

    # The prune: top 5% by velocity percentile, with the documented floor. Ties break on
    # mention count and then on the phrase itself, so two runs over the same lake keep the
    # same candidates — a prune that reshuffles on every run makes its own snapshots
    # incomparable.
    target = min(max(math.ceil(prune_fraction * report.mined), min_keep), report.mined)
    ordered = sorted(
        everything, key=lambda mined: (-mined.velocity_percentile, -mined.mentions, mined.phrase)
    )
    kept = [replace(mined, kept=True) for mined in ordered[:target]]
    report.pruned = report.mined - target
    report.kept = target
    # Kept phrases come back in velocity order: the scorer may re-sort, the ledger note will
    # not, and "what did we keep" is easier to trust when the order means something.
    report.phrases = tuple(kept)
    return report
