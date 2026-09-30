"""MGS — the Minimum Gap Score, computed in code and never by the LLM (spec §6.1).

The formula is fixed by the specification::

    MGS = 0.30*DV + 0.25*(100 - SS) + 0.20*SP + 0.15*MP + 0.10*FE

with every input a **per-category percentile** computed in SQL/Python. The specification
gives the abbreviations, not the words, so the expansions this code uses are:

===========  =========================  ================================================
Sub-score    Meaning                    How this build computes it
===========  =========================  ================================================
``DV``       Demand velocity            percentile of the EWMA z-score of the 30-day
                                        series (blended with the 30d-vs-prior growth),
                                        inside the category
``SS``       Saturation                 percentile of 90-day volume and of how many
                                        sources corroborate — a proxy, see below
``SP``       Seller pain                percentile of the share of signals carrying
                                        complaint markers, blended with their count
``MP``       Money potential            category price band mapped onto a revenue curve,
                                        blended with the percentile of buying intent
``FE``       Feasibility                the category's shippability prior, minus penalties
===========  =========================  ================================================

**Two of these are priors, not percentiles, and that is a documented deviation from a
strict reading of §6.1.** ``FE`` and the price half of ``MP`` cannot be a percentile yet:
nothing in the three sources collected so far can tell two ideas in the same category
apart on shippability or price. Rather than manufacture variation, they use the category
prior from ``config/categories.yaml`` and say so. P4's Tier-A sources (sold-versus-listed
counts, review volume, price distributions) turn both into measured percentiles — the
function signatures do not change, only the inputs.

``SS`` is the other honest weakness: with no incumbent data, "saturated" is inferred from
being large and widely corroborated. It is a proxy with a known bias — it will not notice a
crowded niche that is merely quiet — and the snapshot records the inputs so a later
version can be compared against it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from config.categories import Category, Taxonomy
from trend_analyst.scoring.features import COMPLEXITY_MARKERS, EntityFeatures
from trend_analyst.scoring.normalize import clamp, percentile_ranks, zscore

__all__ = [
    "PRICE_SCALE_HIGH",
    "PRICE_SCALE_LOW",
    "SMALL_SAMPLE_THRESHOLD",
    "WEIGHTS_V1",
    "MgSWeights",
    "ScoreResult",
    "SubScores",
    "price_score",
    "score_all",
    "score_category",
]

#: The revenue curve's anchors: a $10 idea scores 0, a $400 idea scores 100, log-spaced
#: between. Chosen because both ends are real in the category file and the midpoint of the
#: log range lands near the $60-80 where most of these domains actually live.
PRICE_SCALE_LOW: Final = 10.0
PRICE_SCALE_HIGH: Final = 400.0

#: Below this many candidates in a category, percentiles are decoration. The score is still
#: produced (work must continue) but it is flagged, recorded in the snapshot, and rendered
#: with a marker in the ranked table so nobody reads a 100 as a measured 100.
SMALL_SAMPLE_THRESHOLD: Final = 5

#: How the two halves of each blended sub-score are weighted. Not spec-mandated (the spec
#: fixes only the five MGS weights); these are the version's judgement, recorded so a later
#: version can be compared against this one rather than silently replacing it.
_BLEND: Final = {
    "dv_ewma": 0.6,
    "dv_growth": 0.4,
    "ss_volume": 0.7,
    "ss_corroboration": 0.3,
    "sp_ratio": 0.6,
    "sp_count": 0.4,
    "mp_price": 0.5,
    "mp_intent": 0.5,
}

#: Feasibility penalties, in points off the category prior.
_PENALTY_PER_KEYWORD: Final = 12.0
_PENALTY_PER_COMPLEXITY: Final = 6.0
_FEASIBILITY_FLOOR: Final = 5.0


@dataclass(frozen=True, slots=True)
class MgSWeights:
    """The weights and their version — the identity of a scoring model."""

    version: str
    dv: float
    ss: float
    sp: float
    mp: float
    fe: float
    #: Current interest, 0 in v1 (which predates it). Defaults to 0 so every weights
    #: literal written before interest existed still validates and still sums to 1.
    ci: float = 0.0

    def __post_init__(self) -> None:
        total = self.dv + self.ss + self.sp + self.mp + self.fe + self.ci
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"weights must sum to 1.0, got {total!r}")

    def as_dict(self) -> dict[str, float]:
        return {
            "dv": self.dv,
            "ss": self.ss,
            "sp": self.sp,
            "mp": self.mp,
            "fe": self.fe,
            "ci": self.ci,
        }


#: Spec §6.1, verbatim: MGS = 0.30DV + 0.25(100-SS) + 0.20SP + 0.15MP + 0.10FE
WEIGHTS_V1: Final = MgSWeights(version="v1", dv=0.30, ss=0.25, sp=0.20, mp=0.15, fe=0.10)

#: v2 adds current interest (raw mention + engagement heat, global percentile): DV .20,
#: gap .20, SP .15, MP .10, FE .10, CI .25. Interest is the single biggest voice —
#: a giant everyone already argues about outranks a fast riser from zero. Old rows
#: keep v1 and stay comparable — the weights version is on every snapshot, and the
#: inputs dict carries the raw interest so v2 can be recomputed, not just reread.
WEIGHTS_V2: Final = MgSWeights(
    version="v2", dv=0.20, ss=0.20, sp=0.15, mp=0.10, fe=0.10, ci=0.25
)


@dataclass(frozen=True, slots=True)
class SubScores:
    """The 0-100 inputs to the gap score, plus why each one looks the way it does."""

    dv: float
    ss: float
    sp: float
    mp: float
    fe: float
    #: Current interest (raw heat, global percentile). 0 in v1, which predates it.
    ci: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "dv": round(self.dv, 2),
            "ss": round(self.ss, 2),
            "sp": round(self.sp, 2),
            "mp": round(self.mp, 2),
            "fe": round(self.fe, 2),
            "ci": round(self.ci, 2),
        }

    @property
    def gap(self) -> float:
        """``100 - SS``: the headroom half of the formula."""
        return 100.0 - self.ss


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """One candidate's score snapshot: entity, category, MGS, inputs and provenance."""

    entity: str
    category_id: str
    mgs: float
    sub_scores: SubScores
    weights_version: str
    #: Population size of the category the percentiles ran against.
    population: int
    as_of: datetime
    features: dict[str, float | int | str | list[float] | tuple[str, ...]] = field(
        default_factory=dict
    )
    #: Inputs kept verbatim so a later weights version can be compared, not just replaced.
    inputs: dict[str, float] = field(default_factory=dict)

    @property
    def small_sample(self) -> bool:
        """True when this score's percentiles come from too few candidates to be precise."""
        return self.population < SMALL_SAMPLE_THRESHOLD

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "category": self.category_id,
            "mgs": round(self.mgs, 2),
            "sub_scores": self.sub_scores.as_dict(),
            "weights_version": self.weights_version,
            "population": self.population,
            "small_sample": self.small_sample,
            "inputs": {key: round(value, 4) for key, value in self.inputs.items()},
        }


