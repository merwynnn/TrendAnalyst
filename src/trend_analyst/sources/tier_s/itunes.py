"""iTunes Search — `itunes_search` (Tier S, L0): app demand and ratings.

The Search API needs no key: one request per intent query answers matching apps with
the two numbers this pipeline wants — the average rating and how many people left one.
High rating counts on a mediocre average is the app-store version of the WordPress
pattern: an incumbent users cannot quit, which is buyer pain with a number on it.

Eight fixed intent queries (meditation, budgets, habits — the solo-shippable app
categories), software entities only, twenty-five results each. The throttle is polite
on purpose (registry: a request every three seconds); eight requests take half a
minute and the weekly schedule keeps it there.

Two signals per app: the average rating (0-5) and its rating count. The timestamp is
the current version's release date — a freshly-updated app is alive, an abandoned one
is not — falling back to first release, then to fetch time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from trend_analyst.sources.base import (
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["ITunesSearchPlugin"]

API = "https://itunes.apple.com/search"

#: Intent queries in solo-shippable app categories. Fixed, not mined: search needs a
#: starting vocabulary from somewhere, and these are the jobs users hire indie apps for.
QUERIES: Final[tuple[str, ...]] = (
    "meditation",
    "budget tracker",
    "habit tracker",
    "sleep sounds",
    "workout planner",
    "notes",
    "pomodoro timer",
    "language learning",
)

#: Results per query. The API answers up to 200; twenty-five is plenty for the tail.
DEFAULT_LIMIT: Final = 25
#: Quote length: the description's opening, not its marketing tail.
QUOTE_CHARS: Final = 280


def _released_at(value: str, *, fallback: datetime) -> datetime:
    """Current-version release date, else first release, else fetch time."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return fallback


class ITunesSearchPlugin(SourcePlugin):
    """Tier-S collector for iTunes app search."""

    id: ClassVar[str] = "itunes_search"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 500
    rps: ClassVar[float] = 0.33
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("itunes.apple.com",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        limit = min(200, max(1, ctx.max_items or DEFAULT_LIMIT))

        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        for query in QUERIES:
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # a partial shelf is still useful; the log records the spend
            response = ctx.client.get(
                API,
                params={"term": query, "entity": "software", "limit": str(limit)},
            )
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)

        if not statuses:
            if refused:
                raise SourceSkippedError(
                    f"budget refused the app searches ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no app search could be requested ({ctx.budget.snapshot()})"
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
            try:
                payload: Any = json.loads(part)
            except ValueError:
                continue
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                continue
            for app in results:
                if not isinstance(app, dict):
                    continue
                signals.extend(_to_signals(app, fetched_at=raw.fetched_at))
        return signals


def _to_signals(app: dict[str, Any], *, fetched_at: datetime) -> list[Signal]:
    """One app into a rating signal and a rating-count signal."""
    name = str(app.get("trackName") or "").strip()
    if not name:
        return []
    try:
        rating = float(app.get("averageUserRating") or 0)
        count = int(app.get("userRatingCount") or 0)
    except (TypeError, ValueError):
        return []
    ts = _released_at(
        str(app.get("currentVersionReleaseDate") or app.get("releaseDate") or ""),
        fallback=fetched_at,
    )
    url = str(app.get("trackViewUrl") or "")
    blurb = " ".join(str(app.get("description") or "").split())[:QUOTE_CHARS] or name
    base_metadata = {
        "bundle": str(app.get("bundleId") or ""),
        "genre": str(app.get("primaryGenreName") or ""),
        "price": str(app.get("price") or 0),
    }
    return [
        Signal(
            source_id="itunes_search",
            entity=name,
            metric="itunes_rating",
            value=rating,
            ts=ts,
            url=url or None,
            quote=blurb,
            metadata={**base_metadata, "kind": "rating"},
        ),
        Signal(
            source_id="itunes_search",
            entity=name,
            metric="itunes_ratings",
            value=float(count),
            ts=ts,
            url=url or None,
            quote=blurb,
            metadata={**base_metadata, "kind": "count"},
        ),
    ]
