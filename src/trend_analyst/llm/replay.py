"""Replaying a recorded provider answer through a gate (offline, no quota).

The live drill proves the gateway against a real provider; this proves the *gate* against a real
provider's answer, with no network and no spend. It exists because the quota is a real constraint
(LESSONS §6.5) and because a gate that can only be exercised when the provider is up is a gate that
is only tested when nobody is looking.

Where the recording stops, the replay stops: a batch the recording does not cover is answered with
an empty verdict list, so the gate records the miss instead of applying somebody else's verdict.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from trend_analyst.llm.gateway import ProviderSpec, Sender
from trend_analyst.llm.schemas import ExtractorOutput, JudgeBatch, JudgeVerdict, WriterBrief

__all__ = ["ReplayMissError", "load_fixture", "replay_sender", "schema_for_fixture"]

#: A fixture is a batch if its payload holds verdicts; otherwise it is one gate output.
_BATCH_KEY = "verdicts"
#: A fixture is an extraction if its payload holds products.
_PRODUCTS_KEY = "products"


class ReplayMissError(RuntimeError):
    """The recording does not cover what the replay needs. Loud, like an HTTP fixture miss."""


def load_fixture(path: Path) -> dict[str, Any]:
    """Read a recorded provider answer."""
    if not path.is_file():
        raise ReplayMissError(f"no recorded LLM answer at {path}")
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "output" not in payload:
        raise ReplayMissError(f"{path}: expected an object with an 'output' key")
    return dict(payload)


def schema_for_fixture(fixture: dict[str, Any], *, gate: str | None = None) -> type[BaseModel]:
    """Which schema the recording holds: a Judge batch, or a single Writer brief.

    The first version of this helper was hard-wired to `JudgeVerdict`, so a recorded *brief* failed
    to replay at all — the replay must know the shape of what it recorded, and the shape is the
    fixture's own content, not the caller's assumption.
    """
    output = fixture.get("output") or {}
    if isinstance(output, dict) and _BATCH_KEY in output:
        return JudgeBatch
    if isinstance(output, dict) and _PRODUCTS_KEY in output:
        return ExtractorOutput
    if gate == "judge" or "decision" in output:
        return JudgeVerdict
    return WriterBrief


def _recorded_phrase(value: BaseModel) -> str:
    """The candidate a recording is about, whichever gate produced it."""
    if isinstance(value, JudgeBatch):
        return str(value.verdicts[0].phrase) if value.verdicts else ""
    if isinstance(value, ExtractorOutput):
        # An extraction is about a chunk, not a candidate: prompt matching for extractor
        # fixtures is done by the caller, not by a phrase needle.
        return ""
    return str(getattr(value, "phrase", ""))


def replay_sender(
    fixture: dict[str, Any],
    *,
    schema: type[BaseModel] | None = None,
    covered: Sequence[str] = (),
) -> Sender:
    """A `Sender` that answers from the recording, but only where the recording applies.

    The recorded answer names one candidate and is replayed **only in a request that asks about that
    candidate** — an earlier version matched against the whole candidate list, so every batch
    received the same verdict, the batches that did not contain the phrase counted it as unknown,
    and the transcript looked like a model inventing candidates. Matching is done on the prompt,
    which is the only thing a real provider would see.
    """
    recorded = fixture["output"]
    chosen = schema or schema_for_fixture(fixture)
    if chosen is JudgeBatch:
        # A single recorded verdict still has to arrive as a BATCH, because that is what the Judge
        # gate asked its provider for. Returning the bare object made every model "fail validation"
        # (JudgeBatch forbids extra inputs), so the replay reported a provider outage.
        value: BaseModel = JudgeBatch.model_validate(
            recorded if _BATCH_KEY in recorded else {_BATCH_KEY: [recorded]}
        )
    else:
        value = chosen.model_validate(recorded)
    phrase = _recorded_phrase(value)
    needle = f'"{phrase}"'
    expected = {item.lower() for item in covered}
    is_batch = isinstance(value, JudgeBatch)

    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        asks_about_it = needle in prompt
        in_scope = not expected or phrase.lower() in expected
        if asks_about_it and in_scope:
            answer = value.model_dump_json()
        else:
            # Nothing the recording covers: an empty batch is a miss the gate can report, while a
            # fabricated answer would be a lie it cannot.
            answer = JudgeBatch(verdicts=[]).model_dump_json() if is_batch else "{}"
        return (
            answer,
            int(fixture.get("prompt_tokens", 0)),
            int(fixture.get("completion_tokens", 0)),
        )

    return send