def price_score(midpoint: float) -> float:
    """Map a price onto the 0-100 revenue curve (log-spaced between the anchors).

    >>> price_score(10.0)
    0.0
    >>> price_score(400.0)
    100.0
    >>> round(price_score(60.0), 1)
    48.5
    """
    if midpoint <= PRICE_SCALE_LOW:
        return 0.0
    if midpoint >= PRICE_SCALE_HIGH:
        return 100.0
    span = math.log(PRICE_SCALE_HIGH / PRICE_SCALE_LOW)
    return clamp(100.0 * math.log(midpoint / PRICE_SCALE_LOW) / span)


def _feasibility(
    features: EntityFeatures, category: Category, taxonomy: Taxonomy
) -> tuple[float, float, float]:
    """Category shippability prior minus penalties. Returns (score, penalties, complexity)."""
    penalties = len(taxonomy.penalty_for(features.entity))
    joined = " ".join(text.lower() for text in features.sample_texts)
    complexity = sum(1 for marker in COMPLEXITY_MARKERS if marker in joined)
    raw = (
        category.feasibility_prior
        - _PENALTY_PER_KEYWORD * penalties
        - _PENALTY_PER_COMPLEXITY * complexity
    )
    return clamp(max(raw, _FEASIBILITY_FLOOR)), float(penalties), float(complexity)


