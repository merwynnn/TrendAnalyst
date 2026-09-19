"""eBay Browse — the Tier-A plugin that measures supply and price (spec §4.2, brief P4).

Tier-A sources are the scarce ones: they run **only in L2**, on demand, for the top-K candidates the
Judge kept. This plugin answers two of the five MGS questions with observed data instead of a prior:

* ``SS`` — **saturation**: how many active listings compete for the phrase today;
* ``MP`` — **money**: the observed price distribution, which replaces the category price band.

**What it cannot do, stated plainly.** The obvious "sold versus listed" ratio needs eBay's
Marketplace Insights API, which eBay restricts and has deprecated; Browse does not expose sold
counts. So this plugin reports *active supply and prices*, and the sold side stays a gap — recorded
here and in the evidence rather than inferred, because a saturation proxy that quietly pretends to
be
a sold count is exactly the number this project refuses to invent.

Credentials: OAuth2 client-credentials against ``api.ebay.com`` (the registry allows that host, and
the plugin never widens it). Without a client id and secret the plugin raises
`MissingCredentialError`, so an unconfigured L2 run degrades with a reason instead of reporting zero
demand.
"""

from __future__ import annotations

import base64
import json
import statistics
from collections.abc import Sequence
from typing import Any, ClassVar, Final, Literal

from trend_analyst.sources.base import (
    HTTP_CLIENT_ERROR,
    FetchContext,
    MissingCredentialError,
    RawBatch,
    Signal,
    SourcePlugin,
    SourceSkippedError,
)

__all__ = ["EbayBrowsePlugin", "MissingCredentialError"]


def _payload_of(part: str) -> dict[str, Any]:
    """Decode one stored payload. A part is JSON text, and a malformed one is skipped, not fatal."""
    try:
        payload = json.loads(part)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}

TOKEN_URL: Final = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL: Final = "https://api.ebay.com/buy/browse/v1/item_summary/search"
#: eBay's own page size ceiling for the Browse search endpoint.
PAGE_SIZE: Final = 50
#: Pages walked per candidate: enough for a price spread without spending a day's budget on one
#: phrase. The Judge's `enrich` list decides *which* phrases are worth these calls.
MAX_PAGES: Final = 2


