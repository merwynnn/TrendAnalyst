"""The category taxonomy: load `config/categories.yaml`, and match a phrase to a category.

MGS sub-scores are *per-category percentiles* (spec §6.1), so this module answers one
question well: given a phrase, which single category does it belong to — or none?

Matching rules, in order (rewritten in P7 after the first live nights):

1. normalize the phrase (lowercase, punctuation to spaces, collapse whitespace);
2. a phrase containing a stop phrase (weekly threads, megathreads) or a **blocked brand** is
   rejected outright — navigation and licensed characters are not product gaps;
3. find the **head**: the last word that is not a function word ("chisel storage" -> "storage",
   "filament whatever" -> "whatever");
4. the head must be a **core** noun of some category (that category wins outright) or a shared
   **product-form** noun from `product_nouns` ("storage", "holder", "mount"), in which case the
   category comes from the *modifiers*: "chisel storage" -> tools_diy, "monitor mount" ->
   home_office. A head that is neither is **no category**, and the caller drops the candidate;
5. ties go to the category earlier in the file, which is why the file order is meaningful.

**Why the head rule exists (P7, from live data).** Requiring a core noun *anywhere* in the phrase
let "<core noun> <junk>" through: with "filament" in tools_diy's list, the first live nights
produced "filament whatever", "filament thanks", "mouse but i m", "bolt which vendors" and "time
to pick" — fragments that consumed scoring, judging and token budget, and that the Judge then
dropped as noise ("Conversational closing remark, not a product gap", "Garbled phrase from a
gardening query, miscategorized under music audio"). The head rule rejects that class at the
taxonomy, and `product_nouns` keeps the phrases it must not reject: a product phrase's head is the
thing you buy, and for accessories that noun is a form, not a domain word.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from config.settings import default_config_dir

__all__ = [
    "Category",
    "Taxonomy",
    "TaxonomyError",
    "load_taxonomy",
    "normalize_phrase",
    "tokens_of",
]

_WORD_SPLIT: Final = re.compile(r"[^a-z0-9]+")
#: Apostrophes and their typographic cousins: removed rather than turned into spaces, so "don't"
#: normalizes to "dont" and "I've" to "ive". Turning them into spaces produced junk tokens
#: ("don", "t", "i", "ve") that leaked into phrases, into the stop-word logic and — see
#: `l1.NON_ENGLISH_MARKERS` — into a language gate as a false "Turkish" hit from "I've".
_APOSTROPHE: Final = re.compile(r"['\u2019\u02bc\u2032]")
_WHITESPACE: Final = re.compile(r"\s+")


class TaxonomyError(RuntimeError):
    """The taxonomy file is missing or unusable. Always fatal: everything is per-category."""


class Category(BaseModel):
    """One product domain, with the priors the scorer uses before real data exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    keywords: tuple[str, ...]
    #: The buyable nouns of the domain. A phrase must contain at least one to be filed here.
    core: tuple[str, ...]
    #: Typical retail price in USD: the Money sub-score's prior (spec §6.1).
    price_band: tuple[float, float]
    #: How shippable a product in this domain is for a solo operator, 0-100.
    feasibility_prior: float

    @field_validator("keywords", "core")
    @classmethod
    def _keywords_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("an empty list can never match anything")
        return tuple(keyword.lower() for keyword in value)

    @field_validator("price_band")
    @classmethod
    def _band_ordered(cls, value: tuple[float, float]) -> tuple[float, float]:
        low, high = value
        if not 0 < low <= high:
            raise ValueError(f"price_band must be an ordered positive pair, got {value}")
        return value

    @property
    def midpoint_price(self) -> float:
        return (self.price_band[0] + self.price_band[1]) / 2


