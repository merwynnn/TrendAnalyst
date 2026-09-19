---
# TrendAnalyst-64gf
title: P5 — Documents, evals, monitor dry-run
status: completed
type: epic
priority: normal
created_at: 2026-09-19T18:58:22Z
updated_at: 2026-09-19T18:58:44Z
---

P5 — Documents, evals, monitor dry-run (brief §5, §6, §8).

Delivered:
- AGENT.md to §5's bar: commands first, structure+stack, three-tier boundaries, monitor loop,
  five runbooks (+ a sixth for stale watermarks), change classes with verification steps,
  escalation template, rollback rule. 156 lines; detail moved to docs/COMMANDS.md.
- USER_SETUP.md to §6's bar: 11-credential key table (console URL, free tier, key name, verify
  command), approvals with wait times, both schedulers (WSL cron + GitHub Actions), first-run
  checklist ending in health with the expected output, $0/month cost table naming the cap that
  would trigger each paid tier, and the five named failure modes.
- monitor/alerts.py: five rules (rate_limit_storm, quota_burn, source_failure_streak,
  keep_rate_drift, eval_baseline_drop) + a jsonl outbox; every alert carries tier, action,
  reversal and blast radius.
- monitor/drift.py: keep-rate bands vs baseline, eval baseline drop against the STORED baseline
  (a creeping regression is caught), stale watermarks.
- scripts/runbook_drill.py + scripts/staging_db.sh: seven incidents injected into a throwaway
  database that is dropped afterwards; each cure verified to silence its alert.
- 50 golden eval cases (was 10) and an auditable re-label path (--relabel --to --reason).
- .github/workflows/nightly.yml: the reproducible nightly on GitHub's runners (no provider key,
  cannot spend), plus the documented local cron entry for the live run.
- Gate: 19 checks PASS / 0 pending; 517 tests; ruff + mypy --strict clean.

Found and fixed while rehearsing: health reported every skipped source as degraded (exit 1 on a
working system); a dry run left its run row open (7 stale 'running' rows, health claimed a live
run); the eval report double-counted skipped+passed.

Honest gaps: the LLM rubric is still unclaimed (free tier spent, no paid tier), L2 is still
unvalidated against live eBay (no Tier-A key), and docs/evidence/P5-signoff.md stays empty until
a human works through USER_SETUP.md — §8's last item, which an agent cannot do.
