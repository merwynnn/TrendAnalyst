"""Wikipedia Pageviews — `wiki_pageviews` (Tier S, L0): the absolute demand baseline.

The Wikimedia pageviews REST API needs no key and returns, for one UTC day, the 1000
most-viewed articles with their view counts. That is the closest thing to a neutral,
global "how much do people actually care" baseline, which is why it sits in L0.

The watermark is the newest day already collected, so a rerun fetches **only the days
after it** (spec §5.1). Days that are not published yet answer 404 — that is a normal
condition, not a failure, and the plugin records it and moves on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from trend_analyst.sources.base import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["WikipediaPageviewsPlugin"]

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top"
PROJECT = "en.wikipedia"
ACCESS = "all-access"
ARTICLE_URL = "https://en.wikipedia.org/wiki/{article}"

#: How many days one pass may collect, and how far back a first run reaches.
MAX_DAYS_PER_RUN = 3
CURSOR_FORMAT = "%Y-%m-%d"

#: Pages that say nothing about demand.
IGNORED_ARTICLES = frozenset({"Main_Page", "-", "Special:Search", "Wikipedia:Featured_pictures"})


def _day_path(day: datetime) -> str:
    return f"{day.year}/{day.month:02d}/{day.day:02d}"


def _parse_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        return datetime.strptime(cursor, CURSOR_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None  # an unreadable watermark is treated as "no watermark", never guessed


def days_to_fetch(*, cursor: str | None, now: datetime) -> list[datetime]:
    """Complete UTC days still missing, oldest first.

    Today is excluded: the API publishes a day only once it is over. A first run reaches
    back :data:`MAX_DAYS_PER_RUN` days; later runs start the day after the watermark.
    """
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    last_complete = today - timedelta(days=1)

    start = _parse_cursor(cursor)
    if start is None:
        start = last_complete - timedelta(days=MAX_DAYS_PER_RUN - 1)

    days: list[datetime] = []
    day = start + timedelta(days=1) if _parse_cursor(cursor) else start
    while day <= last_complete and len(days) < MAX_DAYS_PER_RUN:
        days.append(day)
        day += timedelta(days=1)
    return days


class WikipediaPageviewsPlugin(SourcePlugin):
    """Tier-S collector for Wikipedia pageviews."""

    id: ClassVar[str] = "wiki_pageviews"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "nightly"
    budget_per_day: ClassVar[int] = 2000
    rps: ClassVar[float] = 4
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("wikimedia.org",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        days = days_to_fetch(cursor=ctx.cursor, now=now)
        if not days:
            return RawBatch.from_parts(
                source_id=self.id,
                parts=[],
                fetched_at=now,
                cursor=ctx.cursor,
                not_modified=True,
            )

        parts: list[str] = []
        statuses: list[int] = []
        newest = ctx.cursor
        for day in days:
            if ctx.budget.acquire() != "ok":
                break
            url = f"{API}/{PROJECT}/{ACCESS}/{_day_path(day)}"
            response = ctx.client.get(url)
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code == HTTP_NOT_FOUND:
                continue  # not published yet; try again tomorrow
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)
            newest = day.strftime(CURSOR_FORMAT)

        if not parts and not statuses:
            raise SourceSkippedError(
                f"no pageview day could be requested ({ctx.budget.snapshot()})"
            )

        return RawBatch.from_parts(
            source_id=self.id,
            parts=parts,
            fetched_at=now,
            cursor=newest,
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
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                ts = _item_timestamp(item)
                articles = item.get("articles")
                if ts is None or not isinstance(articles, list):
                    continue
                for article in articles:
                    if not isinstance(article, dict):
                        continue
                    name = str(article.get("article") or "").strip()
                    if not name or name in IGNORED_ARTICLES:
                        continue
                    signals.append(
                        Signal(
                            source_id=self.id,
                            entity=name.replace("_", " "),
                            metric="wiki_pageviews_top",
                            value=float(article.get("views") or 0),
                            ts=ts,
                            url=ARTICLE_URL.format(article=name),
                            quote=name,
                            metadata={
                                "rank": str(article.get("rank") or ""),
                                "project": str(item.get("project") or PROJECT),
                                "access": str(item.get("access") or ACCESS),
                            },
                        )
                    )
        return signals


def _item_timestamp(item: dict[str, Any]) -> datetime | None:
    """The UTC midnight the item's day starts at, from the API's own date fields."""
    try:
        return datetime(
            int(item["year"]), int(item["month"]), int(item["day"]), tzinfo=UTC
        )
    except (KeyError, TypeError, ValueError):
        return None
