"""Arctic Shift — `arctic_shift` (Tier S, L0): Reddit pain mining.

Arctic Shift is the keyless archive of Reddit (spec Appendix A #2). Reddit's own API is
dropped for v1 — approval plus a commercial contract — so this is how the pipeline gets at
the thing it exists for: what people are complaining about, in their own words.

One request per subreddit, recent posts only. The watermark is the newest post timestamp
already collected, and it is passed back to the API as ``after=``, so a rerun asks only for
what appeared since (spec §5.1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from trend_analyst.sources.base import (
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["ArcticShiftPlugin"]

API = "https://arctic-shift.photon-reddit.com/api/posts/search"
POST_URL = "https://www.reddit.com{permalink}"
CURSOR_FORMAT = "%Y-%m-%d"

#: Pain-mining subreddits: places where people describe what they wish existed, what broke,
#: and what they would pay for. Chosen for signal, not for size.
PAIN_SUBS: tuple[str, ...] = (
    "SomebodyMakeThis",
    "INEEEEDIT",
    "AppIdeas",
    "smallbusiness",
    "Entrepreneur",
    "BuyItForLife",
    "homeautomation",
    "3Dprinting",
    "MechanicalKeyboards",
    "espresso",
    "woodworking",
    "HVAC",
    "gardening",
    "Etsy",
    "dropship",
)

POSTS_PER_SUB = 100
#: How far back a first run reaches, so the lake starts with something to mine.
FIRST_RUN_LOOKBACK_DAYS = 7


def _cursor_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, CURSOR_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _post_timestamp(post: dict[str, Any]) -> datetime | None:
    raw = post.get("created_utc")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (TypeError, ValueError):
        return None


class ArcticShiftPlugin(SourcePlugin):
    """Tier-S collector for the Arctic Shift Reddit archive."""

    id: ClassVar[str] = "arctic_shift"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "nightly"
    budget_per_day: ClassVar[int] = 3000
    rps: ClassVar[float] = 2
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("arctic-shift.photon-reddit.com",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        since = _cursor_date(ctx.cursor) or (now - timedelta(days=FIRST_RUN_LOOKBACK_DAYS))
        after = since.strftime(CURSOR_FORMAT)

        parts: list[str] = []
        statuses: list[int] = []
        newest = since

        limit = ctx.max_items or POSTS_PER_SUB
        for subreddit in PAIN_SUBS:
            if ctx.budget.acquire() != "ok":
                break  # a partial sweep is still useful; the ledger records what was spent
            response = ctx.client.get(
                API,
                params={
                    "subreddit": subreddit,
                    "limit": str(limit),
                    "sort": "desc",
                    "after": after,
                },
            )
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)
            for post in _posts_of(response.text):
                posted = _post_timestamp(post)
                if posted is not None and posted > newest:
                    newest = posted

        if not statuses:
            raise SourceSkippedError(f"no subreddit could be requested ({ctx.budget.snapshot()})")

        return RawBatch.from_parts(
            source_id=self.id,
            parts=parts,
            fetched_at=now,
            cursor=newest.strftime(CURSOR_FORMAT),
            status_codes=tuple(statuses),
            request_count=len(statuses),
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for post in (post for part in raw.parts for post in _posts_of(part)):
            title = str(post.get("title") or "").strip()
            posted = _post_timestamp(post)
            if not title or posted is None:
                continue
            permalink = str(post.get("permalink") or "")
            selftext = str(post.get("selftext") or "").strip()
            signals.append(
                Signal(
                    source_id=self.id,
                    entity=title,
                    metric="reddit_score",
                    value=float(post.get("score") or 0),
                    ts=posted,
                    url=POST_URL.format(permalink=permalink) if permalink else None,
                    quote=(selftext[:280] or title),
                    metadata={
                        "subreddit": str(post.get("subreddit") or ""),
                        "comments": str(post.get("num_comments") or 0),
                        "post_id": str(post.get("id") or ""),
                        "author": str(post.get("author") or ""),
                    },
                )
            )
        return signals


def _posts_of(payload: str) -> list[dict[str, Any]]:
    """The posts inside one API response, tolerating the shapes the API can return."""
    try:
        data: Any = json.loads(payload)
    except ValueError:
        return []
    if isinstance(data, dict):
        posts = data.get("data")
        if isinstance(posts, list):
            return [post for post in posts if isinstance(post, dict)]
    if isinstance(data, list):
        return [post for post in data if isinstance(post, dict)]
    return []