class Taxonomy(BaseModel):
    """Every category, plus the shared penalty and stop lists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    categories: tuple[Category, ...]
    #: Shared product-form nouns ("storage", "holder", "mount"): a phrase whose head is one of
    #: these is filed under whatever category its modifiers point at.
    product_nouns: tuple[str, ...] = ()
    #: Brands and licensed characters. A phrase naming one is not a gap: the market is closed to a
    #: solo operator, and the Judge should not be the first thing that notices.
    brand_blocklist: tuple[str, ...] = ()
    feasibility_penalties: tuple[str, ...] = ()
    stop_phrases: tuple[str, ...] = ()

    def by_id(self, category_id: str) -> Category:
        for category in self.categories:
            if category.id == category_id:
                return category
        raise TaxonomyError(f"unknown category {category_id!r}")

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(category.id for category in self.categories)

    @property
    def core_heads(self) -> frozenset[str]:
        """The head words of every core noun, across categories ("monitor stand" -> "stand")."""
        return frozenset(noun.split()[-1] for category in self.categories for noun in category.core)

    @property
    def form_nouns(self) -> frozenset[str]:
        """The shared product-form nouns, as a set for O(1) lookup."""
        return frozenset(self.product_nouns)

    def head_of(self, phrase: str, *, stop_words: frozenset[str] = frozenset()) -> str | None:
        """The phrase's head word: the last word that is not a trailing function word.

        `stop_words` is passed in rather than imported because the mining layer owns that list and
        this module must stay usable without it: the two agree on the *rule*, and one of them owns
        the *words*.
        """
        words = list(tokens_of(phrase))
        while words and words[-1] in stop_words:
            words.pop()
        return words[-1] if words else None

    def match(self, phrase: str, *, stop_words: frozenset[str] = frozenset()) -> str | None:
        """The single category a phrase belongs to, or None when nothing matches.

        See :meth:`match_with_reason` for the rules; this is the two-word version callers use.
        """
        return self.match_with_reason(phrase, stop_words=stop_words)[0]

    def match_with_reason(
        self, phrase: str, *, stop_words: frozenset[str] = frozenset()
    ) -> tuple[str | None, str]:
        """The category, and *why* a phrase was rejected when it has none.

        The reason matters more than it looks: the first live nights produced 17,000 unmatched
        n-grams per run and the run note could only say "unmatched". Naming the reasons turns the
        ledger line into a measurement — "15,402 phrases had no buyable head noun, 1,190 had a
        function word before the head, 12 named a blocked brand" is a sentence a human can act on.

        Returns:
            ``(category_id | None, reason)`` where reason is one of ``matched``, ``empty``,
            ``stop_phrase``, ``brand``, ``no_head``, ``head_not_buyable``,
            ``function_word_before_head``, ``no_modifier``.
        """
        normalized = normalize_phrase(phrase)
        rejection = self._rejection(normalized, stop_words)
        if rejection is not None:
            return None, rejection
        words = list(tokens_of(normalized))
        while words and words[-1] in stop_words:
            words.pop()
        head = words[-1]

        # 1. The head is a category's own core noun: unambiguous, and the common case.
        head_matches = [
            category
            for category in self.categories
            if any(
                noun.split()[-1] == head and _contains(normalized, noun)
                for noun in category.core
            )
        ]
        if head_matches:
            if len(head_matches) == 1:
                return head_matches[0].id, "matched"
            # Two domains claim the same head ("board" is a cutting board and a circuit board).
            # The modifiers decide; the earlier category in the file breaks a remaining tie.
            best = self._best_by_modifiers(normalized, head_matches) or head_matches[0].id
            return best, "matched"

        # 2. The head is a product form: the category must come from the modifiers, and it does not
        #    have to. "chisel storage" is tools_diy; "storage" on its own is nobody's candidate.
        if head in self.form_nouns:
            matched = self._best_by_modifiers(normalized, self.categories)
            return (matched, "matched") if matched else (None, "no_modifier")
        return None, "head_not_buyable"

    def _rejection(self, normalized: str, stop_words: frozenset[str]) -> str | None:
        """Why a phrase cannot have a category at all, or None when it might.

        Split out so the reason chain is one readable list rather than nine early returns, and so
        every rejection carries a name: the mining layer counts them, and those counts are what
        turned "17,000 unmatched n-grams" into a diagnosis.
        """
        if not normalized:
            return "empty"
        if any(stop in normalized for stop in self.stop_phrases):
            return "stop_phrase"
        if any(_contains(normalized, brand) for brand in self.brand_blocklist):
            return "brand"

        words = list(tokens_of(normalized))
        while words and words[-1] in stop_words:
            words.pop()
        if not words:
            return "no_head"
        # A noun phrase's head is preceded by its modifier, never by a function word: "time to pick"
        # ends in a real noun (a guitar pick) and only the "to" before it marks the sentence
        # fragment it actually is. Live evidence, not theory: the Judge dropped that exact phrase
        # as "garbled phrase from a gardening query, miscategorized under music audio".
        if len(words) > 1 and words[-2] in stop_words:
            return "function_word_before_head"
        return None

    def _best_by_modifiers(self, normalized: str, candidates: Iterable[Category]) -> str | None:
        """The category whose core and context words are best represented in the phrase."""
        best_id: str | None = None
        best_score = (0, 0)
        for category in candidates:
            core_hits = sum(1 for noun in category.core if _contains(normalized, noun))
            if core_hits == 0:
                continue
            keyword_hits = sum(1 for word in category.keywords if _contains(normalized, word))
            score = (core_hits, keyword_hits)
            if score > best_score:
                best_id, best_score = category.id, score
        return best_id

    def penalty_for(self, phrase: str) -> tuple[str, ...]:
        """Which feasibility penalties a phrase trips (patents, hazmat, regulation...)."""
        normalized = normalize_phrase(phrase)
        return tuple(
            penalty
            for penalty in self.feasibility_penalties
            if _contains(normalized, penalty)
        )

    def coverage(self, phrases: Iterable[str]) -> dict[str, object]:
        """How many phrases matched, and how they spread across categories.

        Returned rather than logged, so a script or a test can assert on it: a taxonomy
        that matches almost nothing is a taxonomy that needs editing, and that should be
        visible in a number.

        Rejections are also broken down by reason (`rejections`), because "unmatched" alone cannot
        tell a taxonomy that is too small from a mining layer that is emitting sentence fragments.
        """
        matched: dict[str, int] = {}
        rejections: dict[str, int] = {}
        total = 0
        for phrase in phrases:
            total += 1
            category_id, reason = self.match_with_reason(phrase)
            if category_id is None:
                rejections[reason] = rejections.get(reason, 0) + 1
            else:
                matched[category_id] = matched.get(category_id, 0) + 1
        unmatched = sum(rejections.values())
        return {
            "phrases": total,
            "matched": total - unmatched,
            "unmatched": unmatched,
            "matched_pct": round(100.0 * (total - unmatched) / total, 1) if total else 0.0,
            "by_category": dict(sorted(matched.items(), key=lambda item: -item[1])),
            "rejections": dict(sorted(rejections.items(), key=lambda item: -item[1])),
        }


def normalize_phrase(phrase: str) -> str:
    """Lowercase, apostrophes removed, other punctuation to spaces, collapse whitespace.

    This is *the* matching space: the taxonomy, the mining layer and the language gate all tokenize
    through here, so a change here changes what a phrase is. Apostrophes are deleted rather than
    spaced out because "dont" is a word a search box accepts and "don t" is two tokens that made
    "don" look like content.
    """
    lowered = _WORD_SPLIT.sub(" ", _APOSTROPHE.sub("", phrase.lower()))
    return _WHITESPACE.sub(" ", lowered).strip()


def tokens_of(phrase: str) -> tuple[str, ...]:
    """The words of a phrase, in the same normalized space `match` uses."""
    normalized = normalize_phrase(phrase)
    return tuple(normalized.split()) if normalized else ()


def _contains(haystack: str, needle: str) -> bool:
    """Word-boundary containment: "pan" must not match "pants".

    Multi-word needles are matched as a phrase, still on word boundaries, so "standing
    desk" does not match "outstanding desk".
    """
    pattern = r"(?<![a-z0-9])" + re.escape(needle.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def load_taxonomy(path: Path | str) -> Taxonomy:
    """Load and validate the taxonomy.

    Raises:
        TaxonomyError: the file is missing, unparseable, or has no categories.
    """
    taxonomy_path = Path(path)
    if not taxonomy_path.is_file():
        raise TaxonomyError(f"taxonomy not found: {taxonomy_path}")

    try:
        raw = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"cannot parse {taxonomy_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaxonomyError(f"{taxonomy_path}: expected a mapping at the top level")

    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, dict) or not raw_categories:
        raise TaxonomyError(f"{taxonomy_path}: 'categories' must be a non-empty mapping")

    categories: list[Category] = []
    for category_id, body in raw_categories.items():
        if not isinstance(body, dict):
            raise TaxonomyError(f"{taxonomy_path}: category {category_id!r} must be a mapping")
        try:
            categories.append(Category(id=str(category_id), **body))
        except Exception as exc:  # pydantic ValidationError and friends
            raise TaxonomyError(
                f"{taxonomy_path}: invalid category {category_id!r}: {exc}"
            ) from exc

    return Taxonomy(
        version=int(raw.get("version", 1)),
        categories=tuple(categories),
        product_nouns=tuple(str(noun).lower() for noun in (raw.get("product_nouns") or ())),
        brand_blocklist=tuple(str(brand).lower() for brand in (raw.get("brand_blocklist") or ())),
        feasibility_penalties=tuple(raw.get("feasibility_penalties") or ()),
        stop_phrases=tuple(str(phrase).lower() for phrase in (raw.get("stop_phrases") or ())),
    )


@lru_cache(maxsize=4)
def default_taxonomy(config_dir: Path | str | None = None) -> Taxonomy:
    """The repository taxonomy, cached: it is read once per process, never per phrase."""
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    return load_taxonomy(directory / "categories.yaml")
