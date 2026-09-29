"""Second-opinion scoring: interest, naming, categories, dedupe.

Four user-facing corrections in one place: MGS v2 adds current interest (raw mention +
engagement heat) to the formula; the Extractor must name clear generic products, never
brands; the taxonomy grows beauty, fashion, toys and wellness; and candidates dedupe
across runs on a canonical phrase so "LED Strips" never becomes a second "led strip".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import get_args

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.categories import canonical_phrase, default_taxonomy
from trend_analyst.llm.extract import EXTRACTOR_CATEGORIES, EXTRACTOR_INSTRUCTIONS, heuristic_sender
from trend_analyst.llm.schemas import ExtractorCategory
from trend_analyst.pipeline.decide import run_decide
from trend_analyst.pipeline.layers.l1 import MinedPhrase
from trend_analyst.scoring.features import EntityFeatures
from trend_analyst.scoring.interest import phrase_interest
from trend_analyst.scoring.mgs import WEIGHTS_V1, WEIGHTS_V2, score_category
from trend_analyst.store.models import Candidate, SignalRow
from trend_analyst.store.snapshots import candidate_key, latest_ranked, upsert_candidates

NOW = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.fixture
def sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A session factory inside one transaction, rolled back after the test."""
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def test_canonical_phrase_singularizes_timidly() -> None:
    """Case, spacing and simple plurals merge; ambiguous endings never do."""
    assert canonical_phrase("LED Strips") == "led strip"
    assert canonical_phrase("  desk   mats ") == "desk mat"
    assert canonical_phrase("batteries") == "battery"
    assert canonical_phrase("mattresses") == "mattress"
    assert canonical_phrase("glass") == "glass"
    assert canonical_phrase("news") == "news"
    assert canonical_phrase("virus") == "virus"
    assert canonical_phrase("gas") == "gas"
    assert canonical_phrase("circ saw") == "circ saw"
    assert canonical_phrase("") == ""


def test_weights_v2_adds_interest_and_v1_is_untouched() -> None:
    """v2 = DV .25 + gap .20 + SP .15 + MP .10 + FE .10 + CI .20; v1 has no CI."""
    assert WEIGHTS_V1.ci == 0.0
    assert WEIGHTS_V1.as_dict()["ci"] == 0.0
    assert WEIGHTS_V2.version == "v2"
    assert WEIGHTS_V2.ci == 0.20
    assert WEIGHTS_V2.as_dict()["ci"] == 0.20


def test_interest_flows_into_subscores_and_mgs() -> None:
    """The night's heat lands in the score, scaled by the v2 weight."""
    taxonomy = default_taxonomy()
    category = taxonomy.by_id("tools_diy")
    results = score_category(
        [EntityFeatures(entity="circ saw"), EntityFeatures(entity="desk mat")],
        category,
        taxonomy=taxonomy,
        as_of=NOW,
        weights=WEIGHTS_V2,
        interest={"circ saw": 80.0},
    )
    by_entity = {result.entity: result for result in results}
    assert by_entity["circ saw"].sub_scores.ci == 80.0
    assert by_entity["circ saw"].weights_version == "v2"
    assert by_entity["circ saw"].inputs["interest"] == 80.0
    assert by_entity["desk mat"].sub_scores.ci == 0.0  # unknown heat, not missing data
    assert by_entity["circ saw"].mgs > by_entity["desk mat"].mgs


def test_taxonomy_grew_four_categories() -> None:
    """Beauty, fashion, toys and wellness are scoring buckets with priors."""
    taxonomy = default_taxonomy()
    assert taxonomy.version == 2
    for category_id in (
        "beauty_personal_care", "fashion_apparel", "toys_games", "health_wellness",
    ):
        assert category_id in taxonomy.ids
        category = taxonomy.by_id(category_id)
        assert category.midpoint_price > 0
        assert 0 < category.feasibility_prior <= 100
    schema_categories = set(get_args(ExtractorCategory))
    assert {
        "beauty_personal_care", "fashion_apparel", "toys_games", "health_wellness",
    } <= schema_categories
    assert set(EXTRACTOR_CATEGORIES) >= schema_categories


def test_extractor_prompt_demands_clear_generic_names() -> None:
    """No brands, no models, no proper nouns — a shopper-searchable product name."""
    assert "brand" in EXTRACTOR_INSTRUCTIONS
    assert "beauty_personal_care" in EXTRACTOR_INSTRUCTIONS
    assert "toys_games" in EXTRACTOR_INSTRUCTIONS


@pytest.mark.db
def test_upsert_merges_spelling_variants(sessions: sessionmaker[Session]) -> None:
    """One run's 'LED Strips' and the next night's 'led strip' are one candidate."""
    def mined(phrase: str, mentions: int) -> MinedPhrase:
        return MinedPhrase(
            phrase=phrase, category_id="tools_diy", mentions=mentions,
            source_ids=("arctic_shift",), first_seen=NOW, last_seen=NOW, texts=("t",),
        )

    with sessions() as session:
        first = upsert_candidates(session, [mined("LED Strips", 2)])
        second = upsert_candidates(session, [mined("led strip", 5)])
        assert set(first) == set(second)  # same key, same row
        assert first[candidate_key("led strip", "tools_diy")] == second[
            candidate_key("LED Strips", "tools_diy")
        ]
        rows = session.query(Candidate).filter(Candidate.category == "tools_diy").all()
        assert len(rows) == 1
        assert rows[0].phrase == "led strip"  # stored canonical
        assert rows[0].mentions == 5  # the new count, moving forward only


@pytest.mark.db
def test_phrase_interest_counts_mentions_and_chatter(
    sessions: sessionmaker[Session],
) -> None:
    """Interest is mentions plus raw engagement, percentiled across the night."""
    with sessions() as session:
        session.add_all(
            [
                SignalRow(
                    source_id="hn_firebase", entity="no-drill shelf", metric="comments",
                    value=40.0, ts=NOW, category="tools_diy",
                    quote="landlord keeps my deposit", metadata_json={"comments": 40},
                ),
                SignalRow(
                    source_id="wiki_pageviews", entity="quiet gadget", metric="pageviews",
                    value=5.0, ts=NOW, category="tools_diy", quote="",
                ),
            ]
        )
        session.flush()
        interest = phrase_interest(session, ["no-drill shelf", "quiet gadget"], as_of=NOW)
    assert set(interest) == {"no-drill shelf", "quiet gadget"}
    assert interest["no-drill shelf"] > interest["quiet gadget"]  # heat wins
    assert all(0.0 <= value <= 100.0 for value in interest.values())


@pytest.mark.db
def test_decide_v2_writes_interest_snapshots(sessions: sessionmaker[Session]) -> None:
    """End to end on one signal: v2 snapshots carry interest, versioned as v2."""
    with sessions() as session:
        session.add(
            SignalRow(
                source_id="hn_firebase", entity="no-drill shelf", metric="comments",
                value=12.0, ts=NOW, category="tools_diy", quote="landlord keeps my deposit",
                url="https://example.com/t/1", metadata_json={"comments": 12},
            )
        )
        session.flush()
    report = run_decide(
        sessions=sessions,
        taxonomy=default_taxonomy(),
        as_of=NOW,
        weights=WEIGHTS_V2,
        trigger="manual",
        sender=heuristic_sender(),
        top=5,
    )
    assert report.scoring.scored > 0
    with sessions() as session:
        ideas = latest_ranked(session, limit=10)
        assert ideas
        for idea in ideas:
            assert idea.weights_version == "v2"
            assert idea.interest is not None
            assert 0.0 <= idea.interest <= 100.0
