"""Web-wide diffusion counts — `common_crawl_index` (Tier S-S, layers: L0).

STUB — deferred on design, not on network (the index API answers 200). The index
answers URL-prefix queries, not full-text search: "how many pages mention X" is not
a question it can answer cheaply, and the columnar index that could needs paid AWS.
Any L0 plugin would need arbitrary seed domains — noise dressed as coverage — while
the honest placement is per-candidate L2 enrichment, which is a different layer than
the registry declares. Until that redesign, no plugin: a bad source is worse than a
stub that says why.

Contract to implement (spec §4.1): subclass `SourcePlugin` from
`trend_analyst.sources.base`, declare the same budgets the registry declares
(100 requests/day, 0.2 rps, cache TTL 24 h), implement
`fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`, and call only the hosts in
the registry's `domains` allowlist for this source:

    index.commoncrawl.org

Appendix A role: Web-wide diffusion counts
"""

from __future__ import annotations

STUB_PHASE = "P1"
