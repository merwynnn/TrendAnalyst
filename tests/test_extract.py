"""Extractor gate tests: schema, chunking, code-enforced grounding, budgets.

The gate's contract, in test order:

* the golden fixtures validate (clean) or fail loudly (messy: off-taxonomy category);
* chunking is canonical — same texts, same chunks, whatever order they arrived in;
* grounding is in code: a ref the chunk did not contain is dropped with a count, and a
  product left with no refs is dropped whole (the extraction `unknown_phrase` rule);
* cross-chunk products merge their refs instead of splitting the evidence;
* caps are terminal (partial, named) and a failed chunk is a gap, not a crash;
* the cache makes replay deterministic: the provider is called once per chunk, ever.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.llm.extract import (
    ResolvedProduct,
    build_chunks,
    chunk_payload,
    chunk_prompt,
    extract_products,
    resolve_output,
    union_products,
)
from trend_analyst.llm.gateway import GateBudget, ProviderSpec, default_chain
from trend_analyst.llm.replay import load_fixture, schema_for_fixture
from trend_analyst.llm.schemas import ExtractorOutput, GateName, parse_gate_output
from trend_analyst.store.models import LLMCache

NOW = datetime(2026, 9, 20, tzinfo=UTC)

FIXTURES = Path(__file__).resolve().parent / "data" / "llm"


@pytest.fixture
def sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def texts(count: int, *, source: str = "arctic_shift") -> list[tuple[str, datetime, str]]:
    return [(f"wish a better product {index} existed", NOW, source) for index in range(count)]


def answer_json(*, fixture: str = "extractor_products.json") -> str:
    return json.dumps(load_fixture(FIXTURES / fixture)["output"])


def fixed_sender(text: str, *, calls: list[str] | None = None):
    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        if calls is not None:
            calls.append(prompt)
        return text, 100, 20

    return send


# Schema and fixtures.
def test_the_clean_fixture_validates() -> None:
    outcome = parse_gate_output(answer_json(), ExtractorOutput)
    assert outcome.ok, outcome.reason
    assert outcome.value is not None
    assert [product.phrase for product in outcome.value.products] == [
        "circ saw blade guard",
        "desk mat",
        "chisel storage",
    ]


def test_an_off_taxonomy_category_is_a_schema_violation() -> None:
    """`vehicles` is not a scoring bucket: the schema refuses the whole answer, and the
    existing retry-once-then-drop rule applies — never a guess the scorer cannot rank."""
    outcome = parse_gate_output(answer_json(fixture="extractor_products_messy.json"),
                                ExtractorOutput)
    assert outcome.failed
    assert "category" in outcome.reason


def test_replay_routes_products_to_the_extractor_schema() -> None:
    assert schema_for_fixture(load_fixture(FIXTURES / "extractor_products.json")) is (
        ExtractorOutput
    )


def test_gate_name_includes_the_extractor() -> None:
    assert "extractor" in GateName.__args__


# Chunking.
def test_chunks_are_canonical_regardless_of_input_order() -> None:
    forward, forward_index = build_chunks(texts(65, source="b") + texts(65, source="a"),
                                          chunk_size=30)
    backward, backward_index = build_chunks(texts(65, source="a") + texts(65, source="b"),
                                            chunk_size=30)
    assert [item.id for chunk in forward for item in chunk] == list(range(130))
    assert [(item.id, item.text) for chunk in forward for item in chunk] == [
        (item.id, item.text) for chunk in backward for item in chunk
    ]
    assert [len(chunk) for chunk in forward] == [30, 30, 30, 30, 10]
    # The index maps every global id back to its input position, whichever order arrived.
    assert sorted(forward_index) == list(range(130))
    assert forward_index != backward_index  # positions differ; ids do not


def test_prompt_numbers_every_text_and_payload_ignores_wrapping() -> None:
    (chunk,), _ = build_chunks(texts(2), chunk_size=30)
    prompt = chunk_prompt(chunk)
    assert "[0]" in prompt
    assert "[1]" in prompt
    assert "arctic_shift" in prompt
    assert chunk_payload(chunk)["texts"][0]["id"] == 0


# Grounding.
def test_unknown_refs_are_dropped_and_counted() -> None:
    output = ExtractorOutput.model_validate({"products": [
        {"phrase": "rebar cutter", "category": "tools_diy", "doc_ids": [0, 99]},
    ]})
    resolved, invented = resolve_output(output, valid_ids={0, 1})
    assert invented == 1
    assert [(item.phrase, item.point_ids) for item in resolved] == [
        ("rebar cutter", (0,))
    ]


def test_a_product_with_no_surviving_ref_is_dropped_whole() -> None:
    output = ExtractorOutput.model_validate({"products": [
        {"phrase": "ghost tool", "category": "tools_diy", "doc_ids": [77]},
    ]})
    resolved, invented = resolve_output(output, valid_ids={0})
    assert resolved == []
    assert invented == 1


def test_duplicate_phrases_merge_their_refs() -> None:
    output = ExtractorOutput.model_validate({"products": [
        {"phrase": "Desk Mat", "category": "home_office", "doc_ids": [0]},
        {"phrase": "desk mat ", "category": "home_office", "doc_ids": [1, 2]},
    ]})
    resolved, _ = resolve_output(output, valid_ids={0, 1, 2})
    assert len(resolved) == 1
    assert resolved[0].point_ids == (0, 1, 2)


def test_union_merges_across_chunks() -> None:
    merged = union_products([
        [ResolvedProduct(phrase="desk mat", category="home_office", point_ids=(0,))],
        [ResolvedProduct(phrase="Desk Mat", category="home_office", point_ids=(31,))],
    ])
    assert len(merged) == 1
    assert merged[0].point_ids == (0, 31)


# The gate (needs the database: the cache is a table).
@pytest.mark.db
def test_extract_resolves_products_and_counts_refs(
    sessions: sessionmaker[Session],
) -> None:
    # The chunk holds texts 0..2; the fixture cites 0,1 / 2 / 3 — so "chisel storage"
    # (ref 3 only) must die in grounding while the other two survive with ref 3 counted.
    chunks, _ = build_chunks(texts(3), chunk_size=30)
    with sessions() as session:
        report = extract_products(session, chunks, sender=fixed_sender(answer_json()))
    assert report.status == "ok"
    assert report.chunks == 1
    assert report.calls == 1
    assert report.products_raw == 3
    assert {item.phrase for item in report.products} == {
        "circ saw blade guard", "desk mat",
    }
    assert report.unknown_refs == 1


@pytest.mark.db
def test_schema_violation_fails_the_chunk_loudly(
    sessions: sessionmaker[Session],
) -> None:
    chunks, _ = build_chunks(texts(2), chunk_size=30)
    with sessions() as session:
        report = extract_products(
            session, chunks,
            sender=fixed_sender(answer_json(fixture="extractor_products_messy.json")),
        )
    # The messy fixture fails schema (off-taxonomy category), so the whole chunk fails:
    # a violation is a gap, reported, never a partial product.
    assert report.failed_chunks == 1
    assert report.products == ()


@pytest.mark.db
def test_dry_run_calls_nobody(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []
    chunks, _ = build_chunks(texts(65), chunk_size=30)
    with sessions() as session:
        report = extract_products(
            session, chunks,
            sender=fixed_sender(answer_json(), calls=calls), dry_run=True,
        )
    assert report.status == "dry-run"
    assert report.chunks == 3
    assert calls == []


@pytest.mark.db
def test_dry_run_writes_no_cache_rows(sessions: sessionmaker[Session]) -> None:
    """The heuristic may run, but its answers must never sit in the cache."""
    chunks, _ = build_chunks(texts(3), chunk_size=30)
    with sessions() as session:
        report = extract_products(
            session, chunks, sender=fixed_sender(answer_json()),
            dry_run=True, write_cache=False,
        )
        assert report.status == "dry-run"
        assert session.execute(select(LLMCache)).scalars().all() == []


@pytest.mark.db
def test_the_call_cap_is_terminal(sessions: sessionmaker[Session]) -> None:
    chunks, _ = build_chunks(texts(65), chunk_size=30)
    with sessions() as session:
        report = extract_products(
            session, chunks,
            sender=fixed_sender(answer_json()),
            budget=GateBudget(gate="extractor", calls_per_day=1, tokens_per_day=10_000_000),
        )
    assert report.status == "partial"
    assert "cap" in report.reason
    assert report.calls == 1
    assert report.attempted == 1


@pytest.mark.db
def test_a_failed_chunk_is_a_gap_not_a_crash(
    sessions: sessionmaker[Session],
) -> None:
    def failing(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        raise RuntimeError("503 overloaded")

    chunks, _ = build_chunks(texts(65), chunk_size=30)
    with sessions() as session:
        report = extract_products(
            session, chunks, sender=failing,
        )
    assert report.status == "degraded"
    assert report.failed_chunks == 3
    assert report.products == ()


@pytest.mark.db
def test_replay_is_deterministic_through_the_cache(
    sessions: sessionmaker[Session],
) -> None:
    calls: list[str] = []
    chunks, _ = build_chunks(texts(3), chunk_size=30)
    with sessions() as session:
        first = extract_products(session, chunks, sender=fixed_sender(answer_json(),
                                                                      calls=calls))
        second = extract_products(session, chunks, sender=fixed_sender(answer_json(),
                                                                       calls=calls))
    assert len(calls) == 1, "the second run is served from the cache"
    assert second.cached == 1
    assert [item.phrase for item in first.products] == [
        item.phrase for item in second.products
    ]


def test_default_chain_spreads_over_per_model_quotas() -> None:
    providers = [spec.name for spec in default_chain()]
    assert providers[0] == "gemini"
    assert len(set(providers)) >= 3
    gemini_models = [spec.model for spec in default_chain() if spec.name == "gemini"]
    assert len(set(gemini_models)) >= 5, "one id, one quota bucket: the spread IS the capacity"
