"""Shopify storefronts — `shopify_public` (Tier S, L0): DTC and app-store gaps.

Public `products.json` endpoints need no key: one request per shop answers up to 250
products with titles, prices, vendors and types. Five DTC shops with verified-open
endpoints (probed September 2026 — most storefronts answer 401/404 because they
disabled the endpoint, so the list names only shops that returned 200).

The signal is the price: a cross-shop price ladder for everyday DTC goods, which is
the Money sub-score's first observed input rather than another category prior.
Catalogs move slowly, so a rerun usually refetches identical payloads and the
content-hash dedup (spec §5.2) skips parsing, scoring and the LLM. The cursor is the
run's date: a catalog is a snapshot, not a stream.

Bodies are HTML (`body_html`), stripped with a regex — markup in a quote is noise, and
the quote is what briefs print.
"""

from __future__ import annotations

import html
import json
import re
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

__all__ = ["ShopifyPublicPlugin"]

#: Shops whose products.json answered 200 when probed (September 2026). Most DTC
#: storefronts answer 401/404 — the endpoint is opt-out — so this list is verified,
#: not aspirational. A shop that closes its endpoint is skipped loudly, not silently.
SHOPS: Final[tuple[str, ...]] = (
    "gymshark",
    "ruggable",
    "bombas",
    "puravidabracelets",
    "diffeyewear",
)

#: products.json answers at most 250 products per request.
MAX_PER_SHOP: Final = 250
#: Quote length: the title stays the entity; the stripped body is what briefs print.
QUOTE_CHARS: Final = 280

_TAG_STRIP: Final = re.compile(r"<[^>]*>")


def _plain_text(html_text: str) -> str:
    """A product body without markup or entities."""
    return " ".join(_TAG_STRIP.sub(" ", html.unescape(str(html_text or ""))).split())


def _published_at(value: str, *, fallback: datetime) -> datetime:
    """A product's publish time, or the fetch time when it does not parse."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return fallback


class ShopifyPublicPlugin(SourcePlugin):
    """Tier-S collector for public Shopify storefront catalogs."""

    id: ClassVar[str] = "shopify_public"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 1000
    rps: ClassVar[float] = 1
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("*.myshopify.com",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        limit = min(MAX_PER_SHOP, max(1, ctx.max_items or MAX_PER_SHOP))

        # One part per shop, in SHOPS order — including an empty part for a shop that
        # failed. Positional alignment is how parse() knows which shop a payload came
        # from; without it one closed endpoint would misattribute every shop after it.
        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        for shop in SHOPS:
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # a partial shelf is still useful; the log records the spend
            url = f"https://{shop}.myshopify.com/products.json"
            response = ctx.client.get(url, params={"limit": str(limit)})
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(response.status_code)
            # A closed or moved endpoint leaves an empty part: loud in the status codes,
            # unparsed downstream, and never shifting the shops behind it.
            parts.append(response.text if response.status_code == HTTP_OK else "")

        if not statuses:
            if refused:
                raise SourceSkippedError(
                    f"budget refused the shop catalogs ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no shop catalog could be requested ({ctx.budget.snapshot()})"
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
        for index, part in enumerate(raw.parts):
            try:
                payload: Any = json.loads(part)
            except ValueError:
                continue
            products = payload.get("products") if isinstance(payload, dict) else None
            if not isinstance(products, list):
                continue
            for product in products:
                if not isinstance(product, dict):
                    continue
                signal = _to_signal(product, shop=SHOPS[index] if index < len(SHOPS) else "?",
                                    fetched_at=raw.fetched_at)
                if signal is not None:
                    signals.append(signal)
        return signals


def _to_signal(product: dict[str, Any], *, shop: str, fetched_at: datetime) -> Signal | None:
    """One catalog product into a price signal. None for unpriced or untitled rows."""
    title = _plain_text(str(product.get("title") or ""))
    handle = str(product.get("handle") or "").strip()
    variants = product.get("variants")
    price_raw = variants[0].get("price") if isinstance(variants, list) and variants else None
    try:
        price = float(price_raw) if price_raw is not None else -1.0
    except (TypeError, ValueError):
        return None
    if not title or not handle or price < 0:
        return None
    blurb = _plain_text(str(product.get("body_html") or ""))
    return Signal(
        source_id="shopify_public",
        entity=title,
        metric="shopify_price",
        value=price,
        ts=_published_at(str(product.get("published_at") or ""), fallback=fetched_at),
        url=f"https://{shop}.myshopify.com/products/{handle}",
        quote=blurb[:QUOTE_CHARS] or title,
        metadata={
            "shop": shop,
            "vendor": str(product.get("vendor") or ""),
            "product_type": str(product.get("product_type") or ""),
            "currency": "USD",
        },
    )
