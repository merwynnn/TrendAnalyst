---
# TrendAnalyst-v9p4
title: P4-T2
status: completed
type: task
created_at: 2026-09-19T18:07:20Z
updated_at: 2026-09-19T18:07:20Z
---

- [x] OAuth2 client-credentials against api.ebay.com, token cached for the run
- [x] Browse search reporting active supply (`total`) and the price spread (min/median/max)
- [x] a human-checkable search URL on every signal, so a reader can verify the count
- [x] no listings is an empty batch, never a zero
- [x] the sold-versus-listed gap is recorded in the docstring rather than inferred
- [x] HttpClient gained `post` (protocol, real client, fixture replayer), with method-aware keys

DONE WHEN: the plugin is real code with honest limits, testable without a credential.
EVIDENCE: tests/test_l2.py (parsing, credential gating, price spread)
