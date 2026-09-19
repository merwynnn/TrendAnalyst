"""Tech launches and critiques — `hn_firebase` (Tier S-S, layers: L0).

STUB — implemented in phase P1. Nothing here pretends to work: the class is not
defined yet, so a registry that resolves this module still cannot run the source.

Contract to implement (spec §4.1): subclass `SourcePlugin` from
`trend_analyst.sources.base`, declare the same budgets the registry declares
(2000 requests/day, 5 rps, cache TTL 24 h), implement
`fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`, and call only the hosts in
the registry's `domains` allowlist for this source:

    hacker-news.firebaseio.com
    news.ycombinator.com

Appendix A role: Tech launches and critiques
"""

from __future__ import annotations

STUB_PHASE = "P1"
