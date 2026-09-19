"""The category taxonomy: load `config/categories.yaml`, and match a phrase to a category.

MGS sub-scores are *per-category percentiles* (spec §6.1), so this module answers one
question well: given a phrase, which single category does it belong to — or none?

Matching rules, in order:

1. normalize the phrase (lowercase, punctuation to spaces, collapse whitespace);
2. count word-boundary keyword hits per category (a multi-word keyword must appear whole);
3. the category with the most hits wins; ties go to the category earlier in the file, which
   is why the file order is meaningful and stable;
4. a phrase whose text contains a stop phrase (weekly threads, megathreads, meta posts) is
   rejected outright — those are navigation, not demand;
5. no hits at all means **no category**, and the candidate is dropped rather than filed
   under a guess. The dropped share is reported by `coverage()` so the taxonomy's reach is
   measured, not assumed.
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

    def match(self, phrase: str) -> str | None:
        """The single category a phrase belongs to, or None when nothing matches.

        A phrase must contain at least one of a category's **core** nouns — the things people
        actually buy — before that category is even a candidate. Context words only break
        ties. This is not decoration: the first mining run filed "Cat Jarman" (a
        Wikipedia biography) under pets and "children killing" (a headline) under baby
        products, because "cat" and "children" were keywords. Requiring a buyable noun is
        the difference between a taxonomy and a word list.
        """
        normalized = normalize_phrase(phrase)
        if not normalized or any(stop in normalized for stop in self.stop_phrases):
            return None

        best_id: str | None = None
        best_score = (0, 0)
        for category in self.categories:
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
        """
        matched: dict[str, int] = {}
        total = 0
        unmatched = 0
        for phrase in phrases:
            total += 1
            category_id = self.match(phrase)
            if category_id is None:
                unmatched += 1
            else:
                matched[category_id] = matched.get(category_id, 0) + 1
        return {
            "phrases": total,
            "matched": total - unmatched,
            "unmatched": unmatched,
            "matched_pct": round(100.0 * (total - unmatched) / total, 1) if total else 0.0,
            "by_category": dict(sorted(matched.items(), key=lambda item: -item[1])),
        }


def normalize_phrase(phrase: str) -> str:
    """Lowercase, punctuation to spaces, collapse whitespace — the matching space."""
    lowered = _WORD_SPLIT.sub(" ", phrase.lower())
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
        feasibility_penalties=tuple(raw.get("feasibility_penalties") or ()),
        stop_phrases=tuple(str(phrase).lower() for phrase in (raw.get("stop_phrases") or ())),
    )


@lru_cache(maxsize=4)
def default_taxonomy(config_dir: Path | str | None = None) -> Taxonomy:
    """The repository taxonomy, cached: it is read once per process, never per phrase."""
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    return load_taxonomy(directory / "categories.yaml")
