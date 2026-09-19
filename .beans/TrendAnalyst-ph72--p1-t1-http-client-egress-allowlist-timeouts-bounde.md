---
# TrendAnalyst-ph72
title: 'P1-T1 — HTTP client: egress allowlist, timeouts, bounded retries'
status: completed
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:39:03Z
parent: TrendAnalyst-8dtf
---

- [x] src/trend_analyst/net.py: httpx-backed HttpClient that enforces the per-source egress allowlist BEFORE any request (spec §10)
- [x] timeouts everywhere; bounded retries with exponential backoff on transport errors and 5xx only
- [x] 429 is NOT retried blindly: it is returned so the SourceBudget can back off and record spend (spec §4.3)
- [x] sleeps injected, so retry paths are testable without waiting
- [x] RecordedHttpClient: replays committed fixtures (exact URL match) and enforces the allowlist too
- [x] tests: allowlist deny/allow (exact + wildcard), retry-then-succeed, retry-exhausted, 429 returned un-retried, timeout, fixture replay miss is a loud error

DONE WHEN: tests green and no test touches the network.
EVIDENCE: pasted into docs/evidence/P1.md

## Summary of Changes

src/trend_analyst/net.py — every request a plugin makes goes through here, and four rules
are enforced that a plugin cannot be trusted to remember:

- **Egress allowlist before the request** (spec §10): exact host or an explicit `*.suffix`
  wildcard, and a 3xx is deliberately not followed, because following a redirect would
  quietly widen the allowlist. A denied URL is never sent (asserted).
- **Timeouts everywhere**, and a transport failure is a retryable event rather than a hang.
- **Bounded retries** on transport errors and 5xx with exponential backoff (capped).
  **429 is returned un-retried** on purpose: spec §4.3 gives the budget, not the client,
  the job of backing off. Same for other 4xx: they are final answers.
- **Sleeps injected**, so retry behaviour is verified by counting, not by waiting.

Also: RecordedHttpClient (fixture replay, with a loud FixtureMissError naming the URL and
the re-record command — a stale fixture must never look like "the source returned
nothing"), RecordingHttpClient (used only by the manual recorder; keeps just content-type
and retry-after so a session cookie can never be committed), and a stable request_url() so
fixtures have deterministic keys.

29 tests, none of which touch the network (httpx.MockTransport and fixtures only).

### Bug found while recording, and fixed here
The leash REFUSED on a rate limit instead of pacing, so the first arctic_shift recording
fetched 3 of 15 subreddits and then every further attempt was refused — a silent
under-collection, which is the worst kind of failure for a discovery pipeline.
SourceBudget.acquire() now waits for a slot (sleep injected), while still not waiting out a
backoff or a budget refusal. It also has a bounded loop as well as a deadline, because a
caller-supplied clock that does not advance with sleep would otherwise spin forever: a
wrong clock must not become a hung pipeline.
