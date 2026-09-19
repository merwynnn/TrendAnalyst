"""Revenue: Monte Carlo over TAM x CTR x CVR x price, reported as P10/P50/P90 (spec §6.1).

A single revenue number would be a lie dressed as a forecast. Four multiplied quantities,
each uncertain by an order of magnitude, produce a distribution with a very long right
tail — so this module returns the tail percentiles and the assumptions that produced them,
and the ranked table shows the range rather than the midpoint.

**The honest problem, stated plainly:** TAM is the one input the pipeline cannot yet
observe. Search volume arrives with the Tier-A sources in P4. Until then TAM is derived
from the signal volume this entity actually produced, multiplied by a documented constant
that says "the pipeline sees a small, biased sample of a market", with a wide lognormal
uncertainty around it. That is an assumption, it is labelled as one, and it is recorded in
every snapshot — which is what makes it replaceable instead of invisible.

Determinism (spec §5.4): the RNG is seeded from a hash of the entity, category and model
version, so replaying a checkpoint reproduces identical percentiles. No wall clock, no
global randomness, no ``random`` module state leaks between candidates.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Final

from config.categories import Category
from trend_analyst.scoring.features import EntityFeatures
from trend_analyst.scoring.normalize import clamp

__all__ = [
    "MODEL_V1",
    "RevenueModel",
    "RevenueTriple",
    "estimate_revenue",
    "seed_for",
]

#: One draw is one month: TAM is a monthly reach number, so the triple is monthly revenue.
DEFAULT_DRAWS: Final = 4000

#: Below this monthly figure even the optimistic case is pocket change, and the ranked
#: table says so instead of printing a confident number nobody should act on.
MEANINGFUL_P90: Final = 100.0


@dataclass(frozen=True, slots=True)
class RevenueModel:
    """Every assumption the triple rests on, in one versioned object.

    Beta parameters are stated as (successes, failures) so they read as the intuition they
    encode: ``ctr=(2, 38)`` is "typically 5% of reach clicks, and anything above 15% would
    be surprising". Lognormal sigmas are the spread of the unknown: ``tam_sigma=0.9`` means
    the true market could plausibly be 2.5x larger or smaller than the estimate.
    """

    version: str
    #: Observed signal volume under-counts true addressable demand by roughly this factor.
    tam_scale: float
    tam_sigma: float
    #: Click-through from reach to a product page.
    ctr: tuple[float, float]
    #: Conversion from a visit to a sale.
    cvr: tuple[float, float]
    #: Spread of the price around the category band's midpoint, in log space.
    price_sigma: float
    draws: int = DEFAULT_DRAWS

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "tam_scale": self.tam_scale,
            "tam_sigma": self.tam_sigma,
            "ctr_alpha": self.ctr[0],
            "ctr_beta": self.ctr[1],
            "cvr_alpha": self.cvr[0],
            "cvr_beta": self.cvr[1],
            "price_sigma": self.price_sigma,
            "draws": self.draws,
        }


#: Conservative on purpose: a 5% click rate and a 7% conversion rate are already optimistic
#: for cold traffic, and the wide TAM sigma is the honest half of this model.
MODEL_V1: Final = RevenueModel(
    version="rev1",
    tam_scale=30.0,
    tam_sigma=0.9,
    ctr=(2.0, 38.0),
    cvr=(2.0, 28.0),
    price_sigma=0.3,
)


@dataclass(frozen=True, slots=True)
class RevenueTriple:
    """Monthly revenue at the 10th, 50th and 90th percentile, in USD."""

    p10: float
    p50: float
    p90: float
    model_version: str
    assumptions: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "p10": round(self.p10, 2),
            "p50": round(self.p50, 2),
            "p90": round(self.p90, 2),
            "model_version": self.model_version,
        }

    @property
    def is_meaningful(self) -> bool:
        """False when even the optimistic case is pocket change — worth flagging, not hiding."""
        return self.p90 >= MEANINGFUL_P90


def seed_for(entity: str, category_id: str, model_version: str) -> int:
    """A stable 32-bit seed. Same inputs, same draws, forever (spec §5.4)."""
    digest = hashlib.sha256(f"{entity}|{category_id}|{model_version}".encode()).hexdigest()
    return int(digest[:8], 16)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def estimate_revenue(
    features: EntityFeatures,
    category: Category,
    *,
    model: RevenueModel = MODEL_V1,
) -> RevenueTriple:
    """Monte Carlo the monthly revenue for one candidate.

    TAM comes from the entity's own 90-day signal volume (see the module docstring for what
    that is standing in for). The price is drawn lognormally around the category band's
    midpoint and clamped into the band itself, because a $400 draw for a $15-180 category is
    not uncertainty, it is a bug.
    """
    rng = random.Random(seed_for(features.entity, category.id, model.version))

    # Monthly reach: a quarter of the 90-day volume, floored at one so a quiet entity still
    # produces a distribution instead of a zero.
    monthly_signals = max(features.volume_90d / 3.0, 1.0)
    low_price, high_price = category.price_band
    midpoint = category.midpoint_price

    draws: list[float] = []
    for _ in range(model.draws):
        tam = monthly_signals * model.tam_scale * math.exp(rng.gauss(0.0, model.tam_sigma))
        ctr = rng.betavariate(model.ctr[0], model.ctr[1])
        cvr = rng.betavariate(model.cvr[0], model.cvr[1])
        price = clamp(midpoint * math.exp(rng.gauss(0.0, model.price_sigma)), low_price, high_price)
        draws.append(tam * ctr * cvr * price)

    draws.sort()
    return RevenueTriple(
        p10=_percentile(draws, 0.10),
        p50=_percentile(draws, 0.50),
        p90=_percentile(draws, 0.90),
        model_version=model.version,
        assumptions={
            **model.as_dict(),
            "monthly_signals": round(monthly_signals, 4),
            "price_band": list(category.price_band),
            "price_midpoint": midpoint,
            "seed": seed_for(features.entity, category.id, model.version),
        },
    )
