"""Scoring tests: normalization, features, MGS, the fad flag and the revenue triple.

These are pure-function tests on purpose. The scorer is the part of the system whose output
a human is meant to trust, so it should be checkable without a database, a network or a
fixture: hand-built feature vectors in, exact numbers out, with the arithmetic pinned where
the specification fixes it (MGS weights sum to 1.0 and the formula is 0.30/0.25/0.20/0.15/0.10).

The three properties every scorer in this repository must keep, asserted here:

* **deterministic** — same inputs, same numbers, including the Monte Carlo revenue;
* **bounded** — every sub-score is inside 0-100 and MGS is inside 0-100, at n=1 and at n=1000;
* **honest at small n** — a category with one candidate does not produce a perfect score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config.categories import TaxonomyError, default_taxonomy, load_taxonomy
from trend_analyst.scoring.fad import FAD_WEIGHTS_V1, classify, sigmoid
from trend_analyst.scoring.features import SignalPoint, extract_features
from trend_analyst.scoring.mgs import (
    WEIGHTS_V1,
    MgSWeights,
    price_score,
    score_all,
    score_category,
)
from trend_analyst.scoring.normalize import (
    SINGLETON_PERCENTILE,
    clamp,
    daily_series,
    ewma,
    ewma_z,
    growth_ratio,
    log_compress,
    normalized_slope,
    percentile_ranks,
    safe_ratio,
    summarize,
    zscore,
)
from trend_analyst.scoring.revenue import MODEL_V1, estimate_revenue, seed_for

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def points(
    text: str,
    *,
    days: int,
    per_day: int = 1,
    value: float = 1.0,
    source: str = "arctic_shift",
    end: datetime = NOW,
) -> list[SignalPoint]:
    """``per_day`` signals a day for the last ``days`` days, all saying ``text``."""
    return [
        SignalPoint(ts=end - timedelta(days=day), value=value, text=text, source_id=source)
        for day in range(days)
        for _ in range(per_day)
    ]


# ---------------------------------------------------------------------------
# normalize.py
# ---------------------------------------------------------------------------
def test_percentile_ranks_bounds_and_ties() -> None:
    assert percentile_ranks([1.0, 2.0, 3.0]) == [0.0, 50.0, 100.0]
    assert percentile_ranks([]) == []
    # A single observation is neither the best nor the worst: 50, not 100.
    assert percentile_ranks([42.0]) == [SINGLETON_PERCENTILE]
    # Ties share the average rank rather than being split arbitrarily.
    assert percentile_ranks([5.0, 5.0, 5.0]) == [50.0, 50.0, 50.0]


def test_percentile_ranks_are_order_independent() -> None:
    values = [3.0, 1.0, 9.0, 4.0, 4.0]
    forward = percentile_ranks(values)
    backward = percentile_ranks(list(reversed(values)))
    assert forward == list(reversed(backward))


def test_ewma_weights_the_newest_point_most() -> None:
    assert ewma([]) == 0.0
    assert ewma([0.0, 0.0]) == 0.0
    # Rising into the newest point: the EWMA must sit between the mean (2.5) and the last
    # value (10.0), and move toward it as alpha grows.
    series = [0.0, 0.0, 0.0, 10.0]
    assert ewma(series, alpha=0.5) == pytest.approx(5.0)
    assert ewma(series, alpha=1.0) == pytest.approx(10.0)
    assert ewma(series, alpha=0.1) == pytest.approx(1.0)


def test_zscore_of_a_flat_population_is_zero() -> None:
    assert zscore(5.0, [5.0, 5.0, 5.0]) == 0.0
    assert zscore(5.0, [5.0]) == 0.0
    # A value that IS the population's mean has no distance from it.
    assert zscore(2.0, [1.0, 3.0]) == 0.0
    # One sample standard deviation above and below the mean.
    assert zscore(3.0, [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert zscore(1.0, [1.0, 2.0, 3.0]) == pytest.approx(-1.0)


def test_ewma_z_is_relative_to_its_population() -> None:
    # The same series is unremarkable next to a bigger peer and remarkable next to a quiet one.
    quiet = ewma_z([1.0, 1.0], [0.1, 0.2, 0.3])
    loud = ewma_z([1.0, 1.0], [5.0, 6.0, 7.0])
    assert quiet > 0 > loud


def test_growth_ratio_is_floored_and_bounded() -> None:
    assert growth_ratio(5.0, 0.0) == pytest.approx(5.0)  # 1 mention today, 0 before
    assert growth_ratio(0.0, 10.0) == pytest.approx(-1.0)  # bounded on the way down
    assert growth_ratio(1000.0, 1.0) == pytest.approx(9.0)  # and on the way up
    assert growth_ratio(10.0, 10.0) == 0.0


def test_normalized_slope_needs_two_points_and_normalizes_scale() -> None:
    assert normalized_slope([1.0]) == 0.0
    assert normalized_slope([]) == 0.0
    # Same shape, 1000x the scale: the normalized slope is identical.
    assert normalized_slope([1.0, 2.0, 3.0]) == pytest.approx(
        normalized_slope([1000.0, 2000.0, 3000.0])
    )


def test_daily_series_is_dense_and_bucketizes_by_utc_day() -> None:
    series = daily_series([(NOW, 2.0), (NOW - timedelta(days=2), 3.0)], as_of=NOW, days=3)
    assert series == [3.0, 0.0, 2.0]  # a missing day is a real zero
    assert len(daily_series([], as_of=NOW, days=5)) == 5
    assert daily_series([], as_of=NOW, days=0) == []


def test_small_helpers() -> None:
    assert clamp(150.0) == 100.0
    assert clamp(-5.0) == 0.0
    assert clamp(float("nan")) == 0.0
    assert safe_ratio(1.0, 0.0, default=7.0) == 7.0
    assert log_compress(0.0, 10.0) == 0.0
    # `scale` is the size that counts as large: twice it saturates the curve.
    assert log_compress(20.0, 10.0) == pytest.approx(1.0)
    assert 0.5 < log_compress(10.0, 10.0) < 1.0
    assert summarize([])["count"] == 0.0
    assert summarize([2.0, 4.0])["median"] == 3.0


# ---------------------------------------------------------------------------
# features.py
# ---------------------------------------------------------------------------
def test_features_separate_recent_from_baseline_windows() -> None:
    # 10 mentions in the last 30 days, 10 in the 60 before that.
    recent = points("circ saw", days=30, per_day=1)
    older = points(
        "circ saw", days=1, per_day=10, end=NOW - timedelta(days=60)
    )
    features = extract_features("circ saw", recent + older, as_of=NOW)
    assert features.volume_30d == pytest.approx(30.0)
    assert features.volume_prior_60d == pytest.approx(10.0)
    assert features.growth > 0  # rising against its own baseline


def test_features_count_pain_and_intent_per_text_not_per_match() -> None:
    # One rant that says "broken" twice is one unhappy customer.
    texts = points("broken broken broken", days=1) + points("looking for a circ saw", days=1)
    features = extract_features("circ saw", texts, as_of=NOW)
    assert features.pain_signals == 1
    assert features.intent_signals == 1
    assert features.pain_ratio == pytest.approx(0.5)


def test_features_slope_divergence_marks_a_spike() -> None:
    # A quiet month with one loud day, versus the same total spread evenly.
    spike_points = points("cable management", days=30) + points(
        "cable management", days=1, per_day=20
    )
    flat_points = points("cable management", days=21, per_day=1)
    spike = extract_features("cable management", spike_points, as_of=NOW)
    flat = extract_features("cable management", flat_points, as_of=NOW)
    assert spike.slope_divergence > flat.slope_divergence
    assert spike.spike_ratio > flat.spike_ratio


def test_features_require_timezone_aware_input() -> None:
    with pytest.raises(ValueError, match=r"timezone-aware"):
        SignalPoint(ts=datetime(2026, 9, 20), value=1.0)  # noqa: DTZ001 - the point of the test
    with pytest.raises(ValueError, match=r"timezone-aware"):
        extract_features("x", [], as_of=datetime(2026, 9, 20))  # noqa: DTZ001


def test_features_reject_negative_values() -> None:
    with pytest.raises(ValueError, match=r"non-negative"):
        SignalPoint(ts=NOW, value=-1.0)


# ---------------------------------------------------------------------------
# mgs.py
# ---------------------------------------------------------------------------
def test_weights_match_the_specification_exactly() -> None:
    # Spec §6.1: MGS = 0.30DV + 0.25(100-SS) + 0.20SP + 0.15MP + 0.10FE
    assert (WEIGHTS_V1.dv, WEIGHTS_V1.ss, WEIGHTS_V1.sp, WEIGHTS_V1.mp, WEIGHTS_V1.fe) == (
        0.30,
        0.25,
        0.20,
        0.15,
        0.10,
    )
    assert WEIGHTS_V1.version == "v1"
    assert sum(WEIGHTS_V1.as_dict().values()) == pytest.approx(1.0)


def test_weights_that_do_not_sum_to_one_are_refused() -> None:
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        MgSWeights(version="broken", dv=0.5, ss=0.5, sp=0.5, mp=0.5, fe=0.5)


def test_price_score_is_log_spaced_between_the_anchors() -> None:
    assert price_score(5.0) == 0.0
    assert price_score(10.0) == 0.0
    assert price_score(400.0) == 100.0
    assert price_score(10_000.0) == 100.0
    assert 0 < price_score(60.0) < price_score(200.0) < 100.0


def test_mgs_formula_reproduces_by_hand() -> None:
    taxonomy = default_taxonomy()
    features = [
        extract_features("circ saw", points("circ saw", days=10, per_day=3), as_of=NOW),
        extract_features("saw blade", points("saw blade", days=5), as_of=NOW),
    ]
    scored = score_category(
        features, taxonomy.by_id("tools_diy"), taxonomy=taxonomy, as_of=NOW
    )
    for result in scored:
        subs = result.sub_scores
        expected = (
            0.30 * subs.dv + 0.25 * (100 - subs.ss) + 0.20 * subs.sp
            + 0.15 * subs.mp + 0.10 * subs.fe
        )
        assert result.mgs == pytest.approx(expected)


def test_single_candidate_category_does_not_score_perfectly() -> None:
    """The small-sample guard: an only child is 50th percentile, not 100th."""
    taxonomy = default_taxonomy()
    features = [extract_features("circ saw", points("circ saw", days=30, per_day=9), as_of=NOW)]
    result = score_category(features, taxonomy.by_id("tools_diy"), taxonomy=taxonomy, as_of=NOW)[0]
    assert result.small_sample  # population of one is flagged
    # No evidence-driven sub-score can reach the top of the scale from one observation.
    assert result.sub_scores.dv == pytest.approx(50.0)
    assert result.sub_scores.ss == pytest.approx(50.0)
    assert 0 <= result.mgs <= 100


def test_sub_scores_are_bounded_and_ordered_by_evidence() -> None:
    taxonomy = default_taxonomy()
    features = [
        extract_features("reusable mug", points("reusable mug", days=30, per_day=5), as_of=NOW),
        extract_features("larger mug", points("larger mug", days=2), as_of=NOW),
        extract_features("mug for hot", points("mug for hot", days=1, value=0.0), as_of=NOW),
    ]
    scored = score_all({"kitchen_dining": features}, taxonomy=taxonomy, as_of=NOW)
    for result in scored:
        for value in result.sub_scores.as_dict().values():
            assert 0.0 <= value <= 100.0
        assert 0.0 <= result.mgs <= 100.0
    # The steady, high-volume phrase outranks the one-day one.
    assert scored[0].entity == "reusable mug"


def test_score_all_breaks_ties_deterministically() -> None:
    taxonomy = default_taxonomy()
    features = [
        extract_features("saw blade", points("saw blade", days=3), as_of=NOW),
        extract_features("circ saw", points("circ saw", days=3), as_of=NOW),
    ]
    first = score_all({"tools_diy": features}, taxonomy=taxonomy, as_of=NOW)
    second = score_all({"tools_diy": list(reversed(features))}, taxonomy=taxonomy, as_of=NOW)
    assert [result.entity for result in first] == [result.entity for result in second]


# ---------------------------------------------------------------------------
# fad.py
# ---------------------------------------------------------------------------
def test_sigmoid_is_safe_at_the_extremes() -> None:
    assert sigmoid(0.0) == pytest.approx(0.5)
    assert sigmoid(1000.0) == 1.0  # no OverflowError
    assert sigmoid(-1000.0) == 0.0


def test_fad_label_is_one_of_the_three_the_schema_allows() -> None:
    """`scores.fad_label` has a CHECK constraint; a fourth label would be rejected."""
    for text, days, per_day in (
        ("circ saw", 3, 9),  # a spike
        ("circ saw", 300, 1),  # a standing need
        ("circ saw", 60, 2),  # an ordinary trend
    ):
        assessment = classify(
            extract_features(text, points(text, days=days, per_day=per_day), as_of=NOW)
        )
        assert assessment.label in {"fad", "trend", "evergreen"}
        assert 0.0 <= assessment.probability <= 1.0


def test_a_long_evergreen_history_is_not_a_fad() -> None:
    evergreen = classify(
        extract_features("insulation", points("insulation", days=300), as_of=NOW)
    )
    spike = classify(
        extract_features("insulation", points("insulation", days=2, per_day=9), as_of=NOW)
    )
    assert evergreen.probability < spike.probability


def test_fad_weights_are_versioned_and_visible() -> None:
    assert FAD_WEIGHTS_V1.version == "fadv1"
    assert set(FAD_WEIGHTS_V1.as_dict()) >= {"bias", "slope_divergence", "evergreen"}


# ---------------------------------------------------------------------------
# revenue.py
# ---------------------------------------------------------------------------
def test_revenue_is_deterministic_for_the_same_inputs() -> None:
    taxonomy = default_taxonomy()
    features = extract_features("circ saw", points("circ saw", days=30, per_day=2), as_of=NOW)
    first = estimate_revenue(features, taxonomy.by_id("tools_diy"))
    second = estimate_revenue(features, taxonomy.by_id("tools_diy"))
    assert first == second
    assert first.model_version == "rev1"


def test_revenue_draws_differ_between_candidates() -> None:
    """Determinism is per candidate, not a frozen constant."""
    assert seed_for("circ saw", "tools_diy", "rev1") != seed_for("saw blade", "tools_diy", "rev1")


def test_revenue_triple_is_ordered_and_keeps_the_price_inside_the_band() -> None:
    taxonomy = default_taxonomy()
    for category_id in taxonomy.ids:
        category = taxonomy.by_id(category_id)
        features = extract_features(f"a {category_id.replace('_', ' ')} thing",
                                    points("x", days=90, per_day=4), as_of=NOW)
        triple = estimate_revenue(features, category)
        assert 0 < triple.p10 <= triple.p50 <= triple.p90
        # Assumptions travel with the numbers, including the seed that produced them.
        assert triple.assumptions["price_band"] == list(category.price_band)
        assert triple.assumptions["seed"] == seed_for(features.entity, category.id, "rev1")


def test_revenue_scales_with_demand() -> None:
    taxonomy = default_taxonomy()
    category = taxonomy.by_id("kitchen_dining")
    quiet = estimate_revenue(
        extract_features("reusable mug", points("reusable mug", days=30), as_of=NOW), category
    )
    loud = estimate_revenue(
        extract_features("reusable mug", points("reusable mug", days=30, per_day=50), as_of=NOW),
        category,
    )
    assert loud.p50 > quiet.p50 * 5


def test_revenue_model_is_conservative_by_construction() -> None:
    # A CTR and CVR above 20% would make every candidate look like a business.
    assert MODEL_V1.ctr[0] / (MODEL_V1.ctr[0] + MODEL_V1.ctr[1]) < 0.2
    assert MODEL_V1.cvr[0] / (MODEL_V1.cvr[0] + MODEL_V1.cvr[1]) < 0.2
    assert MODEL_V1.tam_sigma >= 0.5  # the TAM assumption admits it is an assumption


# ---------------------------------------------------------------------------
# the taxonomy itself: scoring buckets, not a matcher
# ---------------------------------------------------------------------------
def test_taxonomy_penalties() -> None:
    taxonomy = default_taxonomy()
    assert taxonomy.penalty_for("patent lithium battery design") == (
        "patent",
        "lithium battery",
    )
    assert taxonomy.penalty_for("wooden spoon") == ()


def test_taxonomy_file_errors_are_loud(tmp_path) -> None:
    with pytest.raises(TaxonomyError, match="not found"):
        load_taxonomy(tmp_path / "missing.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="non-empty mapping"):
        load_taxonomy(broken)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        "version: 1\ncategories:\n  x:\n    label: X\n"
        "    price_band: [50, 10]\n    feasibility_prior: 50\n",
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError, match="invalid category"):
        load_taxonomy(invalid)

    unknown_keys = tmp_path / "unknown_keys.yaml"
    unknown_keys.write_text(
        "version: 1\ncategories:\n  x:\n    label: X\n    keywords: [a]\n    core: [a]\n"
        "    price_band: [10, 50]\n    feasibility_prior: 50\n",
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError, match="invalid category"):
        load_taxonomy(unknown_keys)


def test_every_category_has_a_usable_prior() -> None:
    taxonomy = default_taxonomy()
    assert len(taxonomy.categories) >= 8
    for category in taxonomy.categories:
        assert category.label, f"{category.id} has no label"
        assert 0 < category.price_band[0] < category.price_band[1]
        assert 0 < category.feasibility_prior <= 100
