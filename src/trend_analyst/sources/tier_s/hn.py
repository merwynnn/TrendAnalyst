"""HN Firebase — `hn_firebase` (Tier S, L0): tech launches and critiques.

Two endpoints, no key:

* ``/v0/topstories.json`` — the current front-page story ids (one request).
* ``/v0/item/{id}.json``  — one story each (title, score, comment count, time).

The watermark is the id list of the last run's front page. When the front page is
byte-identical to it, the plugin reports ``not_modified`` and returns *without* fetching a
single story — which is exactly the "a rerun processes only what changed" rule of spec
§5.1, and the difference between a 1-request and a 61-request second run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar

from trend_analyst.sources.base import (
    HTTP_CLIENT_ERROR,
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
    content_hash,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["HackerNewsPlugin"]

API = "https://hacker-news.firebaseio.com/v0"
ITEM_URL = "https://news.ycombinator.com/item?id={item_id}"

#: How many front-page stories get a detail fetch. Bounded on purpose: the daily budget
#: in config/sources.yaml is 2000 requests and one nightly pass must stay far below it.
DEFAULT_ITEM_LIMIT = 60


def _story_id_list_cursor(ids: list[int]) -> str:
    """The watermark: the front page, as a stable string."""
    return ",".join(str(item) for item in ids)


class HackerNewsPlugin(SourcePlugin):
    """Tier-S collector for Hacker News."""

    id: ClassVar[str] = "hn_firebase"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "nightly"
    budget_per_day: ClassVar[int] = 2000
    rps: ClassVar[float] = 5
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("hacker-news.firebaseio.com", "news.ycombinator.com")

    def fetch(self, ctx: FetchContext) -> RawBatch:
        limit = ctx.max_items or DEFAULT_ITEM_LIMIT

        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(
                f"budget refused the front-page request ({ctx.budget.snapshot()})"
            )
        front_page = ctx.client.get(f"{API}/topstories.json")
        ctx.budget.record_response(front_page.status_code, retry_after_s=front_page.retry_after_s)
        if front_page.status_code >= HTTP_CLIENT_ERROR:
            raise SourceSkippedError(f"topstories returned HTTP {front_page.status_code}")

        try:
            ids = [int(item) for item in json.loads(front_page.text)]
        except (ValueError, TypeError) as exc:
            raise SourceSkippedError(f"topstories was not a list of ids: {exc}") from exc

        cursor = _story_id_list_cursor(ids)
        if ctx.cursor is not None and ctx.cursor == cursor:
            return RawBatch.from_parts(
                source_id=self.id,
                parts=[],
                fetched_at=ctx.clock.now(),
                cursor=cursor,
                status_codes=(front_page.status_code,),
                request_count=1,
                not_modified=True,
            )

        parts: list[str] = []
        statuses: list[int] = [front_page.status_code]
        for item_id in ids[:limit]:
            if ctx.budget.acquire() != "ok":
                break  # a partial front page is still useful; the ledger records the spend
            response = ctx.client.get(f"{API}/item/{item_id}.json")
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code == HTTP_OK and response.text.strip() not in {"", "null"}:
                parts.append(response.text)

        return RawBatch.from_parts(
            source_id=self.id,
            parts=parts,
            fetched_at=ctx.clock.now(),
            cursor=cursor,
            status_codes=tuple(statuses),
            request_count=len(statuses),
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            try:
                story: Any = json.loads(part)
            except ValueError:
                continue  # a malformed item is skipped, never guessed at
            if not isinstance(story, dict) or story.get("type") != "story":
                continue
            title = str(story.get("title") or "").strip()
            item_id = story.get("id")
            posted_at = story.get("time")
            if not title or item_id is None or posted_at is None:
                continue

            signals.append(
                Signal(
                    source_id=self.id,
                    entity=title,
                    metric="hn_score",
                    value=float(story.get("score") or 0),
                    ts=datetime.fromtimestamp(float(posted_at), tz=UTC),
                    url=ITEM_URL.format(item_id=item_id),
                    quote=title,
                    metadata={
                        "hn_id": str(item_id),
                        "comments": str(story.get("descendants") or 0),
                        "author": str(story.get("by") or ""),
                        "outbound": str(story.get("url") or ""),
                    },
                )
            )
        return signals


def record_probe(parts: list[str]) -> str:  # pragma: no cover - helper for the recorder
    """Hash a candidate batch, used by the fixture recorder's self-check."""
    return content_hash(parts)
