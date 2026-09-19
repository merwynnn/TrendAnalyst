---
# TrendAnalyst-8dtf
title: P1 — L0 collectors + run ledger
status: todo
type: epic
priority: high
created_at: 2026-09-19T14:15:07Z
updated_at: 2026-09-19T14:15:07Z
---

Implement three Tier-S collectors (HN Firebase, Wikipedia Pageviews, Arctic Shift) as SourcePlugins behind the T4 contract, with an httpx client that enforces the per-source egress allowlist, watermarks, content-hash dedup and run-ledger writes. Acceptance (brief P1): two consecutive nightly-equivalent runs, the second doing near-zero new work, proven by the ledger; and a kill-and-resume test showing only unfinished work re-executes.
