"""Reddit pain mining (~15 subs) — `arctic_shift` (Tier S-S, layers: L0).

STUB — implemented in phase P1. Nothing here pretends to work: the class is not
defined yet, so a registry that resolves this module still cannot run the source.

Contract to implement (spec §4.1): subclass `SourcePlugin` from
`trend_analyst.sources.base`, declare the same budgets the registry declares
(3000 requests/day, 2 rps, cache TTL 24 h), implement
`fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`, and call only the hosts in
the registry's `domains` allowlist for this source:

    arctic-shift.photon-reddit.com

Appendix A role: Reddit pain mining (~15 subs)
"""

from __future__ import annotations

STUB_PHASE = "P1"