def score_category(
    features: Sequence[EntityFeatures],
    category: Category,
    *,
    taxonomy: Taxonomy,
    as_of: datetime,
    weights: MgSWeights = WEIGHTS_V1,
    interest: Mapping[str, float] | None = None,
) -> list[ScoreResult]:
    """Score every candidate in one category against that category's own population.

    The population is the category, never the whole lake: that is what makes a sub-score a
    *per-category* percentile (spec §6.1), and what stops a niche idea from being punished
    for not being a celebrity. Interest is the exception, deliberately: heat is absolute,
    and normalizing it away per niche would hide the giant everyone is talking about —
    so it arrives pre-normalized (global percentile across the night's candidates).
    """
    if not features:
        return []

    population = len(features)
    ewma_levels = [item.ewma_30d for item in features]
    dv_ewma = percentile_ranks([zscore(level, ewma_levels) for level in ewma_levels])
    dv_growth = percentile_ranks([item.growth for item in features])
    ss_volume = percentile_ranks([item.volume_90d for item in features])
    ss_corroboration = percentile_ranks([float(item.source_count) for item in features])
    sp_ratio = percentile_ranks([item.pain_ratio for item in features])
    sp_count = percentile_ranks([float(item.pain_signals) for item in features])
    mp_intent = percentile_ranks([item.intent_ratio for item in features])
    price = price_score(category.midpoint_price)

    results: list[ScoreResult] = []
    for index, item in enumerate(features):
        feasibility, penalties, complexity = _feasibility(item, category, taxonomy)
        ci_value = clamp(float((interest or {}).get(item.entity, 0.0)))
        sub_scores = SubScores(
            dv=clamp(_BLEND["dv_ewma"] * dv_ewma[index] + _BLEND["dv_growth"] * dv_growth[index]),
            ss=clamp(
                _BLEND["ss_volume"] * ss_volume[index]
                + _BLEND["ss_corroboration"] * ss_corroboration[index]
            ),
            sp=clamp(_BLEND["sp_ratio"] * sp_ratio[index] + _BLEND["sp_count"] * sp_count[index]),
            mp=clamp(_BLEND["mp_price"] * price + _BLEND["mp_intent"] * mp_intent[index]),
            fe=feasibility,
            ci=ci_value,
        )
        mgs = (
            weights.dv * sub_scores.dv
            + weights.ss * sub_scores.gap
            + weights.sp * sub_scores.sp
            + weights.mp * sub_scores.mp
            + weights.fe * sub_scores.fe
            + weights.ci * sub_scores.ci
        )
        results.append(
            ScoreResult(
                entity=item.entity,
                category_id=category.id,
                mgs=mgs,
                sub_scores=sub_scores,
                weights_version=weights.version,
                population=population,
                as_of=as_of,
                features=item.as_dict(),
                inputs={
                    "ewma_z": zscore(item.ewma_30d, ewma_levels),
                    "growth": item.growth,
                    "volume_90d": item.volume_90d,
                    "source_count": float(item.source_count),
                    "pain_ratio": item.pain_ratio,
                    "pain_signals": float(item.pain_signals),
                    "intent_ratio": item.intent_ratio,
                    "price_midpoint": category.midpoint_price,
                    "feasibility_prior": category.feasibility_prior,
                    "feasibility_penalties": penalties,
                    "complexity_markers": complexity,
                    "interest": ci_value,
                },
            )
        )
    return results


def score_all(
    features_by_category: Mapping[str, Sequence[EntityFeatures]],
    *,
    taxonomy: Taxonomy,
    as_of: datetime,
    weights: MgSWeights = WEIGHTS_V1,
    interest: Mapping[str, float] | None = None,
) -> list[ScoreResult]:
    """Score every category, returning one flat list ordered by MGS (highest first)."""
    scored: list[ScoreResult] = []
    for category_id, features in features_by_category.items():
        category = taxonomy.by_id(category_id)
        scored.extend(
            score_category(
                features, category, taxonomy=taxonomy, as_of=as_of,
                weights=weights, interest=interest,
            )
        )
    # Ties break on the entity name so the order is deterministic across runs — a ranked
    # table that shuffles equal scores cannot be diffed between nights.
    scored.sort(key=lambda result: (-result.mgs, result.entity))
    return scored
