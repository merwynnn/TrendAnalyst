---
# TrendAnalyst-9jym
title: P4-T1
status: completed
type: task
created_at: 2026-09-19T18:07:20Z
updated_at: 2026-09-19T18:07:20Z
---

- [x] pipeline/layers/l2.py: enrichment for `kept` candidates only, ordered by MGS, cut to top_k
- [x] the lease: a source with no budget is refused ("the leash is required"), not trusted
- [x] the ledger compare-and-swap guards spend, so a resumed night cannot pay twice
- [x] a missing credential is a SKIP with a reason (MissingCredentialError is a SourceSkippedError)
- [x] a Tier-A failure is a gap in tonight's evidence, not a crash
- [x] dry runs store nothing and report zero written signals
- [x] tests: 14 in tests/test_l2.py

DONE WHEN: enrichment is targeted, budgeted, ledgered, and never fakes a zero.
EVIDENCE: tests/test_l2.py; docs/evidence/P4.md
