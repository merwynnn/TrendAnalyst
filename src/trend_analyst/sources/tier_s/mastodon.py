"""Mastodon trends — `mastodon_trends` (Tier S, L0): social trend cross-check.

The instance trends API needs no key: ``trends/statuses`` answers the currently
trending public posts with their engagement counts. One request per run, up to forty
posts — a cross-check on what social media is amplifying right now, in the same shape
as the complaint-mining sources but from a different crowd.

Post bodies are HTML by construction (`<p>…</p>` with mention links), so parsing strips
tags with the standard library — no new dependency for one field. Image-only trending
posts (empty text after stripping) are skipped: a picture with no words is not minable
text, and an empty entity would violate the signal contract.

The cursor is the newest status id seen. Trends have no "nothing new" signal, so a rerun
re-asks and the content-hash dedup (spec §5.2) decides — identical payloads skip parsing,
scoring and the LLM.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html.parser import HTMLParser
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

__all__ = ["MastodonTrendsPlugin"]

API = "https://mastodon.social/api/v1/trends/statuses"

#: The API answers at most 40 statuses per call; one call is the whole pass.
MAX_PER_RUN: Final = 40
#: Quote length: the full post stays the entity; the quote is what briefs print.
QUOTE_CHARS: Final = 280


class _TextExtractor(HTMLParser):
    """Strip tags, keep text: `<p>a <a>b</a></p>` reads as "a b"."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _plain_text(html: str) -> str:
    """A status body without markup. Never raises: unparseable markup is skipped upstream."""
    parser = _TextExtractor()
    try:
        # Markup is untrusted input, not code: anything unparseable is skipped upstream.
        parser.feed(html)
    except Exception:
        return ""
    return parser.text()


class MastodonTrendsPlugin(SourcePlugin):
    """Tier-S collector for Mastodon trending statuses."""

    id: ClassVar[str] = "mastodon_trends"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "nightly"
    budget_per_day: ClassVar[int] = 500
    rps: ClassVar[float] = 1
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("mastodon.social",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        limit = min(MAX_PER_RUN, max(1, ctx.max_items or MAX_PER_RUN))

        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(
                f"budget refused the trends request ({ctx.budget.snapshot()})"
            )
        response = ctx.client.get(API, params={"limit": str(limit)})
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
        if response.status_code != HTTP_OK:
            raise SourceSkippedError(f"trends returned HTTP {response.status_code}")

        newest: str | None = ctx.cursor
        for post in _posts_of(response.text):
            post_id = str(post.get("id") or "")
            if post_id and (newest is None or post_id > newest):
                newest = post_id

        return RawBatch.from_parts(
            source_id=self.id,
            parts=[response.text],
            fetched_at=now,
            cursor=newest,
            status_codes=(response.status_code,),
            request_count=1,
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            for post in _posts_of(part):
                signal = _to_signal(post)
                if signal is not None:
                    signals.append(signal)
        return signals


def _posts_of(payload: str) -> list[dict[str, Any]]:
    """The statuses inside one trends response, tolerating an unexpected shape."""
    try:
        data: Any = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [post for post in data if isinstance(post, dict)]


def _to_signal(post: dict[str, Any]) -> Signal | None:
    """One status into one normalized fact. None for image-only or timeless posts."""
    spoiler = _plain_text(str(post.get("spoiler_text") or ""))
    body = _plain_text(str(post.get("content") or ""))
    text = f"{spoiler}: {body}".strip() if spoiler else body
    if not text:
        return None  # an image with no words is not minable text
    created = str(post.get("created_at") or "")
    try:
        posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
    except ValueError:
        return None
    account = post.get("account")
    author = str(account.get("acct") or "unknown") if isinstance(account, dict) else "unknown"
    engagement = sum(
        float(post.get(key) or 0)
        for key in ("favourites_count", "reblogs_count", "replies_count")
    )
    return Signal(
        source_id="mastodon_trends",
        entity=text,
        metric="mastodon_engagement",
        value=engagement,
        ts=posted,
        url=str(post.get("url") or None) if post.get("url") else None,
        quote=text[:QUOTE_CHARS],
        metadata={
            "author": author,
            "language": str(post.get("language") or ""),
            "reblogs": str(post.get("reblogs_count") or 0),
        },
    )
