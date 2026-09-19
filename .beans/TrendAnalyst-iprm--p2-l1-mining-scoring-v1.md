---
# TrendAnalyst-iprm
title: P2 — L1 mining + scoring v1
status: completed
type: task
created_at: 2026-09-19T15:38:18Z
updated_at: 2026-09-19T15:38:18Z
---

Phase P2 of the builder brief: L1 mining plus scoring v1.

Brief requirement, verbatim: "Phrase extraction, per-category EWMA z-scores, 95 percent prune,
MGS sub-scores in code, fad features, Monte Carlo revenue triple. Seed 10 golden eval cases
from real outputs."
DONE WHEN: "ranked table renders end-to-end from L0 data with snapshots versioned."
EVIDENCE: docs/evidence/P2-gate.md (raw, generated) and docs/evidence/P2.md (outcome).

## Outcome

Four tasks, all completed:

- P2-T1 the taxonomy: config/categories.yaml gained `core` lists (the buyable nouns) and
  config/categories.py the matcher that requires one. Before this rule the mining run filed
  "Cat Jarman" under pets and "children killing" under baby products.
- P2-T2 mining (pipeline/layers/l1.py): segment-aware n-gram extraction, stop words, noise
  patterns, per-category EWMA z-scores, the 95% prune with a documented floor, a substring
  collapse merging the overlapping windows of one title, and a staleness filter.
- P2-T3 scoring (scoring/normalize.py, features.py, mgs.py, fad.py, revenue.py): percentiles,
  EWMA z-scores, the spec's MGS formula verbatim with versioned weights, a logistic fad
  classifier over the three named features, a seeded Monte Carlo revenue triple.
- P2-T4 the decide run (pipeline/decide.py, layers/l3.py, store/snapshots.py): per-source
  attention normalization, append-only versioned snapshots, the ranked table on the CLI
  (--layers L1,L3 and --rank), 10 golden eval cases seeded from real output, and the gate
  check that replays the lake twice and compares snapshot hashes.

Final numbers: 391 tests (none touching the network), ruff clean, mypy --strict clean on 62
files, gate PASS 13 checks / 0 pending. Acceptance output: 83 texts -> 2113 n-grams -> 13
candidates -> 10 kept -> 10 scored -> 10 snapshots, replaying to the identical snapshot hash
6d77ea1defd0552e.

Six bugs found by running things, the worst being silent: a candidate-key order mismatch wrote
candidates without writing snapshots while reporting success.
