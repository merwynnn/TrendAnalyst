---
# TrendAnalyst-zai8
title: T3 — sources.yaml registry + Tier-A-in-L0 hard-fail
status: todo
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T11:42:43Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
---

- [ ] sources.yaml: full Appendix A catalog in FILE ORDER (= execution order): 15 Tier S, then 7 Tier A
- [ ] SourceEntry model (module, tier, layers, schedule, budget_per_day, rps, cache_ttl_h default 24, enabled, domains)
- [ ] loader: registry -> validated entries, module resolution, duplicate-id detection
- [ ] HARD FAIL (spec §4.3): Tier-A listed in L0, unknown tier/layer, empty layers, budget<=0, rps<=0, bad schedule, unknown module
- [ ] egress allowlist derived from the registry (§10); enforcement itself lands in P1
- [ ] budget derivation table for the qualitative Appendix A entries, documented inline and shown to the human
- [ ] tests: one valid fixture + one per failure mode, each asserting the typed error

DONE WHEN: tests green and the real CLI on a bad registry exits non-zero with an actionable message.
EVIDENCE: pasted into docs/evidence/P0.md
