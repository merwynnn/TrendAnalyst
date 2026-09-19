---
# TrendAnalyst-qg0q
title: P3-T2
status: completed
type: task
created_at: 2026-09-19T17:26:31Z
updated_at: 2026-09-19T17:26:31Z
---

- [x] llm/gateway.py: spec §6.3 chain (Gemini -> Groq -> Cerebras -> local Ollama)
- [x] failover on transport error, refusal and schema violation; exactly one schema retry per entry
- [x] bounded transient retries (429/5xx/timeout) with immediate failover for 404/402
- [x] per-gate call AND token caps; a cap degrades with a reason and never retries blindly
- [x] cache consulted before any provider; identical work reaches a provider once, ever
- [x] llm/providers.py: the four real transports, egress-allowlisted, missing key = error
- [x] tests: 22 in tests/test_llm.py (no network), plus the live drill

DONE WHEN: failover and accounting are proven against a real provider.
EVIDENCE: docs/evidence/P3-drill.txt (DRILL: PASS)
