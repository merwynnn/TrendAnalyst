---
# TrendAnalyst-zai8
title: T3 — sources.yaml registry + Tier-A-in-L0 hard-fail
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:35:22Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
---

- [x] sources.yaml: full Appendix A catalog in FILE ORDER (= execution order): 15 Tier S, then 7 Tier A
- [x] SourceEntry model (module, tier, layers, schedule, budget_per_day, rps, cache_ttl_h default 24, enabled, domains)
- [x] loader: registry -> validated entries, module resolution, duplicate-id detection
- [x] HARD FAIL (spec §4.3): Tier-A listed in L0, unknown tier/layer, empty layers, budget<=0, rps<=0, bad schedule, unknown module
- [x] egress allowlist derived from the registry (§10); enforcement itself lands in P1
- [x] budget derivation table for the qualitative Appendix A entries, documented inline and shown to the human
- [x] tests: one valid fixture + one per failure mode, each asserting the typed error

DONE WHEN: tests green and the real CLI on a bad registry exits non-zero with an actionable message.
EVIDENCE: pasted into docs/evidence/P0.md

## Summary of Changes

The registry — the modularity core — is real: 22 sources validated, ordered, leashed.

- **config/sources.yaml** — the full Appendix A catalog in file order (= execution order):
  15 Tier-S (enabled, keyless) then 7 Tier-A (disabled until their credential exists).
  Every budget carries an inline comment saying WHERE it came from.
- **pipeline/layers/__init__.py** — the layer taxonomy now has a real home (L0-L3, of
  which a source may declare L0 or L2), instead of the registry importing a stub.
- **sources/registry.py** — SourceEntry (frozen, extra=forbid), Registry with
  enabled/disabled/for_layer/by_tier/allowed_domains/host_allowed/quota_plan/summary.
- **Hard failures**, each with an actionable message: Tier A in L0 (also when disabled),
  unknown tier/layer/schedule, empty or duplicate layers, budget<=0, rps<=0, negative
  cache TTL, empty domains, URL instead of host, unknown field, unknown top-level key,
  non-snake_case id, unresolvable module, module outside the package, empty registry,
  missing file, unparseable YAML, non-mapping shapes, duplicate id.
- **Duplicate-id detection** uses a strict YAML loader, because PyYAML silently keeps the
  LAST duplicate key — for a DB key that is silent data loss.
- **Module resolution uses find_spec, never import** — proven by a test that asserts the
  plugin module is absent from sys.modules after loading. Startup validates; it does not
  execute plugin code.
- **Egress allowlist** (spec §10) derived per source, with exact host matching plus an
  explicit '*.suffix' wildcard that does NOT match the apex domain.
- **22 per-source plugin stubs** generated from the registry (Tier S -> P1, Tier A -> P4),
  so every declared module resolves today and each stub names its own contract, budgets
  and allowlist.
- **CLI**: `python -m config.cli registry [--json] [--registry PATH]` plus a gate probe.

### Verification
```
uv run pytest -q        ->  125 passed
uv run ruff check .     ->  All checks passed!
uv run mypy src config  ->  Success: no issues found in 56 source files
uv run python -m config.cli registry
   -> 22 sources (15 Tier S, 7 Tier A) · 15 enabled, 7 disabled · L0: 15, L2: 0
   -> execution order starts hn_firebase, arctic_shift, bluesky_jetstream ...
uv run python -m config.cli registry --registry <tier-A-in-L0.yaml>
   -> exit 1: "'ebay_browse' is Tier A but declares layer L0 ... move it to layers: [L2]"
```

### Spec conflicts raised (C2, C5)
- **C2** eBay: §4.2's example says 1000/day, Appendix A says 5,000/day. Appendix A wins
  (the catalog is the normative ranking); documented inline.
- **C5** Appendix A marks Google Books and Common Crawl 'monthly', but the plugin contract
  (§4.1) only offers nightly|weekly|hourly|on_demand. They run weekly — cheap, and the
  watermark makes extra passes nearly free — rather than inventing a fifth schedule.
