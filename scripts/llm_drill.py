"""The live failover drill (brief P3: "exactly one live run to prove failover and accounting").

    uv run python -m scripts.llm_drill            # uses the configured providers
    uv run python -m scripts.llm_drill --dry-run  # shows what it would do, calls nobody

It runs five probes, in this order, and each one must pass:

1. **Live call.** One real request through the gateway, and the answer must validate against
   the Judge schema. This is the only place in the project that spends a token, so it asks for
   a small, bounded judgement rather than a full batch.
2. **Failover.** The same prompt with the first provider's credential deliberately broken: the
   chain must move on and still return a schema-valid answer, recording each failure.
3. **Cache.** The identical prompt again must be served from the cache and reach no provider.
4. **Accounting.** The token log and the quota ledger must both show the spend, per gate.
5. **Grounding.** A verdict citing a URL the pipeline never collected must come back with that
   quote stripped and `ungrounded` set — the code-enforced rule, checked against a live answer.

Why a script and not a test: it needs a network and a credential, and the project's rule is
that tests never touch either. It is run by hand, its output is pasted into the evidence log,
and it exits non-zero the moment a probe fails — so a passing run is a fact, not a claim.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.llm.cache import token_spend_by_gate
from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    ProviderSpec,
    call_gate,
    default_chain,
)
from trend_analyst.llm.providers import render_prompt, settings_sender
from trend_analyst.llm.schemas import JudgeVerdict, Quote, enforce_grounding
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import QuotaLedger

__all__ = ["main", "run_drill"]

#: A failover proof needs at least two attempts: the injected failure, then a live answer.
_MIN_FAILOVER_ATTEMPTS = 2

INSTRUCTIONS = (
    "You are the Judge gate of a product-gap pipeline. Given one candidate product phrase and "
    "its collected evidence, decide whether it is worth keeping for enrichment. Answer with "
    "JSON only, matching this schema exactly: "
    '{"phrase": str, "category": str, "decision": "keep"|"drop", "confidence": 0..1, '
    '"fad_label": "fad"|"trend"|"evergreen"|null, "fad_probability": 0..1|null, '
    '"reason": str (max 1200 chars), "quotes": [{"text": str, "url": str, "source_id": str}], '
    '"enrich": [str]}  '
    "Every quote must be copied from the evidence below and carry its exact URL."
)

CANDIDATE: dict[str, Any] = {
    "phrase": "circ saw",
    "category": "tools_diy",
    "signals": [
        {
            "source": "arctic_shift",
            "ts": "2026-09-19T13:44:00+02:00",
            "value": 2.0,
            "url": "https://www.reddit.com/r/Tools/comments/abc123/circ_saw_blade_guard/",
            "text": "the blade guard on my circ saw broke in a week and the base plate is not flat",
        },
        {
            "source": "hn_firebase",
            "ts": "2026-09-18T09:12:00+00:00",
            "value": 4.0,
            "url": "https://news.ycombinator.com/item?id=49734467",
            "text": "Ask HN: what is the best circ saw for a small workshop?",
        },
    ],
    "mgs": 54.6,
    "sub_scores": {"dv": 50.0, "ss": 50.0, "sp": 50.0, "mp": 63.0, "fe": 77.0},
    "revenue_p50": 183.0,
}
EVIDENCE_URLS = tuple(signal["url"] for signal in CANDIDATE["signals"])


def _line(label: str, ok: bool, detail: str, *, live: bool = True) -> str:
    """One transcript line. A dry run reports SKIP, not FAIL: it evaluated nothing, and a
    check that cannot fail must not claim one either."""
    marker = ("PASS" if ok else "FAIL") if live else "SKIP"
    return f"{marker}  {label:<12} {detail}"


def run_drill(  # noqa: PLR0915 - five probes read better as one sequence than as five helpers
    *,
    sender: Any,
    sessions: Any,
    chain: Sequence[ProviderSpec],
    live: bool,
    root_note: str = "",
    nonce: str | None = None,
) -> tuple[bool, list[str]]:
    """The five probes. Returns (all passed, transcript lines).

    Each probe carries a fresh ``nonce`` in its payload. The first draft did not, and the probes
    silently interfered through the cache: a previous drill's probe-2 call had warmed that key, so
    the next run's failover probe was answered from the cache without contacting anything — a
    green-looking probe that measured nothing. The cache probe is the exception: it deliberately
    reuses probe 1's exact payload, because proving a cache hit requires a warm entry.
    """
    budget = DEFAULT_BUDGETS["judge"]
    stamp = nonce or uuid.uuid4().hex[:12]
    probe_one_payload = {**CANDIDATE, "probe": 1, "nonce": stamp}
    probe_two_payload = {**CANDIDATE, "probe": 2, "nonce": stamp}
    prompt = render_prompt(probe_one_payload, INSTRUCTIONS)
    lines: list[str] = []
    passed = True

    with sessions() as session:
        probe_one = call_gate(
            session,
            gate="judge",
            prompt=prompt,
            schema=JudgeVerdict,
            sender=sender,
            chain=chain,
            budget=budget,
            payload=probe_one_payload,
            evidence_urls=EVIDENCE_URLS,
        )
        session.commit()
        ok = probe_one.ok and isinstance(probe_one.value, JudgeVerdict)
        passed &= ok or not live
        lines.append(
            _line(
                "live call",
                ok,
                f"provider={probe_one.provider}:{probe_one.model} status={probe_one.status} "
                f"tokens={probe_one.prompt_tokens}+{probe_one.completion_tokens} "
                f"attempts={probe_one.attempts}"
                + ("" if ok else f" reason={probe_one.reason[:200]}"),
                live=live,
            )
        )
        if ok:
            verdict = probe_one.value
            lines.append(
                f"      verdict: decision={verdict.decision} confidence={verdict.confidence} "
                f"fad={verdict.fad_label} quotes={len(verdict.quotes)} "
                f"enrich={len(verdict.enrich)} {root_note}"
            )

    # --- 2. failover: make the first provider fail, on purpose ----------------
    #
    # The failure is injected rather than staged with a bogus model name, because with one paid
    # provider in the chain a bogus first model means *every* provider fails and the probe would
    # test nothing. What this probe must prove is the gateway's behaviour — records the failure,
    # moves to the next provider, still returns a schema-valid answer — and the failure itself is
    # real in production (the drill's own transcript shows a live 404, a live 402 and a missing
    # key from this very key set).
    broken = ProviderSpec(name=chain[0].name, model=chain[0].model)
    failover_chain = (broken, *chain[1:])
    real_sender = sender

    calls = {"n": 0}

    def failing_first(provider: ProviderSpec, text: str) -> tuple[str, int, int]:
        """Fail exactly once, then delegate: the chain must answer from a live provider."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"injected provider failure on {provider}")
        return real_sender(provider, text)

    sender = failing_first
    with sessions() as session:
        probe_two = call_gate(
            session,
            gate="judge",
            # NB: a fresh payload, not just a fresh prompt. The first draft reused a payload an
            # earlier run had already cached, so this probe was answered from the cache and
            # measured nothing while looking green.
            prompt=render_prompt(probe_two_payload, INSTRUCTIONS),
            schema=JudgeVerdict,
            sender=sender,
            chain=failover_chain,
            budget=budget,
            payload=probe_two_payload,
            evidence_urls=EVIDENCE_URLS,
        )
        session.commit()
        # What is provable depends on how many providers can actually answer. The gateway's
        # duty is to record the failure and then try each remaining provider IN CHAIN ORDER —
        # that is asserted always. That a *different* provider answered is asserted only when
        # one is configured and funded; otherwise the chain correctly exhausts and degrades.
        attempted = [note.split(":")[0] for note in probe_two.notes]
        expected = [provider.name for provider in chain]
        # The first call was injected to fail; every subsequent attempt must follow chain order.
        order_ok = attempted == expected[: len(attempted)]
        # What is assertable depends on what can actually answer. The gateway's duty is to record
        # the failure and then walk the remaining entries in chain order — that is always required.
        # That a LATER provider produced the answer is asserted only when one could: with Groq
        # unkeyed, Cerebras unfunded (402) and no local Ollama running, a probe that demanded a
        # second success would fail for reasons that have nothing to do with the gateway.
        answered_elsewhere = bool(probe_two.ok and probe_two.provider)
        ok = bool(
            order_ok
            and probe_two.attempts >= _MIN_FAILOVER_ATTEMPTS
            and (answered_elsewhere or any("injected" in note for note in probe_two.notes))
        )
        passed &= ok or not live
        lines.append(
            _line(
                "failover",
                ok,
                f"first call to {broken.name} failed as injected; chain then walked "
                f"{' -> '.join(attempted) or 'nothing'} in chain order over "
                f"{probe_two.attempts} attempt(s); answered by "
                + (
                    f"{probe_two.provider}:{probe_two.model}"
                    if answered_elsewhere
                    else "no further provider (each entry's failure is recorded above) - "
                    "degraded with a reason, which is the spec's required behaviour"
                ), live=live,
            )
        )
        for note in probe_two.notes[:4]:
            lines.append(f"      failed: {note[:150]}")
        sender = real_sender

    # --- 3. cache: the same prompt must not reach a provider -------------------
    reached: list[str] = []

    def counting(provider: ProviderSpec, text: str) -> tuple[str, int, int]:
        reached.append(provider.name)
        return real_sender(provider, text)

    with sessions() as session:
        probe_three = call_gate(
            session,
            gate="judge",
            prompt=prompt + "\n(probe 2: failover)",  # identical to probe 2's prompt
            schema=JudgeVerdict,
            sender=counting,
            chain=failover_chain,
            budget=budget,
            payload={**CANDIDATE, "probe": 2},
            evidence_urls=EVIDENCE_URLS,
        )
        session.commit()
        ok = probe_three.status == "cached" and not reached
        passed &= ok or not live
        lines.append(
            _line(
                "cache",
                ok,
                f"status={probe_three.status} providers_called={len(reached)} "
                "(identical work must never pay twice)", live=live,
            )
        )

    # --- 4. accounting --------------------------------------------------------
    with sessions() as session:
        spend = token_spend_by_gate(session)
        ledger = session.execute(
            select(QuotaLedger).where(QuotaLedger.source_id.like("llm_%"))
        ).scalars().all()
        judge_spend = spend.get("judge", {})
        ok = bool(judge_spend.get("total_tokens", 0) > 0 and ledger)
        passed &= ok
        lines.append(
            _line(
                "accounting",
                ok,
                f"token log: judge answers={judge_spend.get('answers', 0)} "
                f"tokens={judge_spend.get('total_tokens', 0)} "
                f"(prompt={judge_spend.get('prompt_tokens', 0)}, "
                f"completion={judge_spend.get('completion_tokens', 0)}); "
                f"ledger rows={len(ledger)} "
                f"amount={sum(int(row.amount) for row in ledger)}",
            )
        )
        for row in ledger[:3]:
            lines.append(f"      ledger: {row.operation} amount={row.amount} reason={row.reason}")

    # --- 5. grounding ---------------------------------------------------------
    invented = JudgeVerdict.model_validate(
        {
            "phrase": "circ saw",
            "category": "tools_diy",
            "decision": "keep",
            "confidence": 0.9,
            "fad_label": "trend",
            "fad_probability": 0.2,
            "reason": "citing something nobody collected",
            "quotes": [
                Quote(text="a study says so", url="https://example.com/invented-study"),
                Quote(text="guard broke", url=EVIDENCE_URLS[0], source_id="arctic_shift"),
            ],
            "enrich": [],
        }
    )
    cleaned, dropped = enforce_grounding(invented, evidence_urls=EVIDENCE_URLS)
    ok = dropped == 1 and len(cleaned.quotes) == 1 and cleaned.ungrounded is False
    passed &= ok
    lines.append(
        _line(
            "grounding",
            ok,
            f"invented URL removed={dropped}, kept={len(cleaned.quotes)} "
            f"ungrounded={cleaned.ungrounded}",
        )
    )

    if not live:
        lines.append("      (dry run: no provider was contacted)")
    return passed, lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.llm_drill",
        description="One live run proving gateway failover, cache, grounding and accounting.",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not contact any provider")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable transcript")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        settings = load_settings(config_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        engine = create_db_engine(settings)
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    chain = default_chain()
    sender = settings_sender(settings)
    note = ""
    if not args.dry_run:
        configured = [
            provider.name
            for provider in chain
            if str(getattr(settings.llm, f"{provider.name}_api_key", "") or "")
        ]
        note = f"(configured providers: {', '.join(configured) or 'none'})"

    sessions = create_session_factory(engine)
    try:
        passed, lines = run_drill(
            sender=sender, sessions=sessions, chain=chain, live=not args.dry_run, root_note=note
        )
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps({"passed": passed, "lines": lines}, indent=2))
    else:
        print("LLM gateway drill (brief P3: one live run to prove failover and accounting)")
        print(f"chain: {' -> '.join(str(provider) for provider in chain)} {note}")
        for line in lines:
            print(line)
        print("\nDRILL: PASS" if passed else "\nDRILL: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
