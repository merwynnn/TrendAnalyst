"""Live social complaints — `bluesky_jetstream` (Tier S-S, layers: L0).

STUB — implemented in phase P1. Nothing here pretends to work: the class is not
defined yet, so a registry that resolves this module still cannot run the source.

A full plugin was written and recorded against `public.api.bsky.app/xrpc/
app.bsky.feed.searchPosts`, then reverted: the host answers HTTP 403 to every
request from this network (two probes with different user agents, plus five
recorded attempts — all 403), and a hand-written fixture would prove nothing about
an endpoint the pipeline cannot reach. When the block lifts, the plugin shape is:
subclass `SourcePlugin`, five complaint queries (`i wish`, `looking for`,
`recommend`, `hate`, `broken`), cursor = newest post timestamp, metric
`bsky_engagement` (likes + reposts + replies). Jetstream stays out: it is a
websocket firehose and this pipeline's transport is request/response (spec §10).

Contract to implement (spec §4.1): subclass `SourcePlugin` from
`trend_analyst.sources.base`, declare the same budgets the registry declares
(5000 requests/day, 5 rps, cache TTL 24 h), implement
`fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`, and call only the hosts in
the registry's `domains` allowlist for this source:

    public.api.bsky.app
    jetstream1.us-east.bsky.network

Appendix A role: Live social complaints
"""

from __future__ import annotations

STUB_PHASE = "P1"
