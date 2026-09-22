"""itch.io feeds — `itch_io` (Tier S, L0): pre-Steam experiments.

Two public RSS feeds need no key: featured games and the new-and-popular list. Items
carry structured fields most feeds do not bother with — a plain title, a price with
currency, a link, a description and publication dates — so parsing is XML element
reads, not text scraping.

The signal is the price: what the pre-Steam market charges, from free experiments to
ten-dollar niches. A free game with a real following is the earliest demand signal
this pipeline reads; a priced one feeds the Money sub-score observed data instead of
priors.

The cursor is the newest publication date seen. Feeds reorder slowly, so a rerun
usually refetches identical payloads and the content-hash dedup (spec §5.2) skips
parsing, scoring and the LLM.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar, Final
from xml.etree import ElementTree

from trend_analyst.sources.base import (
    HTTP_OK,
    FetchContext,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.registry import Schedule, SourceLayer, Tier

__all__ = ["ItchIoPlugin"]

#: Two feeds: the curated shelf and the popularity list. Both are plain RSS 2.0.
FEEDS: Final[tuple[str, ...]] = (
    "https://itch.io/feed/featured.xml",
    "https://itch.io/games/new-and-popular.xml",
)

#: Quote length: the description's opening, not its feature list.
QUOTE_CHARS: Final = 280

_TAG_STRIP: Final = re.compile(r"<[^>]*>")


def _published_at(value: str, *, fallback: datetime) -> datetime:
    """An item's publication date (RFC 2822), or the fetch time."""
    try:
        parsed = parsedate_to_datetime(str(value or ""))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return fallback


def _text(item: ElementTree.Element, tag: str) -> str:
    """An item child's text, stripped of markup — missing tags read as empty, never crash."""
    found = item.find(tag)
    if found is None or found.text is None:
        return ""
    return " ".join(_TAG_STRIP.sub(" ", found.text).split())


class ItchIoPlugin(SourcePlugin):
    """Tier-S collector for itch.io game feeds."""

    id: ClassVar[str] = "itch_io"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 200
    rps: ClassVar[float] = 0.5
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("itch.io",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()

        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        newest: datetime | None = None
        for url in FEEDS:
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # one feed is still useful; the log records the spend
            response = ctx.client.get(url)
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            if response.status_code != HTTP_OK:
                continue
            parts.append(response.text)
            for item in _items_of(response.text):
                published = _published_at(_text(item, "pubDate"), fallback=now)
                if newest is None or published > newest:
                    newest = published

        if not statuses:
            if refused:
                raise SourceSkippedError(
                    f"budget refused the game feeds ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no game feed could be requested ({ctx.budget.snapshot()})"
            )

        cursor = ctx.cursor
        if newest is not None:
            cursor = newest.astimezone(UTC).isoformat()
        return RawBatch.from_parts(
            source_id=self.id,
            parts=parts,
            fetched_at=now,
            cursor=cursor,
            status_codes=tuple(statuses),
            request_count=len(statuses),
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            for item in _items_of(part):
                signal = _to_signal(item, fetched_at=raw.fetched_at)
                if signal is not None:
                    signals.append(signal)
        return signals


def _items_of(payload: str) -> list[ElementTree.Element]:
    """The feed's items, tolerating malformed XML by returning nothing."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return []
    return list(root.iter("item"))


def _to_signal(item: ElementTree.Element, *, fetched_at: datetime) -> Signal | None:
    """One feed item into a price signal. None for untitled rows."""
    title = _text(item, "plainTitle") or _text(item, "title").split("[")[0].strip()
    link = _text(item, "link")
    if not title or not link:
        return None
    try:
        price = float(_text(item, "price").lstrip("$") or 0)
    except ValueError:
        return None
    description = _text(item, "description")
    return Signal(
        source_id="itch_io",
        entity=title,
        metric="itch_price",
        value=max(0.0, price),
        ts=_published_at(_text(item, "pubDate"), fallback=fetched_at),
        url=link if link.startswith(("http://", "https://")) else None,
        quote=description[:QUOTE_CHARS] or title,
        metadata={
            "price": f"{price:.2f}",
            "currency": _text(item, "currency") or "USD",
        },
    )
