"""WordPress.org — `wordpress_org` (Tier S, L0): micro-SaaS gaps.

The plugin directory API needs no key: `query_plugins` with `browse=popular` answers
paged plugin records with what this pipeline actually wants — installs next to rating.
High installs with a mediocre rating is a market telling you the incumbent is hated but
unavoidable, which is the closest thing to a pre-computed buyer-pain signal in L0.

Each run fetches the first pages of the popular list (bounded by `max_items`, fifty
plugins per page). The list moves slowly, so a rerun usually fetches identical payloads
and the content-hash dedup (spec §5.2) skips parsing, scoring and the LLM. The cursor is
the run's date: it says when the list was read, not a position in it, because popularity
is a ranking, not a stream.

Two signals per plugin: the rating (0-100) and the active-install count. Names arrive
with HTML entities ("Elementor Website Builder &#8211; …"), so they are unescaped —
an entity nobody can read is an entity nobody searches for.
"""

from __future__ import annotations

import html
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

__all__ = ["WordPressOrgPlugin"]

API = "https://api.wordpress.org/plugins/info/1.2/"
PLUGIN_URL = "https://wordpress.org/plugins/{slug}/"

#: Plugins per page request; the API's default page size.
PER_PAGE: Final = 50


def _clean(text: str) -> str:
    """Unescape entities and collapse whitespace: names arrive as HTML-ish text."""
    return " ".join(html.unescape(str(text or "")).split())


def _updated_at(value: str, *, fallback: datetime) -> datetime:
    """A plugin's last-updated day, or the fetch time when it does not parse.

    Best effort on purpose: the field is display text ("2026-09-15", sometimes with a
    time), and a wrong guess would backdate the signal — so anything unparsable falls
    back to now rather than to a guessed midnight.
    """
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return fallback


class WordPressOrgPlugin(SourcePlugin):
    """Tier-S collector for the WordPress.org plugin directory."""

    id: ClassVar[str] = "wordpress_org"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 1000
    rps: ClassVar[float] = 1
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("api.wordpress.org",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        pages = max(1, (ctx.max_items or PER_PAGE) // PER_PAGE + 1)

        parts: list[str] = []
        statuses: list[int] = []
        refused = False
        for page in range(1, pages + 1):
            if ctx.budget.acquire() != "ok":
                refused = True
                break  # a partial list is still useful; the log records what was spent
            response = ctx.client.get(
                API,
                params={
                    "action": "query_plugins",
                    "request[browse]": "popular",
                    "request[per_page]": str(PER_PAGE),
                    "request[page]": str(page),
                    "request[fields]": "name,slug,rating,ratings,num_ratings,"
                    "active_installs,short_description,last_updated",
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
                    f"budget refused the directory request ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no directory page could be requested ({ctx.budget.snapshot()})"
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
            plugins = payload.get("plugins") if isinstance(payload, dict) else None
            if not isinstance(plugins, list):
                continue
            for plugin in plugins:
                if not isinstance(plugin, dict):
                    continue
                signals.extend(_to_signals(plugin, fetched_at=raw.fetched_at))
        return signals


def _to_signals(plugin: dict[str, Any], *, fetched_at: datetime) -> list[Signal]:
    """One plugin record into a rating signal and an installs signal."""
    name = _clean(str(plugin.get("name") or ""))
    slug = str(plugin.get("slug") or "").strip()
    if not name or not slug:
        return []
    try:
        rating = float(plugin.get("rating") or 0)
        installs = int(plugin.get("active_installs") or 0)
        rated = int(plugin.get("num_ratings") or 0)
    except (TypeError, ValueError):
        return []
    ts = _updated_at(str(plugin.get("last_updated") or ""), fallback=fetched_at)
    blurb = _clean(str(plugin.get("short_description") or ""))
    url = PLUGIN_URL.format(slug=slug)
    base_metadata = {
        "slug": slug,
        "installs": str(installs),
        "num_ratings": str(rated),
    }
    return [
        Signal(
            source_id="wordpress_org",
            entity=name,
            metric="wp_rating",
            value=rating,
            ts=ts,
            url=url,
            quote=blurb or name,
            metadata={**base_metadata, "kind": "rating"},
        ),
        Signal(
            source_id="wordpress_org",
            entity=name,
            metric="wp_installs",
            value=float(installs),
            ts=ts,
            url=url,
            quote=blurb or name,
            metadata={**base_metadata, "kind": "installs"},
        ),
    ]
