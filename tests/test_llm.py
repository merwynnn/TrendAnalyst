"""The LLM gates: mocked providers, real failover, real accounting (brief P3).

The brief says *"Start with mocked provider responses, then exactly one live run to prove
failover and accounting."* This file is the mocked half, and it asserts the parts that matter
before any provider is ever contacted:

* the chain fails over in order, and a schema violation costs exactly one retry per provider;
* a cap hit degrades with a reason instead of retrying blindly;
* identical work reaches a provider once, ever;
* **grounding is stripped in code**: a citation the pipeline never collected does not survive,
  no matter how confidently the model wrote it.

No test here touches a network; `sender` is always a stub.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.llm.cache import (
    cache_key,
    get_cached,
    normalize_input,
    purge_expired,
    put_cached,
    token_spend_by_gate,
)
from trend_analyst.llm.gateway import (
    _TRANSIENT_ATTEMPTS as _TRANSIENT,
)
from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    GatewayOutcome,
    ProviderSpec,
    call_gate,
    default_chain,
)
from trend_analyst.llm.schemas import (
    JudgeBatch,
    JudgeVerdict,
    Quote,
    WriterBrief,
    enforce_grounding,
    parse_gate_output,
)
from trend_analyst.store.models import LLMCache

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 20, tzinfo=UTC)
EVIDENCE = ("https://www.reddit.com/r/ecommerce/comments/abc/title/",)


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


def verdict_json(*, url: str = EVIDENCE[0], decision: str = "keep") -> str:
    return json.dumps(
        {
            "phrase": "circ saw",
            "category": "tools_diy",
            "decision": decision,
            "confidence": 0.7,
            "fad_label": "trend",
            "fad_probability": 0.3,
            "reason": "complaints cluster on the blade guard",
            "quotes": [{"text": "guard broke in a week", "url": url, "source_id": "arctic_shift"}],
            "enrich": ["ebay sold listings", "amazon reviews"],
        }
    )


def fixed_sender(text: str, *, prompt_tokens: int = 10, completion_tokens: int = 5):
    def send(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        return text, prompt_tokens, completion_tokens

    return send


def failing_sender(message: str = "503 unavailable"):
    def send(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        raise RuntimeError(message)

    return send


# ---------------------------------------------------------------------------
# schemas and grounding
# ---------------------------------------------------------------------------
def test_parse_gate_output_accepts_a_fenced_json_block() -> None:
    fenced = f"```json\n{verdict_json()}\n```"
    outcome = parse_gate_output(fenced, JudgeVerdict)
    assert outcome.ok
    assert outcome.value is not None
    assert outcome.value.phrase == "circ saw"


def test_parse_gate_output_reports_why_it_failed() -> None:
    assert parse_gate_output("not json at all", JudgeVerdict).failed
    bad = parse_gate_output(json.dumps({"phrase": "x", "decision": "maybe"}), JudgeVerdict)
    assert bad.failed
    assert "schema violation at decision" in bad.reason


def test_quote_urls_must_be_http() -> None:
    """A `javascript:` citation is not a source, and pydantic refuses it at the boundary."""
    with pytest.raises(Exception, match=r"http"):
        Quote(text="x", url="javascript:alert(1)")


def test_grounding_strips_a_citation_the_pipeline_never_collected() -> None:
    """The code-enforced rule: a claim without quote plus URL does not survive."""
    invented = JudgeVerdict.model_validate(
        json.loads(verdict_json(url="https://example.com/made-up"))
    )
    cleaned, dropped = enforce_grounding(invented, evidence_urls=EVIDENCE)
    assert dropped == 1
    assert cleaned.quotes == []
    assert cleaned.ungrounded is True  # marked, not deleted: the judgement still stands
    assert cleaned.decision == "keep"


def test_grounding_keeps_a_citation_that_matches_collected_evidence() -> None:
    real = JudgeVerdict.model_validate(json.loads(verdict_json()))
    cleaned, dropped = enforce_grounding(real, evidence_urls=EVIDENCE)
    assert dropped == 0
    assert len(cleaned.quotes) == 1
    assert cleaned.ungrounded is False


def test_grounding_without_evidence_urls_keeps_quotes() -> None:
    """A data gap must not masquerade as a grounding failure."""
    real = JudgeVerdict.model_validate(json.loads(verdict_json()))
    cleaned, dropped = enforce_grounding(real, evidence_urls=())
    assert dropped == 0
    assert cleaned.ungrounded is False


def test_writer_brief_uses_the_same_rule() -> None:
    brief = WriterBrief.model_validate(
        {
            "phrase": "circ saw",
            "verdict": "crowded but a guard redesign is defensible",
            "quotes": [{"text": "t", "url": "https://example.com/x"}],
        }
    )
    cleaned, dropped = enforce_grounding(brief, evidence_urls=EVIDENCE)
    assert dropped == 1
    assert cleaned.ungrounded is True


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def test_cache_key_ignores_whitespace_but_not_the_model() -> None:
    assert cache_key("judge", "gemini-flash", "a   b\nc") == cache_key(
        "judge", "gemini-flash", "a b c"
    )
    assert cache_key("judge", "gemini-flash", "a b") != cache_key("judge", "local", "a b")
    assert cache_key("judge", "m", {"b": 1, "a": 2}) == cache_key("judge", "m", {"a": 2, "b": 1})


def test_normalize_input_is_stable_for_nested_payloads() -> None:
    left = {"b": [1.0000001, "x  y"], "a": {"d": 1, "c": 2}}
    right = {"a": {"c": 2, "d": 1}, "b": [1.0, "x y"]}
    assert normalize_input(left) == normalize_input(right)


def test_cache_round_trip_and_expiry(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        key = cache_key("judge", "gemini-flash", "prompt")
        assert get_cached(session, key, now=NOW) is None
        put_cached(
            session, key, gate="judge", model="gemini-flash", output={"ok": True},
            prompt_tokens=10, completion_tokens=2, now=NOW, ttl_days=30,
        )
        assert get_cached(session, key, now=NOW) == {"ok": True}
        # 29 days later it is still there; 31 days later it is a miss.
        assert get_cached(session, key, now=NOW + timedelta(days=29)) == {"ok": True}
        assert get_cached(session, key, now=NOW + timedelta(days=31)) is None
        # And the TTL job removes it rather than leaving it to rot.
        assert purge_expired(session, now=NOW + timedelta(days=31)) == 1
        assert get_cached(session, key, now=NOW) is None


# ---------------------------------------------------------------------------
# gateway: failover, caps, cache, accounting
# ---------------------------------------------------------------------------
def test_first_provider_wins_when_it_answers(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict,
            sender=fixed_sender(verdict_json()), evidence_urls=EVIDENCE,
            run_id=None, now=NOW,
        )
        assert outcome.ok
        assert outcome.status == "ok"
        assert outcome.provider == "gemini"
        assert outcome.attempts == 1
        assert outcome.prompt_tokens == 10


def test_gateway_fails_over_in_chain_order(sessions: sessionmaker[Session]) -> None:
    """Order is the specification's; the chain carries a second Gemini model (see below)."""
    tried: list[str] = []

    def flaky(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        tried.append(provider.name)
        if provider.name != "cerebras":
            raise RuntimeError("429 rate limited")
        return verdict_json(), 12, 6

    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=flaky,
            evidence_urls=EVIDENCE, now=NOW,
        )
    assert outcome.ok
    assert outcome.provider == "cerebras"
    # A 429 is TRANSIENT, so each chain entry is retried before the chain moves on; and both
    # Gemini entries (the spec's, plus the second model the live drill proved necessary) come
    # before Groq. Order is the specification's.
    # A 429 is transient, so every chain entry is retried before the chain moves on. The
    # expectation is derived from the chain so adding a provider cannot leave this test lying.
    expected: list[str] = []
    for provider in default_chain():
        expected.extend([provider.name] * (_TRANSIENT if provider.name != "cerebras" else 1))
        if provider.name == "cerebras":
            break
    assert tried == expected
    assert any("429" in note for note in outcome.notes)


