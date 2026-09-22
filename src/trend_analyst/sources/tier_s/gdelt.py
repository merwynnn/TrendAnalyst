"""GDELT DOC — `gdelt_doc` (Tier S, L0): news-cycle validation.

The DOC API needs no key: one request per query answers matching news articles with
titles, URLs, dates and outlet domains. Three launch-flavored queries sweep product
news; the signal is presence itself — a product the press writes about is validated
demand, and one nobody writes about is not disproven, just unmentioned.

Politeness is load-bearing, not decorative: the API answers 429 with "limit requests
to one every 5 seconds", so the registry paces this source at 0.2 rps (the spec
appendix said 1 rps; the endpoint overrules it). Three queries at five-second gaps
take under half a minute a night.

Articles carry no scores, so each is one unit of attention. The cursor is the run's
date: news search is a window, not a stream, and identical windows dedup by hash.
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

__all__ = ["GdeltDocPlugin"]

API = "https://api.gdeltproject.org/api/v2/doc/doc"

#: Launch-flavored queries: product news, not headlines.
QUERIES: Final[tuple[str, ...]] = (
    "product launch",
    "startup launch",
    "crowdfunding campaign",
)

#: Articles per query. Ten keeps the response small and the night quiet.
MAX_RECORDS: Final = 10


def _seen_at(value: str, *, fallback: datetime) -> datetime:
    """A GDELT `seendate` (`20260921T120000Z`), or the fetch time."""
    try:
        parsed = datetime.strptime(str(value), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return fallback


class GdeltDocPlugin(SourcePlugin):
    """Tier-S collector for GDELT news articles."""

    id: ClassVar[str] = "gdelt_doc"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "nightly"
    budget_per_day: ClassVar[int] = 500
    rps: ClassVar[float] = 0.2
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("api.gdeltproject.org",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        limit = min(MAX_RECORDS, max(1, ctx.max_items or MAX_RECORDS))

        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        for query in QUERIES:
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # a partial sweep is still useful; the log records the spend
            response = ctx.client.get(
                API,
                params={
                    "query": query,
                    "mode": "artlist",
                    "maxrecords": str(limit),
                    "format": "json",
                },
            )
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)

        if not statuses:
            if refused:
                raise SourceSkippedError(
                    f"budget refused the news queries ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no news query could be requested ({ctx.budget.snapshot()})"
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
            articles = payload.get("articles") if isinstance(payload, dict) else None
            if not isinstance(articles, list):
                continue
            for article in articles:
                if not isinstance(article, dict):
                    continue
                signal = _to_signal(article, fetched_at=raw.fetched_at)
                if signal is not None:
                    signals.append(signal)
        return signals


def _to_signal(article: dict[str, Any], *, fetched_at: datetime) -> Signal | None:
    """One article into one unit of attention. None for untitled rows."""
    title = str(article.get("title") or "").strip()
    url = str(article.get("url") or "").strip()
    if not title or not url:
        return None
    return Signal(
        source_id="gdelt_doc",
        entity=title,
        metric="gdelt_articles",
        value=1.0,
        ts=_seen_at(str(article.get("seendate") or ""), fallback=fetched_at),
        url=url if url.startswith(("http://", "https://")) else None,
        quote=title,
        metadata={
            "domain": str(article.get("domain") or ""),
            "language": str(article.get("language") or ""),
            "country": str(article.get("sourcecountry") or ""),
        },
    )
