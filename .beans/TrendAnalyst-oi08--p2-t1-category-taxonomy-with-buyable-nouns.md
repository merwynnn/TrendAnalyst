---
# TrendAnalyst-oi08
title: P2-T1 — Category taxonomy with buyable nouns
status: completed
type: task
created_at: 2026-09-19T15:38:34Z
updated_at: 2026-09-19T15:38:34Z
---

- [x] config/categories.yaml: 10 product domains, each with `core` nouns, context keywords, a price band and a feasibility prior
- [x] config/categories.py: loader (typed, extra="forbid") + matcher requiring at least one core noun
- [x] scripts/category_coverage.py: the match rate measured against the real lake, not assumed
- [x] tests: proper nouns rejected, thread furniture rejected, penalties, malformed-file errors

DONE WHEN: the taxonomy rejects "cat jarman" and keeps "cat litter".
EVIDENCE: coverage on the real lake is 1.0% overall (Reddit 9.9%); reported in docs/evidence/P2.md
