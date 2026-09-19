"""The gateway: provider chain, per-gate budgets, cache, validation, honest degradation.

Spec §6.3, implemented line by line:

* *"Provider chain with automatic failover: Gemini Flash -> Groq -> Cerebras -> local Ollama."*
  Providers are tried in order; a transport error, a refusal or a schema violation moves to the
  next one.
* *"Per-gate call and token caps; on cap, the gate degrades (skip with reason logged), never
  retries blindly."* A cap hit is a terminal, recorded outcome — not an invitation to retry.
* *"Identical work never hits a provider twice."* The cache is consulted before any provider.
* *"Schema violation means retry once, then drop with reason."* Once per provider, so a
  permanently malformed provider cannot burn the whole chain's budget on one candidate.

The transport is injected (`sender`), so the entire gateway is testable without a network and
without keys — which is what the brief asks for: *"Start with mocked provider responses, then
exactly one live run to prove failover and accounting."*
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from trend_analyst.llm.cache import (
    DEFAULT_TTL_DAYS,
    cache_key,
    get_cached,
    put_cached,
    record_tokens,
)
from trend_analyst.llm.schemas import (
    GateName,
    enforce_grounding,
    parse_gate_output,
)

__all__ = [
    "DEFAULT_CHAIN",
    "GateBudget",
    "GatewayOutcome",
    "ProviderSpec",
    "call_gate",
    "default_chain",
]

#: Spec §6.3's chain, in order, with the model ids each provider's API actually accepts.
#:
#: Three drafts of this tuple were wrong, and the *live drill* is what caught them:
#:   "gemini-flash" (a friendly name, not an id) -> HTTP 404
#:   "gemini-2.0-flash" (retired for this key)  -> HTTP 404
#:   "gemini-2.5-flash" (advertised, refused)   -> HTTP 404 "no longer available to new users"
#:   gemini-flash-latest 503 under load        -> "experiencing high demand" (retried, then
#:                                                 the chain's second Gemini model answers)
#:   "llama3.3-70b" on Cerebras                 -> 404; that account serves only
#:                                                 qwen-3.8-27b and gpt-oss-120b
#: `gemini-flash-latest` with API version v1beta is what answers 200 for this key. Note that
#: `GET /v1beta/models` LISTS gemini-2.5-flash as available while `generateContent` refuses it
#: for a new account, and that the `-latest` aliases do not exist on v1: a models list is a
#: catalogue, not a promise. A mock would have accepted every one of the wrong names, which is
#: exactly why the brief asks for one live run.
DEFAULT_CHAIN: Final[tuple[tuple[str, str], ...]] = (
    ("gemini", "gemini-flash-latest"),
    # A second model from the same provider, and the live drill is why: Gemini answered 200 for
    # small requests while returning HTTP 503 "experiencing high demand" for the drill's ~450-token
    # prompt, consistently, for minutes. One model per provider is a single point of failure that
    # the spec's chain cannot fail over from, because the next link (Groq) has no key here and the
    # one after it (Cerebras) answers 402. The order still leads with the spec's first provider.
    ("gemini", "gemini-3.6-flash"),
    ("groq", "llama-3.3-70b-versatile"),
    ("cerebras", "gpt-oss-120b"),
    ("ollama", "llama3.2"),
)

OutcomeStatus = Literal["ok", "cached", "skipped"]

#: A provider error that is worth one more attempt before failing over. Live evidence demanded
#: this: Gemini answered HTTP 503 "currently experiencing high demand" on a perfectly valid
#: request, and with only one paid provider configured a transient blip became a gate outage.
#: 429 and 5xx are the provider saying "not now", not "not ever" — unlike a 404 (wrong model) or
#: a 402 (no credit), which are permanent and must fail over immediately.
_TRANSIENT_MARKERS: Final[tuple[str, ...]] = (
    "429", "500", "502", "503", "504", "high demand", "overloaded", "timeout", "timed out",
    "temporarily",
)
#: Attempts per provider for a *transient* error, including the first.
_TRANSIENT_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One provider in the chain."""

    name: str
    model: str

    def __str__(self) -> str:
        return f"{self.name}:{self.model}"


