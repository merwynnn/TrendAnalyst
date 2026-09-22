"""SearchAPI — `searchapi` (Tier A, L2): cross-channel shopping validation.

Tier-A sources run **only in L2**, on demand, for the top-K candidates the Judge kept.
This plugin asks Google (via SearchAPI.io) for one candidate phrase and reads the
shopping shelf: how many listings compete, at what prices, with what ratings — the
cross-channel check on what eBay says, from the search side instead of the marketplace
side.

One request per phrase (the registry allows five a day, so L2 can enrich five
candidates a night). The answer is aggregated into ONE signal per phrase: the lake
stores facts, not raw SERPs, and a hundred shopping rows per candidate would drown the
velocity series it feeds.

Credentials: a SearchAPI.io key in `tier_a.searchapi_key`, sent as the `api_key`
query parameter (their API design, not ours). Without it the plugin raises
`MissingCredentialError`, so an unconfigured L2 run degrades with a reason instead of
reporting zero competition. Test responses are hand-written and labelled (the eBay
precedent): recording a real response would commit the key, which travels in the URL.
"""

from __future__ import annotations

import json
import statistics
from typing import Any, ClassVar, Final, Literal
from urllib.parse import quote_plus

from trend_analyst.sources.base import (
    HTTP_CLIENT_ERROR,
    FetchContext,
    MissingCredentialError,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)

__all__ = ["MissingCredentialError", "SearchApiPlugin"]

API: Final = "https://www.searchapi.io/api/v1/search"
SHOPPING_URL: Final = "https://www.google.com/search?q={query}&udm=28"


def _secret(value: Any) -> str:
    """Read a credential whether it arrives as a `SecretStr`, a string, or nothing."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    text = str(getter() if callable(getter) else value)
    return "" if text in {"", "SET_ME", "None"} else text


class SearchApiPlugin(SourcePlugin):
    """Search one phrase and report its Google shopping shelf."""

    id: ClassVar[str] = "searchapi"
    tier: ClassVar[Literal["A"]] = "A"
    layers: ClassVar[tuple[Literal["L2"], ...]] = ("L2",)
    schedule: ClassVar[Literal["on_demand"]] = "on_demand"
    budget_per_day: ClassVar[int] = 5
    rps: ClassVar[float] = 0.5
    domains: ClassVar[tuple[str, ...]] = ("www.searchapi.io",)

    def __init__(self, *, settings: Any = None) -> None:
        self._settings = settings

    def _api_key(self) -> str:
        section = getattr(self._settings, "tier_a", None)
        key = _secret(getattr(section, "searchapi_key", None))
        if not key:
            raise MissingCredentialError(
                "tier_a.searchapi_key is not set: L2 cannot validate the shopping shelf"
            )
        return key

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        phrase = (ctx.cursor or "").strip()
        if not phrase:
            raise SourceSkippedError("no enrichment phrase: L2 passes the candidate in the cursor")
        api_key = self._api_key()
        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(f"budget refused the search request ({ctx.budget.snapshot()})")
        response = ctx.client.get(
            API,
            params={"engine": "google", "q": phrase, "num": "10", "api_key": api_key},
        )
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise SourceSkippedError(f"search query returned HTTP {response.status_code}")
        return RawBatch.from_parts(
            source_id=self.id,
            parts=[response.text],
            fetched_at=now,
            cursor=phrase,
            status_codes=(response.status_code,),
            request_count=1,
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            try:
                payload: Any = json.loads(part)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            # The phrase searched for travels in the batch cursor — the response does not
            # echo the query — so parse re-attaches it here, where both are in hand.
            phrase = str(raw.cursor or "").strip()
            if not phrase:
                continue
            signal = _to_signal(payload, phrase=phrase, fetched_at=raw.fetched_at)
            if signal is not None:
                signals.append(signal)
        return signals


def _price_of(item: dict[str, Any]) -> float | None:
    """An item's price as a float, from the extracted field or the display string."""
    raw = item.get("extracted_price")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        cleaned = "".join(
            char for char in str(item.get("price") or "") if char.isdigit() or char == "."
        )
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None


def _to_signal(payload: dict[str, Any], *, phrase: str, fetched_at: Any) -> Signal | None:
    """One SERP into one shelf signal: listing count, price spread, ratings."""
    items = payload.get("inline_shopping")
    if not isinstance(items, list) or not items:
        return None
    prices: list[float] = []
    ratings: list[float] = []
    sellers: set[str] = set()
    top_title = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if not top_title:
            top_title = str(item.get("title") or "")
        price = _price_of(item)
        if price is not None and price >= 0:
            prices.append(price)
        try:
            rating = float(item.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        if rating > 0:
            ratings.append(rating)
        seller = str(item.get("seller") or "").strip()
        if seller:
            sellers.add(seller)
    if not prices:
        return None
    return Signal(
        source_id="searchapi",
        entity=phrase,
        metric="search_shopping",
        value=float(len(items)),
        ts=fetched_at,
        url=SHOPPING_URL.format(query=quote_plus(phrase)),
        quote=top_title[:280] or phrase,
        metadata={
            "price_min": f"{min(prices):.2f}",
            "price_median": f"{statistics.median(prices):.2f}",
            "price_max": f"{max(prices):.2f}",
            "priced_listings": str(len(prices)),
            "avg_rating": f"{(sum(ratings) / len(ratings)):.2f}" if ratings else "",
            "sellers": str(len(sellers)),
        },
    )
