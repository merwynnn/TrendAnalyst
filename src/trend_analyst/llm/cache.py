"""The 30-day LLM cache, keyed by a hash of the normalized input (spec §6.3, brief P3).

*"Identical work never hits a provider twice."* The key is a SHA-256 over the gate, the model
and the *normalized* input — normalized meaning whitespace-collapsed and stable-ordered, so two
spellings of the same question share an answer instead of paying for it twice.

The cache is also the token log: every row carries its prompt and completion token counts, so
"spend per gate" is a query rather than a claim. TTL is enforced on read (an expired row is a
miss) and swept by `purge_expired`, which P4's TTL job calls alongside the raw-lake expiry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_analyst.llm.schemas import GateName
from trend_analyst.store.models import LLMCache

__all__ = [
    "DEFAULT_TTL_DAYS",
    "cache_key",
    "get_cached",
    "normalize_input",
    "purge_expired",
    "put_cached",
    "token_spend_by_gate",
]

#: Spec §6.3: "LLM cache keyed by hash of normalized input, 30-day TTL".
DEFAULT_TTL_DAYS: Final = 30


def normalize_input(payload: Mapping[str, Any] | Sequence[Any] | str) -> str:
    """A canonical string for hashing: sorted keys, collapsed whitespace, no float noise."""
    if isinstance(payload, str):
        canonical: Any = " ".join(payload.split())
    elif isinstance(payload, Mapping):
        canonical = {str(key): _clean(value) for key, value in sorted(payload.items())}
    else:
        canonical = [_clean(item) for item in payload]
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _clean(value: Any) -> Any:
    """Recursively normalize: strings lose whitespace runs, floats lose representation drift."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def cache_key(gate: GateName | str, model: str, payload: Mapping[str, Any] | str) -> str:
    """The cache key: gate + model + normalized input, hashed.

    The model is part of the key on purpose — an answer produced by Gemini is not an answer
    produced by the local fallback, and the brief's failover chain would otherwise mix them.
    """
    material = f"{gate}|{model}|{normalize_input(payload)}"
    return hashlib.sha256(material.encode()).hexdigest()


def get_cached(
    session: Session,
    key: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The cached output for ``key``, or None when absent or expired.

    Expiry is checked on read as well as being sweepable: a TTL job that has not run yet must
    not be able to serve a stale answer.
    """
    reference = now or datetime.now(UTC)
    row = session.get(LLMCache, key)
    if row is None:
        return None
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires_at <= reference:
        return None
    return dict(row.output)


def put_cached(
    session: Session,
    key: str,
    *,
    gate: GateName | str,
    model: str,
    output: Mapping[str, Any],
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    now: datetime | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> None:
    """Store an answer. First write wins: a race between two identical calls is not an error."""
    reference = now or datetime.now(UTC)
    statement = pg_insert(LLMCache).values(
        cache_key=key,
        gate=str(gate),
        model=model,
        output=dict(output),
        created_at=reference,
        expires_at=reference + timedelta(days=ttl_days),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    session.execute(statement.on_conflict_do_nothing(index_elements=["cache_key"]))
    session.flush()


def token_spend_by_gate(session: Session) -> dict[str, dict[str, int]]:
    """Tokens and rows per gate — the brief's "token log shows spend per gate"."""
    rows = session.execute(
        select(
            LLMCache.gate,
            func.count().label("answers"),
            func.coalesce(func.sum(LLMCache.prompt_tokens), 0).label("prompt"),
            func.coalesce(func.sum(LLMCache.completion_tokens), 0).label("completion"),
        ).group_by(LLMCache.gate)
    ).all()
    return {
        str(row.gate): {
            "answers": int(row.answers),
            "prompt_tokens": int(row.prompt),
            "completion_tokens": int(row.completion),
            "total_tokens": int(row.prompt) + int(row.completion),
        }
        for row in rows
    }


def purge_expired(session: Session, *, now: datetime | None = None) -> int:
    """Delete expired rows. Returns how many went — the TTL job's evidence."""
    reference = now or datetime.now(UTC)
    result = session.execute(delete(LLMCache).where(LLMCache.expires_at <= reference))
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
