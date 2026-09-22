"""The category taxonomy: load `config/categories.yaml`, describe the scoring buckets.

MGS sub-scores are *per-category percentiles* (spec §6.1), so this module answers which
bucket a scored product belongs to — and what that bucket's priors are. It no longer
matches text: the Extractor gate files products itself, and the deterministic matcher
(n-grams, head-noun rule, brand blocklist) was deleted with it. A word list cannot keep
up with language; keeping one next to a model that does the job would only let the two
disagree.
"""

from __future__ import annotations

import re
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
    #: ("don", "t", "i", "ve") that leaked into phrases downstream.
_APOSTROPHE: Final = re.compile(r"['\u2019\u02bc\u2032]")
_WHITESPACE: Final = re.compile(r"\s+")


class TaxonomyError(RuntimeError):
    """The taxonomy file is missing or unusable. Always fatal: everything is per-category."""


class Category(BaseModel):
    """One product domain, with the priors the scorer uses before real data exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    #: Typical retail price in USD: the Money sub-score's prior (spec §6.1).
    price_band: tuple[float, float]
    #: How shippable a product in this domain is for a solo operator, 0-100.
    feasibility_prior: float

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
    """Every category, plus the shared feasibility penalties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    categories: tuple[Category, ...]
    feasibility_penalties: tuple[str, ...] = ()

    def by_id(self, category_id: str) -> Category:
        for category in self.categories:
            if category.id == category_id:
                return category
        raise TaxonomyError(f"unknown category {category_id!r}")

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(category.id for category in self.categories)

    def penalty_for(self, phrase: str) -> tuple[str, ...]:
        """Which feasibility penalties a phrase trips (patents, hazmat, regulation...)."""
        normalized = normalize_phrase(phrase)
        return tuple(
            penalty
            for penalty in self.feasibility_penalties
            if _contains(normalized, penalty)
        )

def normalize_phrase(phrase: str) -> str:
    """Lowercase, apostrophes removed, other punctuation to spaces, collapse whitespace.

    This is *the* matching space: scoring, grounding and evidence lookup all tokenize
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
        feasibility_penalties=tuple(raw.get("feasibility_penalties") or ()),
    )


@lru_cache(maxsize=4)
def default_taxonomy(config_dir: Path | str | None = None) -> Taxonomy:
    """The repository taxonomy, cached: it is read once per process, never per phrase."""
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    return load_taxonomy(directory / "categories.yaml")
