"""Autocomplete expansion — `suggest_autocomplete` (Tier S, L0): long-tail intent.

Google's suggestion endpoint needs no key: one request per seed phrase answers the ten
completions real users type most. The seeds are the ten category labels — broad enough
to cover the catalog, specific enough that the completions are shippable long tail
("standing desk" -> "standing desk frame", "standing desk mat") rather than trivia.

Amazon was dropped, not deferred: every documented completion endpoint either 404s or
answers empty lists without a real session context (probed September 2026), and faking
session ids to coax answers would be the fiction the fixture rule exists to prevent.
The registry lists only the Google host.

Suggestions are timeless — the API returns no dates — so each signal carries the fetch
time and the cursor is the run's date. A rerun re-asks the same seeds and the
content-hash dedup (spec §5.2) skips identical payloads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Final
from urllib.parse import quote_plus

from trend_analyst.sources.base import (
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["SuggestAutocompletePlugin"]

API = "https://suggestqueries.google.com/complete/search"
SEARCH_URL = "https://www.google.com/search?q={query}"

#: The Firefox answer is a two-element list: the seed, then its completions.
_SEED_AND_COMPLETIONS: Final = 2

#: Seeds: the category labels, the broadest honest starting points the pipeline owns.
SEEDS: Final[tuple[str, ...]] = (
    "standing desk",
    "coffee grinder",
    "cordless drill",
    "garden hose",
    "dog crate",
    "yoga mat",
    "usb hub",
    "circular saw",
    "baby stroller",
    "guitar pedal",
)


class SuggestAutocompletePlugin(SourcePlugin):
    """Tier-S collector for Google autocomplete expansions."""

    id: ClassVar[str] = "suggest_autocomplete"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 500
    rps: ClassVar[float] = 0.5
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("suggestqueries.google.com",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        seeds = SEEDS[: max(1, ctx.max_items or len(SEEDS))]

        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        for seed in seeds:
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # a partial expansion is still useful; the log records the spend
            response = ctx.client.get(API, params={"client": "firefox", "q": seed})
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)

        if not statuses:
            if refused:
                raise SourceSkippedError(
                    f"budget refused the suggestion queries ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no suggestion query could be requested ({ctx.budget.snapshot()})"
            )

        return RawBatch.from_parts(
            source_id=self.id,
            parts=parts,
            fetched_at=now,
            cursor=now.astimezone(UTC).date().isoformat(),
            status_codes=tuple(statuses),
            request_count=len(statuses),
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            signals.extend(_to_signals(part, fetched_at=raw.fetched_at))
        return signals


def _to_signals(payload: str, *, fetched_at: datetime) -> list[Signal]:
    """One suggestion response into ranked completion signals.

    The Firefox client answers `["seed", ["completion", ...], ...]`; anything else is
    skipped rather than guessed at. Rank is the signal: the first completion is what
    most users type, so it scores highest.
    """
    try:
        data: Any = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(data, list) or len(data) < _SEED_AND_COMPLETIONS:
        return []
    completions = data[1]
    if not isinstance(completions, list):
        return []
    seed = str(data[0])
    signals: list[Signal] = []
    for rank, completion in enumerate(completions):
        text = str(completion or "").strip()
        if not text:
            continue
        signals.append(
            Signal(
                source_id="suggest_autocomplete",
                entity=text,
                metric="google_suggest",
                value=float(max(1, 10 - rank)),
                ts=fetched_at,
                url=SEARCH_URL.format(query=quote_plus(text)),
                quote=text,
                metadata={"seed": seed, "rank": str(rank + 1)},
            )
        )
    return signals
