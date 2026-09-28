"""The pain gate: one LLM call per run assessing every niche's painful problem.

The user decides what is painful — *"a painful problem is something quite problematic to
some degree for people"* — so no keyword list here. The model reads each niche's top
products with their evidence texts and scores how painful the niche's core problem is
(0 = enthusiasm with no problem in sight, 100 = people actively suffering and paying
to stop it), names the problem in one line, and ties the score to the evidence in 1-2
sentences. Those three fields are what the dashboard shows per niche.

Mechanics mirror the Judge: one injected sender (tests never touch a provider), one
cached call per run (the cache key is the evidence payload, so a re-run over the same
lake is free), a per-day budget backstop, and degradation that changes nothing — an
unassessed niche simply shows no pain score rather than a zero that would read as "no
pain". Niches here are taxonomy categories: each one is a problem-space whose products
compete for the same wallet, which is exactly the unit the dashboard ranks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    GateBudget,
    ProviderSpec,
    Sender,
    call_gate,
    default_chain,
)
from trend_analyst.llm.schemas import NichePain, NichePainBatch

__all__ = [
    "PAIN_INSTRUCTIONS",
    "NicheEvidence",
    "PainReport",
    "assess_niches",
    "pain_prompt",
]

PAIN_INSTRUCTIONS: Final = """You are the pain gate of a niche-discovery pipeline. \
The goal is to find a
profitable niche: a product that solves a PAINFUL problem in a PASSIONATE market.

For each niche you receive its top products (with mention counts and momentum scores)
and evidence texts people actually wrote, from named sources.

For EACH niche, you — not a keyword list — decide what is painful. A painful problem
is something quite problematic to some degree for people: it costs them time, money,
health or peace of mind, it keeps recurring, and workarounds or complaints exist.
Score 0-100 how painful the niche's core problem is:
- 0-20: enthusiasm with no problem in sight (hobbies people enjoy, collections, upgrades);
- 21-50: annoyances and friction worth fixing but nobody suffers;
- 51-80: real recurring problems people actively work around or complain about;
- 81-100: people suffering and already paying to make it stop.
Name the core painful problem in one line (or state there is none), and give a 1-2
sentence rationale tied to the evidence. When the evidence shows wants without pain,
score low — wanting is not suffering.

Answer with JSON only:
{"niches": [{"category": str, "pain_score": 0..100, "painful_problem": str,
"rationale": str, "representative_phrases": [str]}]}"""

#: Evidence budget per niche: enough signal for a judgement, small enough for free tiers.
_MAX_PHRASES_PER_NICHE: Final = 8
_MAX_TEXTS_PER_NICHE: Final = 8
_MAX_TEXT_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class NicheEvidence:
    """What the model sees for one niche: its top products and people's own words."""

    category: str
    #: (phrase, mentions, mgs) triples, best first.
    products: tuple[tuple[str, int, float], ...] = ()
    sample_texts: tuple[str, ...] = ()


@dataclass(slots=True)
class PainReport:
    """One run's pain assessments, keyed by category."""

    status: str = "empty"
    reason: str = ""
    assessments: dict[str, NichePain] = field(default_factory=dict)
    calls: int = 0
    cached: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    unknown_categories: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "calls": self.calls,
            "cached": self.cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "unknown_categories": self.unknown_categories,
            "niches": {
                category: {
                    "pain_score": round(item.pain_score, 1),
                    "painful_problem": item.painful_problem,
                    "rationale": item.rationale,
                    "representative_phrases": list(item.representative_phrases),
                }
                for category, item in sorted(self.assessments.items())
            },
        }

    def summary(self) -> str:
        if self.status != "ok":
            return f"pain: {self.status} ({self.reason})"
        top = max(
            self.assessments.items(), key=lambda item: item[1].pain_score, default=None
        )
        headline = f"{top[0]}={top[1].pain_score:.0f}" if top else "no niches"
        return (
            f"pain: assessed {len(self.assessments)} niche(s), "
            f"most painful {headline}"
            f"{' (cached)' if self.cached else ''}"
        )


