"""Gate schemas and the grounding rule, enforced in code (spec §6.3, brief P3).

Every gate output is Pydantic-validated JSON. Two rules here are worth more than the models
they validate:

* **A schema violation means retry once, then drop with a reason** — never an infinite loop,
  never a silent pass-through of malformed JSON into the scorer.
* **Grounding is enforced in code**: *"any factual claim without quote plus URL is stripped
  before it reaches scores or briefs"*. That sentence is implemented literally — a quote
  survives only if it carries a text and an ``http(s)`` URL, and that URL must be one the
  pipeline actually collected for that candidate. A model that invents a citation loses it
  here, before anything reads it, and the loss is counted rather than trusted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "GateName",
    "GateSchemaError",
    "JudgeBatch",
    "JudgeVerdict",
    "PlannerPlan",
    "Quote",
    "ValidationOutcome",
    "WriterBrief",
    "enforce_grounding",
    "parse_gate_output",
]

#: A fenced block needs three parts when split on the fence marker.
_FENCE_PARTS: Final = 2

#: The three gates, matching `llm_cache.gate`'s CHECK constraint.
GateName = Literal["planner", "judge", "writer"]

Decision = Literal["keep", "drop"]
FadLabel = Literal["fad", "trend", "evergreen"]


class GateSchemaError(RuntimeError):
    """A gate returned something the schema refuses. Carries the reason for the ledger."""


class Quote(BaseModel):
    """A claim's evidence: the words, and where they came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=600)
    url: str = Field(min_length=8, max_length=2000)
    source_id: str = Field(default="", max_length=64)

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        """Only http(s). A `javascript:` or `file:` citation is not a source."""
        scheme = urlparse(value).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"quote URLs must be http(s), got {scheme or 'no scheme'!r}")
        return value


class JudgeVerdict(BaseModel):
    """One candidate's judgement: keep or drop, a fad call, a reason, and its evidence."""

    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1, max_length=300)
    category: str = Field(default="", max_length=64)
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    fad_label: FadLabel | None = None
    fad_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=1200)
    quotes: list[Quote] = Field(default_factory=list)
    #: What the Judge wants L2 to fetch for this candidate (Tier-A, P4).
    enrich: list[str] = Field(default_factory=list)
    #: Set by `enforce_grounding`, never by the model.
    ungrounded: bool = False
    dropped_quotes: int = 0

    @field_validator("enrich")
    @classmethod
    def _sane_enrich(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip()[:120] for item in value if item.strip()]
        return cleaned[:8]


class JudgeBatch(BaseModel):
    """A night's judging in one call per batch of candidates (spec §6.2: ~20 calls)."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[JudgeVerdict] = Field(default_factory=list)

    def by_phrase(self) -> dict[str, JudgeVerdict]:
        return {verdict.phrase: verdict for verdict in self.verdicts}


class PlannerPlan(BaseModel):
    """The weekly planner: a search plan, not prose (spec §6.2, ~1 call)."""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    per_source_budgets: dict[str, int] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=2000)


class WriterBrief(BaseModel):
    """The nightly one-pager for a top-K candidate (spec §6.2, ~30 calls)."""

    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1, max_length=300)
    verdict: str = Field(default="", max_length=4000)
    players: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    angles: list[str] = Field(default_factory=list)
    revenue_reasoning: str = Field(default="", max_length=4000)
    quotes: list[Quote] = Field(default_factory=list)
    ungrounded: bool = False
    dropped_quotes: int = 0


class ValidationOutcome(BaseModel):
    """What happened when raw text met a schema — the ledger records this verbatim."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    ok: bool
    value: Any | None = None
    reason: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


def _extract_json(raw: str) -> Any:
    """Parse a gate's answer, tolerating a fenced code block around the JSON.

    Providers wrap JSON in ```json fences often enough that refusing it would cost a retry on
    every call; anything else (prose, multiple objects) is a violation and is reported as one.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", _FENCE_PARTS)[1] if text.count("```") >= _FENCE_PARTS else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return json.loads(text)


def parse_gate_output(raw: str, model: type[BaseModel]) -> ValidationOutcome:
    """Validate one gate answer. Never raises: the reason is what the caller logs."""
    try:
        payload = _extract_json(raw)
    except (json.JSONDecodeError, IndexError) as exc:
        return ValidationOutcome(ok=False, reason=f"not JSON: {exc}")
    try:
        return ValidationOutcome(ok=True, value=model.model_validate(payload))
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first["loc"]) or "?"
            detail = str(first["msg"])
        else:
            location, detail = "?", "invalid payload"
        return ValidationOutcome(ok=False, reason=f"schema violation at {location}: {detail}")


def enforce_grounding(
    verdict: JudgeVerdict | WriterBrief | JudgeBatch,
    *,
    evidence_urls: Iterable[str] = (),
) -> tuple[JudgeVerdict | WriterBrief | JudgeBatch, int]:
    """Strip every claim that cannot be traced to a URL the pipeline actually collected.

    Returns the cleaned object and how many quotes were removed. An object whose quotes are all
    stripped is marked ``ungrounded`` rather than discarded: the *judgement* is still a
    judgement, but nothing downstream may present it as cited fact, and the flag is what tells
    the brief renderer to say "no citation" instead of inventing confidence.

    ``evidence_urls`` empty means "no URLs available to check against" — quotes are then kept,
    because the alternative (stripping everything) would silently turn a data gap into a
    grounding failure. That case is visible: the caller passes the candidate's URLs.
    """
    # A BATCH must be grounded verdict by verdict. The first implementation checked for a
    # `.quotes` attribute, which a `JudgeBatch` does not have — so the rule silently did nothing
    # on the exact path the Judge gate uses, and every invented citation in a nightly batch would
    # have been stored as fact. Found by the test that asserts an ungrounded verdict is marked.
    if hasattr(verdict, "verdicts"):
        cleaned_items: list[JudgeVerdict] = []
        total_dropped = 0
        for item in verdict.verdicts:
            cleaned_item, dropped = enforce_grounding(item, evidence_urls=evidence_urls)
            if isinstance(cleaned_item, JudgeVerdict):
                cleaned_items.append(cleaned_item)
            total_dropped += dropped
        batch = verdict.model_copy(update={"verdicts": cleaned_items})
        return batch, total_dropped

    allowed = {url.strip() for url in evidence_urls if url and url.strip()}
    kept: list[Quote] = []
    dropped = 0
    for quote in getattr(verdict, "quotes", []):
        if not quote.text.strip():
            dropped += 1
            continue
        if allowed and quote.url not in allowed:
            dropped += 1
            continue
        kept.append(quote)

    cleaned = verdict.model_copy(
        update={"quotes": kept, "dropped_quotes": dropped, "ungrounded": not kept}
    )
    return cleaned, dropped
