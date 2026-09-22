"""OpenAlex — `openalex_arxiv` (Tier S, L0): paper-surge tech radar.

The OpenAlex API needs no key: one request per applied-tech query answers recent works
sorted by citation count, with titles, DOIs, dates and abstracts. A surge of cited
papers around a buildable thing (sensors, fermentation, automation) is the earliest
signal this pipeline reads — research interest precedes products by years.

arXiv's export API is declared in the registry but not called: from this network it
answers every request with an empty body (probed twice, September 2026 — likely
throttling, not absence), and an empty answer recorded as a fixture would pin a
fiction. The domain stays allowlisted for the retry; the docstring, not the code,
is where that wait is recorded.

Abstracts arrive as an inverted index (`{word: [positions]}`), reconstructed here by
the standard positional sort. A work with no reconstructable abstract still yields its
title — the title is the entity, the abstract is its evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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

__all__ = ["OpenAlexPlugin"]

API = "https://api.openalex.org/works"

#: Applied-tech queries: buildable domains, not disciplines. Recent works only — the
#: radar watches what is starting, and citation-sorted pages of old classics are not it.
QUERIES: Final[tuple[str, ...]] = (
    "3d printing",
    "wearable sensor",
    "home automation",
    "fermentation",
    "drone delivery",
)

#: Works per query. Twenty-five keeps the response small and the radar wide.
DEFAULT_PER_PAGE: Final = 25
#: How far back "recent" reaches.
LOOKBACK_DAYS: Final = 90
#: Quote length: the abstract's opening, not its method section.
QUOTE_CHARS: Final = 280


def _reconstruct_abstract(inverted: dict[str, list[int]]) -> str:
    """An inverted index back into reading order: positions decide, ties keep insertion."""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for position in positions:
            if isinstance(position, int) and position not in slots:
                slots[position] = str(word)
    return " ".join(slots[index] for index in sorted(slots))


class OpenAlexPlugin(SourcePlugin):
    """Tier-S collector for OpenAlex recent works."""

    id: ClassVar[str] = "openalex_arxiv"
    tier: ClassVar[Tier] = "S"
    layers: ClassVar[tuple[SourceLayer, ...]] = ("L0",)
    schedule: ClassVar[Schedule] = "weekly"
    budget_per_day: ClassVar[int] = 2000
    rps: ClassVar[float] = 5
    cache_ttl_h: ClassVar[int] = 24
    domains: ClassVar[tuple[str, ...]] = ("api.openalex.org", "export.arxiv.org")

    def fetch(self, ctx: FetchContext) -> RawBatch:
        now = ctx.clock.now()
        since = (now - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
        per_page = min(200, max(1, ctx.max_items or DEFAULT_PER_PAGE))

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
                    "search": query,
                    "filter": f"from_publication_date:{since}",
                    "sort": "cited_by_count:desc",
                    "per-page": str(per_page),
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
                    f"budget refused the works queries ({ctx.budget.snapshot()})"
                )
            raise SourceSkippedError(
                f"no works query could be requested ({ctx.budget.snapshot()})"
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
            works = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(works, list):
                continue
            for work in works:
                if not isinstance(work, dict):
                    continue
                signal = _to_signal(work, fetched_at=raw.fetched_at)
                if signal is not None:
                    signals.append(signal)
        return signals


def _to_signal(work: dict[str, Any], *, fetched_at: datetime) -> Signal | None:
    """One work into a citation signal. None for untitled rows."""
    title = str(work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None
    try:
        cited = int(work.get("cited_by_count") or 0)
    except (TypeError, ValueError):
        return None
    doi = str(work.get("doi") or "")
    ts = _published_at(str(work.get("publication_date") or ""), fallback=fetched_at)
    abstract = ""
    inverted = work.get("abstract_inverted_index")
    if isinstance(inverted, dict):
        positions: dict[str, list[int]] = {
            str(word): [int(position) for position in refs if isinstance(position, int)]
            for word, refs in inverted.items()
            if isinstance(refs, list)
        }
        abstract = _reconstruct_abstract(positions)
    topic = ""
    primary_topic = work.get("primary_topic")
    if isinstance(primary_topic, dict):
        topic = str(primary_topic.get("display_name") or "")
    return Signal(
        source_id="openalex_arxiv",
        entity=title,
        metric="openalex_cited",
        value=float(max(0, cited)),
        ts=ts,
        url=doi or None,
        quote=abstract[:QUOTE_CHARS] or title,
        metadata={
            "doi": doi,
            "topic": topic,
            "publication_year": str(work.get("publication_year") or ""),
        },
    )


def _published_at(value: str, *, fallback: datetime) -> datetime:
    """A work's publication day, or the fetch time when it does not parse."""
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return fallback