def default_chain() -> tuple[ProviderSpec, ...]:
    """Gemini Flash -> Groq -> Cerebras -> local Ollama."""
    return tuple(ProviderSpec(name=name, model=model) for name, model in DEFAULT_CHAIN)


@dataclass(frozen=True, slots=True)
class GateBudget:
    """Per-gate caps (spec §6.2's call budgets, §6.3's token caps)."""

    gate: GateName
    calls_per_day: int
    tokens_per_day: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "gate": self.gate,
            "calls_per_day": self.calls_per_day,
            "tokens_per_day": self.tokens_per_day,
        }


#: The nightly budgets from spec §6.2: Judge is batched (~20 calls), Writer is top-K (~30),
#: the Planner runs weekly (~1). Token caps are the §6.3 "token caps" and are deliberately
#: generous per call but small per day: they exist to stop a runaway loop, not to ration.
DEFAULT_BUDGETS: Final[dict[str, GateBudget]] = {
    "planner": GateBudget(gate="planner", calls_per_day=4, tokens_per_day=60_000),
    "judge": GateBudget(gate="judge", calls_per_day=25, tokens_per_day=400_000),
    "writer": GateBudget(gate="writer", calls_per_day=40, tokens_per_day=600_000),
}


@dataclass(slots=True)
class GatewayOutcome:
    """What the gateway did, in the shape the ledger and the caller both need."""

    status: OutcomeStatus
    gate: str
    reason: str = ""
    provider: str = ""
    model: str = ""
    value: Any | None = None
    cached: bool = False
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    removed_quotes: int = 0
    ungrounded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "cached"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "gate": self.gate,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "cached": self.cached,
            "attempts": self.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "removed_quotes": self.removed_quotes,
            "ungrounded": self.ungrounded,
        }


#: A sender returns (raw_text, prompt_tokens, completion_tokens) or raises. Injected so tests
#: never touch a provider, and so a live run swaps one function rather than the gateway.
Sender = Callable[[ProviderSpec, str], "tuple[str, int, int]"]


def _sum_tokens(*values: int) -> int:
    return sum(int(value) for value in values)


