"""Niche book velocity — `google_books` (Tier S-S, layers: L0).

STUB — deferred, not forgotten. Three probes from this network (September 2026)
all answered HTTP 429 on different queries, so the throttling is IP-level, not
load: no politeness setting fixes it. Two ways back: (1) wait for the throttle to
clear and record with `record_fixtures --source google_books`; (2) a free API key
(console.cloud.google.com) turns the source into an optional-key plugin that skips
without one. Until either, a hand-written fixture would prove nothing.

Contract to implement (spec §4.1): subclass `SourcePlugin` from
`trend_analyst.sources.base`, declare the same budgets the registry declares
(100 requests/day, 0.5 rps, cache TTL 24 h), implement
`fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`, and call only the hosts in
the registry's `domains` allowlist for this source:

    www.googleapis.com

Appendix A role: Niche book velocity
"""

from __future__ import annotations

STUB_PHASE = "P1"
