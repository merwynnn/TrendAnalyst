---
# TrendAnalyst-gp6a
title: P4-T3
status: completed
type: task
created_at: 2026-09-19T18:07:20Z
updated_at: 2026-09-19T18:07:20Z
---

- [x] scripts/nightly.py: L0 -> L1/L3 -> L2 -> judge -> writer -> TTL -> reconciliation
- [x] monitor/reconcile.py: every spend row checked against the record that should explain it
- [x] --offline replays recorded fixtures and gate answers, so the whole path is verifiable for free
- [x] exits non-zero when anything spent cannot be explained
- [x] gate probe: all layers, then a balanced ledger, on every gate run
- [x] tests: 16 in tests/test_reconcile.py

DONE WHEN: the P4 acceptance is a command, not a claim.
EVIDENCE: gate check "P4 nightly - all layers then a balanced ledger" PASS
