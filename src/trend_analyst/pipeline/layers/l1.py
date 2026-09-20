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
#: Documents a phrase must appear in to count as "shared" rather than one person's remark. Used for
#: the reported single-document share and as the secondary sort key in the prune — *not* as a hard
#: gate, because the measurement said otherwise: the first live night's best find ("chisel storage")
#: came from exactly one document, and a two-document gate would have deleted it.
MIN_DOCUMENTS_FOR_NEWS: Final = 2
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
#: thing someone could buy or want. The indefinites and common verbs near the end were added in P7
#: after a live run mined "anybody saw" (modifier: "anybody", which was missing here; head: "saw",
#: which is a product and therefore must NOT be in this list).
#:
#: **This is a word list, not prose the parser can ignore.** The first version of the P7 edit put an
#: explanatory comment *inside* the string below, and `.split()` turned "saw", `"saw"` and "saw,"
#: into stop words — silently deleting the saw products from mining. `test_stop_words_are_words_and_
#: never_a_product_noun` now enforces both halves of that lesson.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    # A single long word list is more readable than a 130-line tuple literal.
    """
    a about after again against all also am an and any are around as at back be because been
    before being below best better between both but by cannot could did do does doing done
    down during each even ever every few for from further get got had has have having he her
    here hers him his how however i if in into is it its itself just like made make many may me
    might more most much must my no nor not now of off on once one only or other our out over own
    same she should since so some such than that the their them then there these they this those
    through to too under until up upon us use used using very was we were what when where which
    while who why will with would yet you your yours
    actually basically still means mean important thing things stuff really quite pretty
    even far due let lets making takes take comes come goes go knows know thats thats
    anybody anyone somebody someone everybody everyone nobody none nothing anything something
    everything wants wanted needs needed went
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

#: Documents are skipped when they contain at least this many NON-English marker words. Two, not
#: one, because a single hit is often a proper noun, an abbreviation or a loanword inside an
#: English sentence; two hits in a short post is a language.
NON_ENGLISH_MARKERS_MIN: Final = 2

#: Function words from the languages that actually appear in the pain-mining subreddits — Turkish
#: (r/3Dprinting, r/smallbusiness), German, Spanish, Portuguese, French, Italian — plus Swedish and
#: Dutch, which are cheap to include and rare enough not to hurt. NOT a language detector: it is a
#: *document* gate whose named failure mode is a short foreign post with no marker words in it.
#:
#: Why it exists at all: a live night mined "filament nerisi" and "filament nermeniz" (Turkish for
#: "filament recommendation") and spent two of ten candidate slots on them. The Judge dropped both —
#: correctly, and after the scoring and a provider call. English is the only language this
#: pipeline's taxonomy and prompts are written for, so a foreign document is out of scope, not bad
#: data.
NON_ENGLISH_MARKERS: Final[frozenset[str]] = frozenset(
    """
    ve bir icin bu ile da de cok ama bana benim nasil hangi nerisi oneri onerisi var yok
    mein meine meinen ich nicht und sehr auch nur oder aber mit von für auf ist sind war
    para con una não muito pero como porque también todo nada
    avec pour mais dans sur très je tu il elle nous vous est sont
    con per molto anche dove quando perché questo questa
    och att det inte med för är som men
    een het niet van voor met ook maar deze
    """.split()  # noqa: SIM905
)


def _is_foreign(text: str) -> bool:
    """True when a document is written in a language this pipeline is not built for.

    Called on the *document*, not the phrase: a Turkish sentence almost always carries a Turkish
    function word even when the product noun inside it is an English loanword ("filament nerisi").
    """
    tokens = set(tokens_of(text))
    return len(tokens & NON_ENGLISH_MARKERS) >= NON_ENGLISH_MARKERS_MIN


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
    #: Distinct documents the phrase appeared in. The single-document share is the honest measure of
    #: how much of a night's output rests on one post. Defaulted last so every existing construction
    #: site keeps working while the callers that need it pass it explicitly.
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
    """What mining did, in numbers, so the prune and the drops are visible in the ledger."""

    texts_scanned: int = 0
    ngrams: int = 0
    filtered: int = 0
    unmatched: int = 0
    foreign_texts: int = 0
    stale: int = 0
    mined: int = 0
    collapsed: int = 0
    pruned: int = 0
    kept: int = 0
    prune_fraction: float = DEFAULT_PRUNE_FRACTION
    min_keep: int = DEFAULT_MIN_KEEP
    #: Why phrases had no category, by reason ("no_head", "head_not_buyable", "brand", ...).
    #: The first live nights could only report "unmatched" for 17,000 phrases, which cannot tell a
    #: small taxonomy from fragmentary mining.
    rejections: dict[str, int] = field(default_factory=dict)
    #: Phrases that appeared in a single document. Reported, not rejected: with a small lake the
    #: candidate floor (min_keep) makes a hard >=2-document gate throw away findable products —
    #: measured, not assumed: the one-document phrase "chisel storage" was the first night's best
    #: find, and a two-document gate would have deleted it.
    single_document: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    phrases: tuple[MinedPhrase, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "texts_scanned": self.texts_scanned,
            "foreign_texts": self.foreign_texts,
            "ngrams": self.ngrams,
            "filtered": self.filtered,
            "unmatched": self.unmatched,
            "rejections": dict(sorted(self.rejections.items(), key=lambda item: -item[1])),
            "stale": self.stale,
            "single_document": self.single_document,
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
        reasons = ", ".join(
            f"{reason} {count}" for reason, count in list(self.rejections.items())[:4]
        )
        return (
            f"L1: {self.texts_scanned} texts ({self.foreign_texts} non-English skipped) -> "
            f"{self.ngrams} n-grams -> "
            f"{self.mined} candidates (rejected {self.unmatched}: {reasons}; "
            f"stale {self.stale}, collapsed {self.collapsed}), pruned {self.pruned}, "
            f"kept {self.kept} ({self.single_document} from a single document)"
        )


def _is_numeric_leading(tokens: Sequence[str]) -> bool:
    """True when a phrase starts with a bare number: "16 bolt" is a measurement, not a product.

    "3d printer" survives because "3d" is not purely numeric; "5/16 bolt" becomes "16 bolt" in the
    tokenizer and is exactly the fragment this rejects (the Judge's words: "a fragmented
    misinterpretation of a '5/16 bolt'").
    """
    return bool(tokens) and tokens[0].isdigit()


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


def _scan_text(
    text: str,
    *,
    timestamp: datetime,
    source_id: str,
    taxonomy: Taxonomy,
    report: MiningReport,
    occurrences: dict[str, list[tuple[datetime, str, str]]],
) -> None:
    """Extract one document's phrases, categorise them, and record why each one was dropped."""
    for phrase in extract_phrases(text):
        report.ngrams += 1
        tokens = tuple(phrase.split())
        if _is_numeric_leading(tokens):
            report.unmatched += 1
            key = "numeric_leading"
            report.rejections[key] = report.rejections.get(key, 0) + 1
            continue
        category_id, reason = taxonomy.match_with_reason(phrase, stop_words=STOP_WORDS)
        if category_id is None:
            report.unmatched += 1
            report.rejections[reason] = report.rejections.get(reason, 0) + 1
            continue
        occurrences.setdefault(f"{category_id}{phrase}", []).append(
            (timestamp, source_id, text)
        )


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
        if _is_foreign(text):
            # Out of scope, not bad data: counted separately from "rejected for its shape".
            report.foreign_texts += 1
            continue
        report.texts_scanned += 1
        _scan_text(
            text,
            timestamp=timestamp,
            source_id=source_id,
            taxonomy=taxonomy,
            report=report,
            occurrences=occurrences,
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
                documents=len({hit[2] for hit in hits}),
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
        everything,
        key=lambda mined: (
            # Wider-shared phrases first, then velocity, then volume. A phrase two people wrote
            # independently is a stronger signal than a phrase one person wrote twice, and with a
            # small lake the velocity percentiles are too coarse to separate them.
            -int(mined.documents >= MIN_DOCUMENTS_FOR_NEWS),
            -mined.velocity_percentile,
            -mined.mentions,
            mined.phrase,
        ),
    )
    kept = [replace(mined, kept=True) for mined in ordered[:target]]
    report.pruned = report.mined - target
    report.kept = target
    # Counted on what was actually kept, not on everything that survived filtering: the number next
    # to it in the ledger line says how much of tonight's output rests on a single post.
    report.single_document = sum(1 for mined in kept if mined.documents < MIN_DOCUMENTS_FOR_NEWS)
    # Kept phrases come back in velocity order: the scorer may re-sort, the ledger note will
    # not, and "what did we keep" is easier to trust when the order means something.
    report.phrases = tuple(kept)
    return report