def pain_prompt(niches: Sequence[NicheEvidence]) -> tuple[str, dict[str, Any]]:
    """Build the prompt and the cache-key payload from the night's niche evidence."""
    lines = [PAIN_INSTRUCTIONS, ""]
    payload: dict[str, Any] = {"niches": []}
    for niche in niches:
        products = [
            {"phrase": phrase, "mentions": mentions, "mgs": round(mgs, 1)}
            for phrase, mentions, mgs in niche.products[:_MAX_PHRASES_PER_NICHE]
        ]
        texts = [
            text[:_MAX_TEXT_CHARS] for text in niche.sample_texts[:_MAX_TEXTS_PER_NICHE]
        ]
        payload["niches"].append(
            {"category": niche.category, "products": products, "texts": texts}
        )
        lines.append(f"## niche: {niche.category}")
        for product in products:
            lines.append(
                f"- {product['phrase']} (mentions {product['mentions']}, "
                f"MGS {product['mgs']})"
            )
        lines.append("evidence:")
        for text in texts:
            lines.append(f"  - {text}")
        lines.append("")
    return "\n".join(lines), payload


def assess_niches(
    session: Session,
    niches: Sequence[NicheEvidence],
    *,
    sender: Sender | None,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    bypass_cache: bool = False,
    progress: Any = None,
) -> PainReport:
    """Assess every niche's pain in a single cached provider call.

    Without a sender there is nothing to assess: the report says `skipped`, and the
    dashboard shows niches without pain scores rather than zeros that would read as
    "no pain". An assessment for an unknown category is counted and dropped, never
    applied to a lookalike.
    """
    report = PainReport()
    if not niches:
        report.reason = "no niches to assess"
        return report
    if sender is None and not dry_run:
        report.status = "skipped"
        report.reason = "no gate transport available, so no niche was assessed"
        return report
    prompt, payload = pain_prompt(niches)
    outcome = call_gate(
        session,
        gate="pain",
        prompt=prompt,
        schema=NichePainBatch,
        sender=sender if sender is not None else _never_sender,
        chain=tuple(chain) if chain is not None else default_chain(),
        budget=budget or DEFAULT_BUDGETS["pain"],
        payload=payload,
        now=now,
        dry_run=dry_run,
        bypass_cache=bypass_cache,
    )
    report.calls = 1
    report.cached = outcome.cached
    report.prompt_tokens = outcome.prompt_tokens
    report.completion_tokens = outcome.completion_tokens
    if outcome.capped or outcome.dry_run:
        report.status = "skipped"
        report.reason = outcome.reason
        return report
    if not outcome.ok or not isinstance(outcome.value, NichePainBatch):
        report.status = "degraded"
        report.reason = outcome.reason or "the provider gave no usable assessment"
        return report
    known = {niche.category for niche in niches}
    for item in outcome.value.niches:
        if item.category not in known:
            report.unknown_categories += 1
            continue
        report.assessments[item.category] = item
    report.status = "ok" if report.assessments else "degraded"
    report.reason = (
        ""
        if report.assessments
        else "no assessment matched a known niche"
        + (f" ({report.unknown_categories} unknown)" if report.unknown_categories else "")
    )
    if progress is not None:
        progress(f"pain: {report.summary()}")
    return report


def _never_sender(provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
    """A dry run with a real sender calls nobody — the gate reports a stop first."""
    raise AssertionError("pain gate called the provider on a dry run")


def niche_evidence_for(
    category: str,
    scored: Sequence[Any],
    texts_by_phrase: Mapping[str, Sequence[str]],
    *,
    limit: int = _MAX_PHRASES_PER_NICHE,
) -> NicheEvidence:
    """Pick a niche's evidence: its best-scoring products plus their own words."""
    ordered = sorted(scored, key=lambda item: (-item.mgs, item.phrase))[:limit]
    samples: list[str] = []
    for item in ordered:
        for text in texts_by_phrase.get(item.phrase, ()):
            if len(samples) >= _MAX_TEXTS_PER_NICHE:
                break
            cleaned = " ".join(text.split())
            if cleaned and cleaned not in samples:
                samples.append(cleaned)
    return NicheEvidence(
        category=category,
        products=tuple((item.phrase, item.mentions, item.mgs) for item in ordered),
        sample_texts=tuple(samples),
    )
