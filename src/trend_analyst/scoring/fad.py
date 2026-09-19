"""The fad flag: a classifier feature set, not a vibe (spec §6.1).

The specification names the features — *30d vs 90d vs 365d slope divergence, seasonal
strength, evergreen ratio* — and this module computes exactly those, then combines them
into a probability with hand-set weights rather than a trained model. That is a deliberate
choice, not a shortcut: there is no labelled history to train on until the Judge gate has
been keeping and dropping candidates for months (P3+), and a model trained on ten golden
cases would be a very confident way to encode ten golden cases.

So: a logistic function over interpretable features, every weight visible, the whole thing
versioned. When labelled history arrives, the features stay and only the weights move.

The output is one of the three labels the schema allows (``scores.fad_label`` has a CHECK
constraint listing exactly these, and the specification's §7 row lists one fad flag):

* ``fad`` — rose fast, on a slope the longer windows do not share
* ``evergreen`` — active most of the year, slopes flat: a standing need, not a moment
* ``trend`` — rising, and the longer window agrees

**A fourth label was written and then removed.** An earlier draft of this module emitted
``dead`` for candidates silent for two months; the database's CHECK constraint rejected it
mid-development, which is the constraint doing its job. Staleness is not a *shape*, it is a
reason not to mine something at all, so the test moved to L1's staleness filter
(``pipeline/layers/l1.py``) where it belongs, and the three labels here describe live
candidates only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final, Literal

from trend_analyst.scoring.features import EntityFeatures
from trend_analyst.scoring.normalize import clamp

__all__ = [
    "FAD_WEIGHTS_V1",
    "FadAssessment",
    "FadLabel",
    "FadWeights",
    "classify",
    "sigmoid",
]

FadLabel = Literal["fad", "trend", "evergreen"]

#: A candidate active on this share of the 365-day window is a standing need.
_EVERGREEN_RATIO: Final = 0.35
#: Probability above which a spiky candidate is called a fad rather than a trend.
_FAD_PROBABILITY: Final = 0.6
#: Slope divergence below which an all-year candidate is a standing need, not a spike.
_FLAT_DIVERGENCE: Final = 0.5


@dataclass(frozen=True, slots=True)
class FadWeights:
    """The logistic's coefficients, versioned like the MGS weights."""

    version: str
    bias: float
    slope_divergence: float
    spike: float
    seasonality: float
    evergreen: float
    long_trend: float

    def as_dict(self) -> dict[str, float]:
        return {
            "bias": self.bias,
            "slope_divergence": self.slope_divergence,
            "spike": self.spike,
            "seasonality": self.seasonality,
            "evergreen": self.evergreen,
            "long_trend": self.long_trend,
        }


#: Signs are the argument: a 30-day slope the 90-day slope does not share, a tall single
#: day, and a weekday pattern all push toward *fad*; being active all year and rising over
#: the full year both push away from it. The bias is negative so a quiet, flat entity lands
#: well under the threshold instead of at 0.5.
FAD_WEIGHTS_V1: Final = FadWeights(
    version="fadv1",
    bias=-1.0,
    slope_divergence=1.8,
    spike=0.8,
    seasonality=0.5,
    evergreen=-1.6,
    long_trend=-1.2,
)


@dataclass(frozen=True, slots=True)
class FadAssessment:
    """A probability, a label, and the features behind both."""

    probability: float
    label: FadLabel
    weights_version: str
    features: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "probability": round(self.probability, 4),
            "label": self.label,
            "weights_version": self.weights_version,
            "features": {key: round(value, 4) for key, value in self.features.items()},
        }


def sigmoid(value: float) -> float:
    """Numerically safe logistic function.

    >>> round(sigmoid(0.0), 3)
    0.5
    >>> round(sigmoid(50.0), 4)
    1.0
    >>> round(sigmoid(-50.0), 4)
    0.0
    """
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def classify(
    features: EntityFeatures,
    *,
    weights: FadWeights = FAD_WEIGHTS_V1,
) -> FadAssessment:
    """Classify one entity's shape as fad / trend / evergreen / dead.

    The label is decided by explicit rules over the same features, *before* the probability
    is considered for the ``fad``/``trend`` split. Order matters and is deliberate: "active
    all year" outranks "spiky", because an evergreen product that had a busy week is still
    evergreen.
    """
    # Feature scaling: each term is brought into roughly [-2, 2] so no single feature can
    # dominate the logit by being measured in different units.
    slope_divergence = clamp(features.slope_divergence, -2.0, 2.0)
    spike = clamp(math.log1p(features.spike_ratio) / 2.0, 0.0, 2.0)
    seasonality = clamp(features.seasonality / 2.0, 0.0, 2.0)
    evergreen = clamp(features.evergreen_ratio, 0.0, 1.0)
    long_trend = clamp(features.slope_365d, -2.0, 2.0)

    logit = (
        weights.bias
        + weights.slope_divergence * slope_divergence
        + weights.spike * spike
        + weights.seasonality * seasonality
        + weights.evergreen * evergreen
        + weights.long_trend * long_trend
    )
    probability = sigmoid(logit)

    label: FadLabel
    if evergreen >= _EVERGREEN_RATIO and slope_divergence < _FLAT_DIVERGENCE:
        label = "evergreen"
    elif probability >= _FAD_PROBABILITY:
        label = "fad"
    else:
        label = "trend"

    return FadAssessment(
        probability=probability,
        label=label,
        weights_version=weights.version,
        features={
            "slope_divergence": slope_divergence,
            "spike": spike,
            "seasonality": seasonality,
            "evergreen_ratio": evergreen,
            "slope_365d": long_trend,
            "days_since_last": float(features.days_since_last),
            "logit": logit,
        },
    )
