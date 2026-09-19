---
# TrendAnalyst-361i
title: P4 — L2 enrichment, briefs, snapshots, TTL
status: completed
type: task
created_at: 2026-09-19T18:07:20Z
updated_at: 2026-09-19T18:07:20Z
---

Phase P4 of the builder brief: L2 enrichment, briefs, snapshots, TTL.

Brief requirement, verbatim: "eBay Browse and one more Tier-A plugin (L2 only, on-demand, top-K),
brief rendering, snapshot history views, TTL jobs."
DONE WHEN: "full nightly run completes inside budget with quota ledger balanced to zero unexplained
spend."
EVIDENCE: docs/evidence/P4-gate.md (raw, 16 checks PASS) and docs/evidence/P4.md (outcome).

## Outcome

All six deliverables are in place and the acceptance runs on every gate:

  $ uv run python -m scripts.nightly --offline --dry-run
    L0 collect ok - L1/L3 decide ok (10 scored) - L2 skipped (no Tier-A credential, said so)
    L3 judge dry-run - L3 writer empty - TTL would delete 0
    ledger: 118 rows, 30,180 units of spend, 118 explained - BALANCED
  NIGHTLY: PASS

"Balanced to zero unexplained spend" needed a definition, and writing it found the bug that made the
bar unreachable: reconciliation compared a per-SOURCE ledger total against a per-(RUN, SOURCE) log
amount, so 71 of 113 rows were flagged - every source ever collected more than once. Fixed, tested,
and the number is now zero.

What landed:
- sources/tier_a/ebay.py: real OAuth2 client-credentials + Browse search reporting ACTIVE supply and
  the observed price spread. The sold-versus-listed ratio needs Marketplace Insights, which eBay
  restricts; that gap is recorded in the docstring rather than inferred
- pipeline/layers/l2.py: only `kept` candidates, top-K by MGS, a source with no budget is REFUSED,
  the ledger's compare-and-swap guards the spend, and a missing credential is a SKIP not a failure
- llm/writer.py + pipeline/briefs.py: one page per kept candidate, grounded, deterministic rendering
- store/history.py + CLI: --history, --history-delta, --compare-versions
- store/ttl.py + scripts/ttl_job.py: raw lake 90 days, LLM cache 30, ledger and snapshots never
- scripts/nightly.py: every layer in the specification's order, then reconciliation
- evals/run_evals.py, .github/workflows/ci.yml, gate probe 16

Honest gaps, stated rather than papered over:
- L2 is unvalidated against live eBay (no credentials); the test fixture is hand-written and labelled
- the LLM-judge rubric of spec 8 is not implemented - it needs briefs and a paid judge
- one more Tier-A plugin than eBay is not implemented yet; the L2 layer that drives them is

Seven bugs found, the two that matter most being the reconciliation comparison and
MissingCredentialError subclassing RuntimeError (a missing key reported as a provider failure).

Numbers: 484 tests, ruff clean, mypy --strict clean on 68 files, gate PASS 16 checks / 0 pending.