def test_schema_violation_retries_once_then_moves_on(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def bad_then_good(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        if provider.name == "gemini":
            return "I think we should keep this one.", 5, 5
        return verdict_json(), 5, 5

    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=bad_then_good,
            evidence_urls=EVIDENCE, now=NOW,
        )
    assert outcome.provider == "groq"
    # One schema retry per chain entry, over the chain's Gemini entries before Groq.
    geminis = sum(1 for provider in default_chain() if provider.name == "gemini")
    assert calls == ["gemini"] * (geminis * 2) + ["groq"]
    assert any("not JSON" in note for note in outcome.notes)


def test_a_permanent_provider_error_fails_over_immediately(
    sessions: sessionmaker[Session],
) -> None:
    """A 404 (wrong model) or a 402 (no credit) is not "not now" — it must not be retried.

    The live drill produced both: Gemini 404 for a retried model name, Cerebras 402 for an
    unfunded key. Retrying those would spend the day's call budget learning nothing.
    """
    tried: list[str] = []

    def permanent(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        tried.append(provider.name)
        if provider.name != "ollama":
            raise RuntimeError("HTTP 402: payment required")
        return verdict_json(), 1, 1

    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="permanent", schema=JudgeVerdict, sender=permanent,
            evidence_urls=EVIDENCE, now=NOW,
        )
    assert outcome.provider == "ollama"  # answered by the last entry
    assert tried == [provider.name for provider in default_chain()]  # one attempt each


def test_every_provider_failing_is_a_recorded_skip_not_an_abort(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict,
            sender=failing_sender(), now=NOW,
        )
    assert outcome.status == "skipped"
    assert outcome.value is None
    assert "dropping with reason" in outcome.reason
    # A 503 is transient, so every chain entry is retried before the gate gives up: the ledger
    # records all of them rather than one line per provider.
    assert len(outcome.notes) == len(default_chain()) * _TRANSIENT


def test_cache_hit_never_reaches_a_provider(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def counting(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        return verdict_json(), 3, 1

    with sessions() as session:
        first = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=counting,
            evidence_urls=EVIDENCE, now=NOW,
        )
        second = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=counting,
            evidence_urls=EVIDENCE, now=NOW,
        )
        assert first.status == "ok"
        assert second.status == "cached"
        assert calls == ["gemini"]  # identical work hit a provider exactly once

        # Whitespace-only differences share the same cache entry.
        third = call_gate(
            session, gate="judge", prompt="p   ", schema=JudgeVerdict, sender=counting,
            evidence_urls=EVIDENCE, now=NOW,
        )
        assert third.status == "cached"
        assert calls == ["gemini"]


def test_call_cap_degrades_with_a_reason_and_no_retry(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def counting(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        return verdict_json(), 1, 1

    limits = DEFAULT_BUDGETS["judge"]
    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=counting,
            cached_calls_today=limits.calls_per_day, now=NOW,
        )
    assert outcome.status == "skipped"
    assert "daily call cap reached" in outcome.reason
    assert "without retry" in outcome.reason
    assert calls == []  # a cap is terminal, not an invitation


def test_token_cap_degrades_too(sessions: sessionmaker[Session]) -> None:
    limits = DEFAULT_BUDGETS["writer"]
    with sessions() as session:
        outcome = call_gate(
            session, gate="writer", prompt="p", schema=WriterBrief,
            sender=fixed_sender("{}"), cached_tokens_today=limits.tokens_per_day, now=NOW,
        )
    assert outcome.status == "skipped"
    assert "token cap reached" in outcome.reason


def test_dry_run_calls_nobody_and_writes_nothing(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def counting(provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        return verdict_json(), 1, 1

    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict, sender=counting, dry_run=True,
        )
        assert outcome.status == "skipped"
        assert calls == []
        assert session.execute(select(LLMCache)).all() == []


def test_accounting_lands_in_the_token_log(
    sessions: sessionmaker[Session],
) -> None:
    """Brief: *"token log shows spend per gate"* — this is the assertion behind that claim."""
    with sessions() as session:
        call_gate(
            session, gate="judge", prompt="a", schema=JudgeVerdict,
            sender=fixed_sender(verdict_json()), evidence_urls=EVIDENCE, now=NOW,
        )
        call_gate(
            session, gate="judge", prompt="b", schema=JudgeVerdict,
            sender=fixed_sender(verdict_json(), prompt_tokens=20, completion_tokens=7),
            evidence_urls=EVIDENCE, now=NOW,
        )
        spend = token_spend_by_gate(session)
        assert spend["judge"]["answers"] == 2
        assert spend["judge"]["total_tokens"] == (10 + 5) + (20 + 7)


def test_a_batch_of_candidates_shares_one_gate_call(sessions: sessionmaker[Session]) -> None:
    """Judge batching (spec §6.2: ~20 calls a night) is one schema, many verdicts."""
    batch = {
        "verdicts": [
            json.loads(verdict_json()),
            json.loads(verdict_json(decision="drop")),
        ]
    }
    with sessions() as session:
        outcome = call_gate(
            session, gate="judge", prompt="batch", schema=JudgeBatch,
            sender=fixed_sender(json.dumps(batch)), evidence_urls=EVIDENCE, now=NOW,
        )
    assert outcome.ok
    assert isinstance(outcome.value, JudgeBatch)
    assert [verdict.decision for verdict in outcome.value.verdicts] == ["keep", "drop"]


def test_default_chain_matches_the_specification() -> None:
    """The spec's provider order, spread over Gemini's per-model quotas (documented in source).

    The extra entries are a deviation with live evidence behind it: Gemini returned HTTP 503
    "experiencing high demand" for the drill's ~450-token prompt while small prompts succeeded,
    and the 2026-09-21 model probe proved the 3.x family are distinct rate-limit buckets (200
    for 3.5/3.6, 503-overload for 3.7/3.8 — real ids, not wrong names). The distinct-provider
    order is still exactly Gemini -> Groq -> Cerebras -> Ollama.
    """
    chain = default_chain()
    names = [provider.name for provider in chain]
    assert names == ["gemini"] * 6 + ["groq", "cerebras", "ollama"]
    assert len({provider.model for provider in chain if provider.name == "gemini"}) == 6
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    assert seen == ["gemini", "groq", "cerebras", "ollama"]


def test_outcome_serializes_for_the_ledger(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        outcome: GatewayOutcome = call_gate(
            session, gate="judge", prompt="p", schema=JudgeVerdict,
            sender=fixed_sender(verdict_json()), evidence_urls=EVIDENCE, now=NOW,
        )
    payload = outcome.as_dict()
    assert payload["status"] == "ok"
    assert json.dumps(payload)  # the run note can carry it verbatim
