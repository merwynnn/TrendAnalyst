---
# TrendAnalyst-pc18
title: P2-T3 — MGS, fad flag, Monte Carlo revenue in code
status: completed
type: task
created_at: 2026-09-19T15:38:34Z
updated_at: 2026-09-19T15:38:34Z
---

- [x] scoring/normalize.py: percentile ranks, EWMA, z-scores, growth ratios, dense daily series, log compression
- [x] scoring/features.py: 30/90/365-day windows, slope divergence, spike ratio, seasonality, evergreen ratio, pain and intent markers
- [x] scoring/mgs.py: MGS = 0.30DV + 0.25(100-SS) + 0.20SP + 0.15MP + 0.10FE, per-category percentiles, weights version v1
- [x] scoring/fad.py: the three named features, a logistic with visible weights, version fadv1, three schema-legal labels
- [x] scoring/revenue.py: TAM x CTR x CVR x price, 4000 draws, seeded per candidate, P10/P50/P90 with the assumptions attached
- [x] tests: 35 pure-function tests including determinism, bounds, small-sample honesty

DONE WHEN: scores are reproducible and bounded, and the formula matches the spec by hand.
EVIDENCE: tests/test_scoring.py; the MGS weights assertion pins 0.30/0.25/0.20/0.15/0.10
