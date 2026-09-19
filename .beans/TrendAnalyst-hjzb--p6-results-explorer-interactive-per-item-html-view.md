---
# TrendAnalyst-hjzb
title: 'P6 — Results explorer: interactive per-item HTML view'
status: in-progress
type: task
created_at: 2026-09-19T16:01:39Z
updated_at: 2026-09-19T16:01:39Z
---

Roadmap item P6, first cut delivered and extended by later phases.

The operator asked for a way to READ the results with maximum detail per item: every named
source that mentioned it, the data collected from each, gate outcomes, and the LLM's advice.
An interactive HTML view was the right call, with two constraints this implementation keeps:

- ONE self-contained file. No CDN, no fonts, no framework, no server. It must open offline and
  still work in five years, and requiring a local server to read your own results is friction
  the page exists to remove.
- EVERY figure labelled with its provenance: `stored` (read from a score snapshot - what the
  system decided, and which weights version decided it), `recomputed` (derived from the raw
  lake at build time, so a later collection can make it disagree with the snapshot, and the
  page says so), or `pending` (the phase that owes it). A number the system cannot justify is
  never rendered; a missing figure is an explicit empty state, never a zero, because a zero
  reads as a measurement.

Built (first cut, P2 data):
- src/trend_analyst/viewer/build.py + template.html -> docs/viewer/index.html (regenerate with
  `uv run python -m trend_analyst.viewer.build`)
- ranked table: MGS, all five sub-scores, headroom, fad label + probability, revenue triple,
  mentions, and which sources mentioned it; sortable on every column, filterable by text,
  category and fad label
- per-item detail: score with the formula written out, the Monte Carlo assumptions, the
  recomputed evidence (signal counts, distinct named sources, 45-day timeline, quotes with
  URLs), the full feature vector (windows, EWMA, slopes, spike ratio, seasonality, evergreen
  ratio, pain/intent counts), provenance (candidate id, run id, scored at, snapshot history
  per weights version), and a "still missing" panel
- the source registry with roles, tiers, layers, budgets, watermarks and last-run status
- the run ledger with layer status and notes
- deep links: `#item=<phrase>` opens one item, so a single result is a URL you can keep

Verified by looking at it: two headless screenshots (table + detail panel), 4 tests covering
data fidelity, payload escaping, determinism, and evidence matching.

Honest limits, recorded as bugs found while building it:
- the first build mixed candidates from older runs into the table (six phrases the substring
  collapse had since retired), which reads as a scorer bug rather than history -> the default is
  now the newest decide run, with --all-runs for history
- evidence matching searched only the title column, so a phrase mined from a Reddit body showed
  "0 signals" beside a feature panel full of computed numbers -> the query searches the quote too

Extensions still owed (this is why P6 stays open):
- P3: the Judge's keep/drop verdict and its cited quotes per item
- P4: Tier-A market data (sold-vs-listed, review counts, price distribution) per item
- P5: Writer briefs linked to a snapshot, alert history, eval case results per item
- a trend sparkline per item across snapshots (the data is already there: scores are append-only)
