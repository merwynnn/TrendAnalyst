---
# TrendAnalyst-fy5c
title: 'T2 — Layered settings: defaults < YAML < env < CLI'
status: in-progress
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:02:07Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
---

- [ ] config/settings.py — pydantic-settings model, nested sections (app, db, llm, tier_a, gates, budgets, health)
- [ ] YAML layer: config/settings.yaml + config/settings.<env>.yaml
- [ ] secrets layer: config/secrets.local.yaml (untracked, decision B4); template committed as config/secrets.example.yaml
- [ ] env layer (optional override, TA_ prefix, __ nesting) + CLI layer (--set section.key=value)
- [ ] secret redaction: SecretStr, absent from logs, repr and --json output
- [ ] tests: full precedence matrix, redaction, missing-file tolerance, bad-YAML error

DONE WHEN: tests green and a real CLI run proves CLI > env > YAML > defaults.
EVIDENCE: pasted into docs/evidence/P0.md
