---
# TrendAnalyst-gdco
title: P2-T4 — Ranked table, versioned snapshots, eval seed
status: completed
type: task
created_at: 2026-09-19T15:38:34Z
updated_at: 2026-09-19T15:38:34Z
---

- [x] store/snapshots.py: append-only writes, candidate_key helper, idempotent per (candidate, run, weights_version), ranked and history views, snapshot_hash
- [x] pipeline/decide.py + layers/l3.py: the decide run, per-source attention normalization, dry-run that writes nothing
- [x] CLI: --layers L0,L1,L3, --rank, --top, --min-keep, --prune-fraction, --as-of; refuses L2 and unknown layers
- [x] scripts/seed_evals.py: 10 golden cases from real output (6 keep / 4 drop), mirrored into eval_cases, provenance in `notes`
- [x] gate: P2 probe runs the CLI twice and asserts an identical snapshot hash, plus the ranked-table assertions
- [x] tests/test_decide.py: 19 db-backed tests covering idempotency, determinism, dry run, history, append-only enforcement

DONE WHEN: the ranked table renders end-to-end from L0 data with snapshots versioned.
EVIDENCE: gate check "P2 decide — rank, snapshot, deterministic replay" PASS