def _is_transient(message: str) -> bool:
    """True when a provider error is worth another attempt rather than an immediate failover."""
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def call_gate(
    session: Session,
    *,
    gate: GateName,
    prompt: str,
    schema: type[BaseModel],
    sender: Sender,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    payload: Mapping[str, Any] | None = None,
    cached_calls_today: int = 0,
    cached_tokens_today: int = 0,
    evidence_urls: Sequence[str] = (),
    run_id: Any = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> GatewayOutcome:
    """Run one gate call through cache, caps, chain, schema and grounding.

    Args:
        prompt: the full prompt text (hashed for the cache, sent verbatim to the provider).
        payload: the structured input the prompt was built from. Part of the cache key when
            given — two prompts that differ only in whitespace share a cache entry.
        cached_calls_today, cached_tokens_today: already-spent counts for this gate, read by
            the caller from the ledger. Passed in rather than queried so a batch of candidates
            can share one read instead of hammering the ledger per call.
        evidence_urls: the URLs the pipeline actually collected for this candidate. Grounding
            is enforced against these.
        dry_run: do everything, write nothing, call nobody — used by tests and by `--dry-run`.
    """
    providers = tuple(chain) if chain is not None else default_chain()
    limits = budget or DEFAULT_BUDGETS[gate]
    key_material: Mapping[str, Any] | str = payload if payload is not None else prompt

    if cached_calls_today >= limits.calls_per_day:
        return GatewayOutcome(
            status="skipped",
            gate=gate,
            reason=(
                f"daily call cap reached ({cached_calls_today}/{limits.calls_per_day}); "
                "degrading without retry (spec §6.3)"
            ),
        )
    if cached_tokens_today >= limits.tokens_per_day:
        return GatewayOutcome(
            status="skipped",
            gate=gate,
            reason=(
                f"daily token cap reached ({cached_tokens_today}/{limits.tokens_per_day}); "
                "degrading without retry (spec §6.3)"
            ),
        )

    primary = providers[0] if providers else ProviderSpec(name="none", model="none")
    key = cache_key(gate, primary.model, key_material)
    if not dry_run:
        cached_output = get_cached(session, key, now=now)
        if cached_output is not None:
            validated = schema.model_validate(cached_output)
            cleaned, removed = _ground(validated, evidence_urls)
            return GatewayOutcome(
                status="cached",
                gate=gate,
                reason="cache hit: identical work never reaches a provider (spec §6.3)",
                provider="cache",
                model=primary.model,
                value=cleaned,
                cached=True,
                removed_quotes=removed,
                ungrounded=bool(getattr(cleaned, "ungrounded", False)),
            )

    attempts = 0
    failures: list[str] = []

    for provider in providers:
        # Two retry budgets, kept apart on purpose. The first draft shared one 2-iteration loop
        # for both, which silently capped transient retries at two and made _TRANSIENT_ATTEMPTS a
        # constant that lied about the behaviour.
        schema_retries = 0
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            attempts += 1
            if dry_run:
                failures.append(f"{provider}: dry run, provider not called")
                break
            try:
                raw, prompt_tokens, completion_tokens = sender(provider, prompt)
            except Exception as exc:  # any provider failure fails over, never aborts the gate
                message = str(exc)
                failures.append(f"{provider}: transport error: {message}")
                if _is_transient(message) and attempt < _TRANSIENT_ATTEMPTS:
                    # Bounded, and only for "not now" errors: a 404 (wrong model) or a 402 (no
                    # credit) is permanent and moves on immediately.
                    continue
                break

            outcome = parse_gate_output(raw, schema)
            if outcome.failed:
                failures.append(f"{provider} attempt {attempt}: {outcome.reason}")
                if schema_retries < 1:  # spec §6.3: a schema violation retries once
                    schema_retries += 1
                    continue
                break

            assert outcome.value is not None  # parse_gate_output guarantees it when ok
            cleaned, removed = _ground(outcome.value, evidence_urls)
            if not dry_run:
                put_cached(
                    session,
                    key,
                    gate=gate,
                    model=provider.model,
                    output=outcome.value.model_dump(mode="json"),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    now=now,
                    ttl_days=DEFAULT_TTL_DAYS,
                )
                record_tokens(
                    session,
                    gate=gate,
                    model=provider.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cache_key_value=key,
                    run_id=run_id,
                )
            return GatewayOutcome(
                status="ok",
                gate=gate,
                reason="",
                provider=provider.name,
                model=provider.model,
                value=cleaned,
                attempts=attempts,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                removed_quotes=removed,
                ungrounded=bool(getattr(cleaned, "ungrounded", False)),
                notes=failures,
            )

    return GatewayOutcome(
        status="skipped",
        gate=gate,
        reason=(
            "every provider failed after validation; dropping with reason rather than "
            f"retrying blindly: {'; '.join(failures[:4]) or 'no providers configured'}"
        ),
        attempts=attempts,
        notes=failures,
    )


def _ground(value: Any, evidence_urls: Sequence[str]) -> tuple[Any, int]:
    """Apply the code-enforced grounding rule to whatever the schema produced."""
    if hasattr(value, "quotes"):
        return enforce_grounding(value, evidence_urls=evidence_urls)
    return value, 0


def load_prompt(path: str | Path) -> str:  # pragma: no cover - helper for live runs
    """Read a prompt template from disk (used by the live failover drill)."""
    return str(Path(path).read_text(encoding="utf-8"))


def json_prompt(payload: Mapping[str, Any]) -> str:  # pragma: no cover - helper for live runs
    """Render a payload as a compact JSON prompt body."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)