def _secret(value: Any) -> str:
    """Read a credential whether it arrives as a `SecretStr`, a string, or nothing."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    text = str(getter() if callable(getter) else value)
    return "" if text in {"", "SET_ME", "None"} else text


class EbayBrowsePlugin(SourcePlugin):
    """Search eBay for a phrase and report how much competes with it, at what prices."""

    id: ClassVar[str] = "ebay_browse"
    tier: ClassVar[Literal["A"]] = "A"
    layers: ClassVar[tuple[Literal["L2"], ...]] = ("L2",)
    schedule: ClassVar[Literal["on_demand"]] = "on_demand"
    budget_per_day: ClassVar[int] = 5000
    rps: ClassVar[float] = 1.0
    domains: ClassVar[tuple[str, ...]] = ("api.ebay.com",)

    METRIC: ClassVar[str] = "ebay_active_listings"

    def __init__(self, *, settings: Any = None, market: str = "EBAY_US") -> None:
        self._settings = settings
        self._market = market
        #: Cached app token for the life of one run: an OAuth dance per candidate would add one
        #: request per phrase for no benefit, and the token is valid for two hours.
        self._token: str | None = None

    # -- credentials -------------------------------------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        section = getattr(self._settings, "tier_a", None)
        client_id = _secret(getattr(section, "ebay_client_id", None))
        client_secret = _secret(getattr(section, "ebay_client_secret", None))
        if not client_id or not client_secret:
            raise MissingCredentialError(
                "ebay_client_id/ebay_client_secret are not set: L2 cannot measure supply or price"
            )
        return client_id, client_secret

    def _app_token(self, ctx: FetchContext) -> str:
        """One OAuth2 client-credentials token, cached for the run."""
        if self._token:
            return self._token
        client_id, client_secret = self._credentials()
        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(f"budget refused the token request ({ctx.budget.snapshot()})")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        response = ctx.client.post(
            TOKEN_URL,
            headers={
                "authorization": f"Basic {basic}",
                "content-type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
        )
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise SourceSkippedError(f"eBay token endpoint returned HTTP {response.status_code}")
        try:
            token_payload = json.loads(response.text)
        except ValueError as exc:
            raise MissingCredentialError(f"eBay token endpoint returned non-JSON: {exc}") from exc
        token = _secret(token_payload.get("access_token"))
        if not token:
            raise MissingCredentialError("eBay returned no access token for these credentials")
        self._token = token
        return token

    # -- contract ----------------------------------------------------------------------------
    def fetch(self, ctx: FetchContext) -> RawBatch:
        """Search for the phrase L2 asked about, walking at most ``MAX_PAGES`` pages.

        The cursor *is* the phrase: L2 is targeted rather than incremental, so there is no watermark
        to advance — the caller chooses what to enrich and the ledger records what it cost.
        """
        query = (ctx.cursor or "").strip()
        if not query:
            raise SourceSkippedError("no phrase to enrich: L2 must set the cursor")
        token = self._app_token(ctx)
        parts: list[str] = []
        statuses: list[int] = []
        requests = 1  # the token call
        for page in range(MAX_PAGES):
            if ctx.budget.acquire() != "ok":
                break
            offset = page * PAGE_SIZE
            response = ctx.client.get(
                SEARCH_URL,
                headers={
                    "authorization": f"Bearer {token}",
                    "x-ebay-c-marketplace-id": self._market,
                },
                params={
                    "q": query,
                    "limit": str(PAGE_SIZE),
                    "offset": str(offset),
                },
            )
            ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
            statuses.append(int(response.status_code))
            requests += 1
            if response.status_code >= HTTP_CLIENT_ERROR:
                break
            parts.append(response.text)
            payload = _payload_of(response.text)
            total = int(payload.get("total") or 0)
            if offset + PAGE_SIZE >= min(total, PAGE_SIZE * MAX_PAGES):
                break

        return RawBatch.from_parts(
            source_id=self.id,
            cursor=query,
            parts=parts,
            status_codes=tuple(statuses),
            fetched_at=ctx.clock.now(),
            request_count=requests,
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        """One signal per phrase: how many listings compete, and their price distribution.

        `value` is the active-listing count (a raw count; the scorer normalizes it), and the
        metadata
        carries the price spread so a reader can check the number without re-reading the lake.
        """
        phrase = str(raw.cursor or "").strip()
        if not phrase:
            return []
        total = 0
        prices: list[float] = []
        for part in raw.parts:
            payload = _payload_of(part)
            total = max(total, int(payload.get("total") or 0))
            for item in payload.get("itemSummaries") or []:
                value = (item.get("price") or {}).get("value")
                if value is None:
                    continue
                try:
                    prices.append(float(value))
                except (TypeError, ValueError):
                    continue

        if total == 0 and not prices:
            # Nobody sells this phrase. An empty batch, not a zero: "no listings found" and "zero
            # demand" are different claims, and only the first one is supported by this response.
            return []

        metadata: dict[str, str] = {
            "total_listings": str(total),
            "priced_listings": str(len(prices)),
        }
        if prices:
            metadata.update(
                {
                    "price_min": f"{min(prices):.2f}",
                    "price_median": f"{statistics.median(prices):.2f}",
                    "price_max": f"{max(prices):.2f}",
                }
            )
        return [
            Signal(
                source_id=self.id,
                entity=phrase,
                metric=self.METRIC,
                value=float(total),
                ts=raw.fetched_at,
                # A human-checkable URL for the same search, so any reader can verify the count.
                url=f"https://www.ebay.com/sch/i.html?_nkw={phrase.replace(' ', '+')}",
                metadata=metadata,
            )
        ]

    @staticmethod
    def enrichment_of(signals: Sequence[Signal]) -> dict[str, str]:
        """The price spread for one enrichment, in the shape a scorer or a brief can consume."""
        for signal in signals:
            if signal.metric == EbayBrowsePlugin.METRIC:
                return dict(signal.metadata)
        return {}
