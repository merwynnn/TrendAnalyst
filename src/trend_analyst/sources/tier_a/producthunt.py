"""Product Hunt — `producthunt_graphql` (Tier A, L2): launch velocity.

Tier-A sources run **only in L2**, on demand, for the top-K candidates the Judge kept.
This plugin reads the ranked launch shelf: post names, taglines, vote counts and dates —
launch velocity with a number on it, which is what "is this space heating up" needs.

**Live status, stated plainly (September 2026).** The OAuth token endpoint works
(form-encoded `client_credentials` mints a bearer token), but the GraphQL endpoint
answers every query — including `{ __typename }` — with an empty HTTP 400, with any
token, any query and any user agent. Fifteen probes, one live token, zero answers.
The likely causes are app-side activation or edge bot-management, both outside this
codebase. So this plugin is complete but UNVALIDATED: parsing is exercised against a
hand-written, labelled response shape (the eBay precedent), the registry entry stays
disabled, and enabling it waits on one successful live recording. A fixture that
pretends a recording happened would be worse than none.

Credentials: OAuth2 client-credentials against `api.producthunt.com` (the registry
allows that host, and the plugin never widens it). Without a client id and secret the
plugin raises `MissingCredentialError`, so an unconfigured L2 run degrades with a
reason instead of reporting zero launches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

__all__ = ["MissingCredentialError", "ProductHuntPlugin"]

TOKEN_URL: Final = "https://api.producthunt.com/v2/oauth/token"
GRAPHQL_URL: Final = "https://api.producthunt.com/v2/api/graphql"

#: Ranked launches per run. Twenty is the shelf readers actually scan; more would spend
#: complexity budget (PH rate-limits by query complexity) for launches nobody opens.
FIRST: Final = 20

_LAUNCHES_QUERY: Final = """{
  posts(first: %d, order: RANKING) {
    edges {
      node {
        id
        name
        tagline
        votesCount
        createdAt
        slug
      }
    }
  }
}"""


def _secret(value: Any) -> str:
    """Read a credential whether it arrives as a `SecretStr`, a string, or nothing."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    text = str(getter() if callable(getter) else value)
    return "" if text in {"", "SET_ME", "None"} else text


class ProductHuntPlugin(SourcePlugin):
    """Read Product Hunt's ranked launches and report votes per launch."""

    id: ClassVar[str] = "producthunt_graphql"
    tier: ClassVar[Literal["A"]] = "A"
    layers: ClassVar[tuple[Literal["L2"], ...]] = ("L2",)
    schedule: ClassVar[Literal["on_demand"]] = "on_demand"
    budget_per_day: ClassVar[int] = 200
    rps: ClassVar[float] = 0.5
    domains: ClassVar[tuple[str, ...]] = ("api.producthunt.com",)

    def __init__(self, *, settings: Any = None) -> None:
        self._settings = settings
        #: Cached bearer token for the life of one run: an OAuth dance per candidate
        #: would add a request per phrase for no benefit.
        self._token: str | None = None

    # -- credentials -----------------------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        section = getattr(self._settings, "tier_a", None)
        client_id = _secret(getattr(section, "producthunt_client_id", None))
        client_secret = _secret(getattr(section, "producthunt_client_secret", None))
        if not client_id or not client_secret:
            raise MissingCredentialError(
                "producthunt_client_id/producthunt_client_secret are not set: "
                "L2 cannot read launch velocity"
            )
        return client_id, client_secret

    def _app_token(self, ctx: FetchContext) -> str:
        """One OAuth2 client-credentials token, cached for the run."""
        if self._token:
            return self._token
        client_id, client_secret = self._credentials()
        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(f"budget refused the token request ({ctx.budget.snapshot()})")
        response = ctx.client.post(
            TOKEN_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
            content=f"client_id={client_id}&client_secret={client_secret}"
            "&grant_type=client_credentials",
        )
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise SourceSkippedError(
                f"Product Hunt token endpoint returned HTTP {response.status_code}"
            )
        try:
            token_payload = json.loads(response.text)
        except ValueError as exc:
            raise SourceSkippedError(f"token endpoint answered non-JSON: {exc}") from exc
        token = str(token_payload.get("access_token") or "")
        if not token:
            raise SourceSkippedError("token endpoint answered without an access token")
        self._token = token
        return token

    # -- fetch / parse ----------------------------------------------------------
    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        spent_before = ctx.budget.spent_today
        token = self._app_token(ctx)
        if ctx.budget.acquire() != "ok":
            raise SourceSkippedError(
                f"budget refused the launches request ({ctx.budget.snapshot()})"
            )
        response = ctx.client.post(
            GRAPHQL_URL,
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
            content=json.dumps({"query": _LAUNCHES_QUERY % FIRST}),
        )
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise SourceSkippedError(
                f"launches query returned HTTP {response.status_code}"
            )
        return RawBatch.from_parts(
            source_id=self.id,
            parts=[response.text],
            fetched_at=now,
            cursor=now.astimezone(UTC).date().isoformat(),
            status_codes=(response.status_code,),
            # Whatever the budget spent since this fetch started: the token dance only
            # spends on the first candidate of a run, the query spends every time.
            request_count=ctx.budget.spent_today - spent_before,
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        signals: list[Signal] = []
        for part in raw.parts:
            try:
                payload: Any = json.loads(part)
            except ValueError:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            posts = data.get("posts") if isinstance(data, dict) else None
            edges = posts.get("edges") if isinstance(posts, dict) else None
            if not isinstance(edges, list):
                continue
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                node = edge.get("node")
                if not isinstance(node, dict):
                    continue
                signal = _to_signal(node, fetched_at=raw.fetched_at)
                if signal is not None:
                    signals.append(signal)
        return signals


def _to_signal(node: dict[str, Any], *, fetched_at: datetime) -> Signal | None:
    """One launch into a vote-count signal. None for unnamed rows."""
    name = str(node.get("name") or "").strip()
    if not name:
        return None
    try:
        votes = int(node.get("votesCount") or 0)
    except (TypeError, ValueError):
        return None
    slug = str(node.get("slug") or "").strip()
    tagline = " ".join(str(node.get("tagline") or "").split())
    try:
        launched = datetime.fromisoformat(
            str(node.get("createdAt") or "").replace("Z", "+00:00")
        )
        if launched.tzinfo is None:
            launched = launched.replace(tzinfo=UTC)
    except ValueError:
        launched = fetched_at
    return Signal(
        source_id="producthunt_graphql",
        entity=name,
        metric="ph_votes",
        value=float(max(0, votes)),
        ts=launched,
        url=f"https://www.producthunt.com/posts/{slug}" if slug else None,
        quote=tagline[:280] or name,
        metadata={
            "slug": slug,
            "post_id": str(node.get("id") or ""),
        },
    )
