---
# TrendAnalyst-5hfz
title: 'P2-T2 — L1 mining: phrases, velocity, 95% prune'
status: completed
type: task
created_at: 2026-09-19T15:38:34Z
updated_at: 2026-09-19T15:38:34Z
---

- [x] pipeline/layers/l1.py: 2-4 gram extraction, segment-aware (n-grams never cross a joined field)
- [x] stop words, noise patterns, stop phrases, staleness horizon, Wikipedia excluded from mining
- [x] per-category EWMA z-score per phrase, percentile-ranked within the category
- [x] prune 95% by velocity with a documented floor; `mined`, `collapsed`, `pruned`, `kept` all reported
- [x] tests: separator crossing, thread furniture, the floor, staleness, substring collapse

DONE WHEN: the prune is applied, visible in the ledger, and never silently disabled.
EVIDENCE: "kept 10 (pruned 3, unmatched 2091, collapsed 8)" in the gate output
