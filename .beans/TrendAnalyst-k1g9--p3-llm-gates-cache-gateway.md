---
# TrendAnalyst-k1g9
title: P3 — LLM gates, cache, gateway
status: completed
type: task
created_at: 2026-09-19T17:26:31Z
updated_at: 2026-09-19T17:26:31Z
---

Phase P3 of the builder brief: LLM gates, cache, gateway.

Brief requirement, verbatim: "Gateway with Gemini -> Groq -> Cerebras -> local fallback, per-gate
budgets, 30-day cache, Pydantic gate schemas, code-enforced grounding rule. Start with mocked
provider responses, then exactly one live run to prove failover and accounting."
DONE WHEN: "judge and writer produce schema-valid, cited outputs on golden inputs; token log shows
spend per gate."
EVIDENCE: docs/evidence/P3-gate.md (raw, 14 checks PASS), docs/evidence/P3-drill.txt (live drill),
docs/evidence/P3.md (outcome).

## Outcome

Both halves of the acceptance are met for the JUDGE gate. The WRITER gate is not built (it belongs
with brief rendering in P4) and is recorded as the outstanding piece.

Live drill, all five probes PASS: a real schema-valid cited Judge verdict (442-464 prompt tokens per
call), a failover to a live second model after an injected failure, a cache hit that reached no
provider, token accounting reconciled against the quota ledger (4410 tokens over 6 rows), and the
grounding rule stripping an invented citation.

What landed:
- llm/schemas.py: Pydantic gate outputs, extra="forbid", http-only citations, parse with location
- llm/cache.py: 30-day TTL keyed by sha256(gate|model|normalized input); expiry on read and
  sweepable; the cache is also the token log (token_spend_by_gate)
- llm/gateway.py: chain order, failover, one schema retry per entry, bounded transient retries,
  terminal caps that degrade with a reason, cache before provider, injected transport
- llm/providers.py: the four real transports; a missing key is an ERROR so an unkeyed provider
  exercises failover; every request passes the egress allowlist
- llm/gates.py + migration 0002 (judgements): the Judge gate — batching, evidence assembly,
  verdicts applied to candidates.status, append-only cited judgements, unknown phrases ignored,
  and a degraded gate that changes NOTHING
- llm/replay.py + scripts/judge_replay.py: the whole gate offline over a REAL recorded provider
  answer, with --fresh so the accounting is real
- viewer: judge column, Gates section (verdicts, keep/drop, no-citation count, token log per gate),
  per-item Gate advice panel with cited quotes, reason and enrichment list

Six bugs found, five of them "a check that measured nothing":
1. grounding was silently bypassed for JudgeBatch (checked for .quotes, not .verdicts) — invented
   citations would have been stored as fact
2. the raw lake insert was not idempotent: a repeated content hash inside one run raised a
   UniqueViolation and took the source down
3. "newest hash" is not dedup: with several payloads per run the newest masks the others, so the
   next run re-parsed everything (the gate's L0 probe caught 12 payloads re-parsed)
4. --json printed two concatenated documents when one command ran two phases
5. a replay that hit the cache proved nothing about tokens (0+0) -> --fresh
6. the replay answered every batch with one recording (matched the phrase against the queue, not
   the batch's prompt)

Numbers: 438 tests, ruff clean, mypy --strict clean on 62-66 files, gate PASS 14 checks / 0 pending.
Beans: P3-T1..T4 completed.
