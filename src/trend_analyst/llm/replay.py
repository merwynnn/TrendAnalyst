"""Replaying a recorded provider answer through a gate (offline, no quota).

The live drill proves the gateway against a real provider; this proves the *gate* against a real
provider's answer, with no network and no spend. It exists because the quota is a real constraint
(LESSONS §6.5) and because a gate that can only be exercised when the provider is up is a gate that
is only tested when nobody is looking.

Where the recording stops, the replay stops: an uncovered phrase is reported as missing rather than
answered with somebody else's verdict.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trend_analyst.llm.gateway import ProviderSpec, Sender
from trend_analyst.llm.schemas import JudgeBatch, JudgeVerdict

__all__ = ["ReplayMissError", "load_fixture", "replay_sender"]


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


def replay_sender(fixture: dict[str, Any], *, covered: Sequence[str] = ()) -> Sender:
    """A `Sender` that answers from the recording, but only where the recording applies.

    The recorded answer names one phrase, and it is replayed **only in a batch that asks about
    that phrase** — the first version matched against the whole candidate list, so every batch
    received the same verdict, the batches that did not contain the phrase counted it as unknown,
    and the transcript looked like a model inventing candidates. Matching is done on the prompt,
    which is the only thing a real provider would see.

    A batch the recording does not cover gets an empty verdict list, so the gate reports those
    candidates as missing rather than answering with somebody else's verdict.
    """
    recorded = fixture["output"]
    verdict = JudgeVerdict.model_validate(recorded)
    needle = f'"{verdict.phrase}"'
    expected = {phrase.lower() for phrase in covered}

    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        # The exact JSON-quoted phrase, not a substring: an unquoted match made "blade circ saw"
        # and "circ saw it still" count as batches about "circ saw", so three batches received a
        # verdict about a candidate they did not contain.
        asks_about_it = needle in prompt
        in_scope = not expected or verdict.phrase.lower() in expected
        covered_here = asks_about_it and in_scope
        batch = JudgeBatch(verdicts=[verdict]) if covered_here else JudgeBatch(verdicts=[])
        return (
            batch.model_dump_json(),
            int(fixture.get("prompt_tokens", 0)),
            int(fixture.get("completion_tokens", 0)),
        )

    return send
