---
# TrendAnalyst-frnt
title: P3-T1
status: completed
type: task
created_at: 2026-09-19T17:26:31Z
updated_at: 2026-09-19T17:26:31Z
---

- [x] llm/schemas.py: JudgeVerdict, JudgeBatch, PlannerPlan, WriterBrief, all extra="forbid"
- [x] Quote requires an http(s) URL (a javascript: citation is refused at the boundary)
- [x] parse_gate_output tolerates a fenced json block and reports the violation's location
- [x] enforce_grounding(): strips any quote whose URL the pipeline never collected, marks the verdict ungrounded rather than deleting it
- [x] the rule covers BATCHES (the bug that would have stored invented citations as fact)
- [x] tests: 12 in tests/test_llm.py covering fences, violations, grounding in all three directions

DONE WHEN: an invented citation cannot survive, and a stripped verdict says so.
EVIDENCE: tests/test_llm.py; docs/evidence/P3.md
