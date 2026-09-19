# Bugs and lessons

Every bug this project has found by running it, what class of failure it belongs to, how it
was fixed, and — the part that matters — what now stops that class from recurring. Phase
logs (`docs/evidence/P0.md`, `P1.md`, `P2.md`) record bugs as they happened; this file is the
durable accumulation, and it is updated when a phase closes.

**Why a separate file.** A bug fixed in a commit message is a bug that comes back the moment
someone rewrites the code around it. Each entry below therefore ends in **prevention** — a
test, a gate check, a constraint or a rule — because a fix without one is a promise, and this
file is not in the business of promises.

Status legend: `fixed` (code changed) · `guarded` (a check now fails if it returns) ·
`open` (known, not yet addressed, with the phase that will).

---

## 1. Silent success — the failure mode that costs the most

Everything in this class shares a shape: the system reported success while doing nothing, or
the wrong thing. None of them raised, none of them showed up in a log, and each was found
only because something else printed a number and that number was *impossible*.

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 1.1 | **Leash refused instead of pacing** (P1) | The first live recording collected **3 of 15** subreddits, then every further attempt was refused; the run reported `ok` | `SourceBudget` treated a rate-limit decision as terminal for the source instead of waiting for a slot | `SourceBudget.acquire()` waits for a slot with an injected sleep; its loop is bounded so a clock that does not advance with `sleep` cannot hang a run | A test asserts all 15 subreddits are collected; the ledger records `rate_limit_hits` per source, and the gate's L0 probe asserts real coverage |
| 1.2 | **Candidate-key order mismatch** (P2) | 12 candidates written, **0 score snapshots**, run reported `ok` | `upsert_candidates` returned keys as `phrase\x1fcategory`; the caller looked them up as `category\x1fphrase`, so the filter matched nothing | One `candidate_key()` helper, used by both sides | The report prints "0 new, 0 already present" rather than assuming, and the gate asserts `scored == snapshots_written` |
| 1.3 | **`dry_run` still wrote** (P1) | `--dry-run` skipped the ledger but wrote the raw lake | The flag was honoured in one code path (ledger) and not the other (lake) | Honoured in `collect_one`: a dry run reports what *would* be stored | A test asserts row counts are unchanged after a dry run, for both the lake and the snapshots |
| 1.4 | **A gate check that passed while failing** (P2) | The new P2 probe reported PASS with **empty** snapshot hashes | It parsed JSON with a bare `python` (not on PATH here) and piped the payload into `uv run python - <<HEREDOC` — where the heredoc **is** stdin, so the payload was never read | The payload goes through a temp file; the parse failure is fatal (`\|\| return 1`) | The check fails on a hash shorter than 32 chars, so "both sides failed identically" can never read as agreement |
| 1.5 | **`rowcount == -1` for `INSERT … ON CONFLICT DO NOTHING`** (P1) | "New signals: **-1**" | psycopg returns -1 for that statement rather than 0 | Counting moved to `RETURNING id` | The number is asserted non-negative where it is consumed |
| 1.6 | **A stub reported zero rather than skipping** (P1) | Twelve unimplemented L0 sources would have looked like sources that found nothing | The plugin loader had no way to say "not written yet"; the loader path treated it as a runtime failure | `SourceSkippedError` (skip ≠ empty batch); the ledger records `skipped` with the reason | A test asserts stub sources are recorded as `skipped`, and the health CLI counts them separately from `ok` |

**The rule this class earned:** *no number is reported that cannot be reconciled*. Every
count that reaches a human (items, new, scored, snapshots, spend) is either read back from
the database or asserted against a second source, and any counter that can go negative,
silent, or empty is treated as a bug in the reporting, not as a zero.

## 2. Statistics and arithmetic that were wrong but plausible

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 2.1 | **Percentiles on a population of one** (P2) | A category with one candidate produced sub-scores of 100, i.e. a perfect score from a single observation | Rank-based percentiles are undefined at n=1 and the natural implementation returns the top | `SINGLETON_PERCENTILE = 50.0` (an only child is neither best nor worst); `small_sample` flag when a category has < 5 candidates, printed as `*` in the table | Tests assert a single-candidate category scores exactly 50 and is flagged; §6.1's "per-category percentile" gets re-checked whenever weights change |
| 2.2 | **Budgets and plugins used different clocks** (P1) | Test pacing ran against wall time (**149 s → 9 s** after the fix); a replay could not reproduce a live run's pacing | Two `now()` calls in two modules | One clock per run, injected: `restore_budgets(..., clock_for=...)` | Tests pin every clock to the fixture's recording time; the gate's replay asserts an identical snapshot hash |
| 2.3 | **Three test expectations were the author's arithmetic, not the code's** (P2) | EWMA at α=0.5, a z-score against a population containing the value, and `log_compress`'s 0.5 point all "failed" | The tests were written from intuition about the formula rather than from the formula | Expectations recomputed by hand and the `log_compress` docstring corrected (it described a curve the code does not implement) | Where the spec fixes a formula (MGS weights), the test asserts the spec's numbers verbatim rather than a round-trip of the implementation |
| 2.4 | **Growth ratios without a floor** (P2) | A single prior mention produced an apparent +900% growth | `(recent - baseline) / baseline` with `baseline = 0` | `growth_ratio(..., floor=1.0)`, bounded to [-1, 9] | A test pins both the floor case and the cap |

**The rule this class earned:** *a statistic that cannot say "I don't know" will lie.* Every
score here can report "insufficient evidence" (`small_sample`, `skipped_no_evidence`,
`population`, a `None` watermark, an exit code of 2) and the UI is required to show it.

## 3. Data shape and unit errors

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 3.1 | **n-grams crossed a field separator** (P2) | "cat jarman cat jarman" — a phrase nobody said | Text was built as `entity \| quote` and n-grams spanned the join | Extraction is per segment (`\|`, `•`, ` -- `) | A test asserts no phrase contains a token twice across a separator |
| 3.2 | **Proper nouns matched generic keywords** (P2) | "Cat Jarman" (a biography) filed under *pets*; "children killing" under *baby products* | Category matching accepted any keyword hit, and "cat"/"children" are keywords | Each category has a `core` list of **buyable nouns**, and a phrase must contain one; keywords only break ties | `test_taxonomy_requires_a_buyable_noun`; the coverage script measures the match rate against the real lake instead of assuming it |
| 3.3 | **A fourth fad label violated the schema** (P2) | `dead` failed `scores.fad_label`'s CHECK constraint | The label set grew in code without changing the database | Staleness became a mining filter; the three spec labels stayed | The constraint is the guard — it rejected the drift at the first write, which is exactly what constraints are for |
| 3.4 | **Mixed units summed into one number** (P2) | Hacker News upvotes, Reddit scores and pageviews are not the same quantity | The first design summed signal values directly | Each signal is converted to a percentile **within its own (source, metric)** before anything is summed: an *attention index* | Tests assert per-source bounds and that no two sources share a population |
| 3.5 | **`slots=True` dataclasses have no `__dict__`** (P1) | The CLI's `--json` path crashed | `asdict()`/vars assumptions | Explicit `as_dict()` methods | The gate runs that exact CLI command, so the crash cannot come back unnoticed |

**The rule this class earned:** *units and identities belong in the type system, not in
convention.* Where a value crosses a boundary (source → lake → score), the transformation is
named (`normalize_payload`, attention percentile, `candidate_key`) rather than implied.

## 4. Environment and platform traps

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 4.1 | **130-second connections** (P0) | Every DB connection took 130 s, then succeeded | Windows resolves `localhost` to IPv6 `::1` first; WSL's relay black-holes it, so each attempt waited out the TCP timeout | DSN uses `127.0.0.1` (never `localhost`); migrations get `connect_timeout` + `lock_timeout` | Measured 130.09 s vs 0.06 s; the DSN is generated by `provision_pg.sh`, so nobody hand-writes it |
| 4.2 | **WSL kills idle distros** (P0) | Postgres died within a minute of the last `wsl` call, even with systemd enabled | WSL2 shuts down idle distributions | `db_up.sh` parks a keep-alive process | `db_up.sh` is idempotent and the health CLI reports a down database as exit 2 with a reason |
| 4.3 | **`str(sqlalchemy.URL)` masks the password as `***`** (P0) | Rebuilding a DSN authenticates as user `***` | SQLAlchemy hides secrets in `__str__` on purpose | `.render_as_string(hide_password=False)` where a DSN must be rebuilt | Comment at the call site; the test-suite DSN is built that way |
| 4.4 | **`alembic -x` is a global option**, and nested `uv run` deadlocks (P0) | `-x` after the subcommand is ignored; `uv run alembic` inside `uv run pytest` hangs on the project lock | CLI option placement; uv's lock is not re-entrant | `-x` precedes the subcommand; subprocesses use `sys.executable -m alembic` | Both are encoded in the tests and in `scripts/gate.sh` |
| 4.5 | **PyYAML keeps the last duplicate key silently** (P0) | A duplicated source id would silently drop a source | YAML spec behaviour, and PyYAML does not warn | The registry loader refuses duplicate keys (strict loader) | A test feeds a file with a duplicate and asserts the refusal |
| 4.6 | **`SourcePlugin.fetch/parse` were not abstract** (P0) | An incomplete plugin instantiated fine and failed only when called | Missing `@abstractmethod` | Both are abstract | Contract tests instantiate every stub and assert the refusal |

**The rule this class earned:** *an environment fact that cost more than ten minutes to
discover gets written down where it will be needed again* — in a script comment, a docstring,
or this file — and preferably encoded in a check rather than a sentence.

## 5. Process drifts (documentation, config, generated code)

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 5.1 | **Config drift** (P0) | `db:` in the settings model vs `database:` in the provisioning script, `.env.example` and the secrets template | Three sources of truth, no check | Unified on one key | A test fails if the committed template drifts from the model again |
| 5.2 | **Stubs that looked like implementations** (P0) | A module could return a plausible value and pass for finished | No marker for "not yet" | `STUB_PHASE` marker; anything callable in a stub must raise `NotImplementedError`, and a stub may not re-export working code | `tests/test_layout.py` enforces both directions |
| 5.3 | **Eval labels that were not labels** (P2) | 10 of 10 seeded cases came out `expected_keep: true` — a set that cannot catch keeping rubbish | The provisional threshold sat below every observed score | The threshold became a documented **median split**, stated in the file header | The header names the baseline's limits where a reader will meet them; the P3 Judge replaces the labels |
| 5.4 | **A heredoc-nested command mangled a batch of edits** (P1/P2, twice) | Beans bodies were half-written; a Python patch aborted mid-file | `<<'BODY'`/`<<'PY'` heredocs nested inside each other; a shell heredoc consumed the rest of the script | Everything long goes through `--body-file` / a written `.py` file | Rule adopted: no nested heredocs in build commands; long content is written to a file first |
| 5.6 | **A "wrap long lines" helper mangled string literals** (P5; fifth occurrence of the family 5.4 belongs to) | Five files ended up with `SyntaxError: unterminated string literal`, each right after an automated line-wrap pass over lines >100 chars | The wrapper broke on the last space before column 100 with no notion of string or f-string boundaries | Repaired by hand; long strings re-wrapped as implicit concatenation | **Rule:** never apply a text-transform (wrapper, regex, re-indenter) to source you have not parsed. For edits at that scale use the formatter (`ruff format`), the AST, or an editor that matches exact blocks — and run `ruff check` immediately after any mechanical edit, before anything else. It was the tripwire every single time |
| 5.7 | **`Path.write_text` writes CRLF on Windows, and CRLF breaks `.sh` inside WSL** (P5) | `scripts/staging_db.sh` died with `set: pipefail: invalid option name` | `write_text` applies the platform newline translation (`os.linesep`) unless `newline` is passed explicitly | Wrote it again with an explicit LF; converted the CRLF scripts on disk | `.gitattributes` already promised `*.sh text eol=lf` - the gap was the working tree, not the repo. Check a generated script with `python -c "print(open(f,'rb').read().count(b'
'))"` before trusting it |
"` is passed | Wrote it again with `newline="
"`; converted the two CRLF scripts on disk | `.gitattributes` already promised `*.sh text eol=lf` — the gap was the working tree, not the repo. Check with `python -c "print(open(f,'rb').read().count(b'
'))"` before trusting any generated script |
| 5.5 | **Fixing the symptom, not the class** (recurring near-miss) | Each bug above was fixed once, in one place | The reflex fix is local | After each bug: name the class, then add the guard for the class | This file. Each entry's **prevention** column is the deliverable, not the fix |

---

## 6. Live-only failures: what a mock cannot tell you (P3)

The brief asks for mocked provider responses *and then exactly one live run*. The live run
found four things no mock would have, and every one of them was a **configuration fiction**:

| # | Bug | Symptom | Root cause | Fix | Prevention |
|---|---|---|---|---|---|
| 6.1 | **Friendly model names are not model ids** | Three different chain drafts 404ed on every provider | "gemini-flash", "gemini-2.0-flash", "llama3.3-70b" (Cerebras serves only qwen-3.8-27b / gpt-oss-120b) | Chain carries ids read from `GET /v1beta/models` and `GET /v1/models` | The drill fails loudly on the first request; the ids are recorded in the source with the reason |
| 6.2 | **A models list is a catalogue, not a promise** | `GET /v1beta/models` listed `gemini-2.5-flash`; `generateContent` answered 404 "no longer available to new users" | Google advertises models a new key cannot call | Used `gemini-flash-latest` on `v1beta` (200 OK) | Never trust a capability list; make one real call |
| 6.3 | **A transient 5xx was treated as permanent** | Gemini answered 503 "experiencing high demand" for the ~450-token drill prompt while small prompts succeeded; the gate reported total failure | The gateway failed over on every provider error | Bounded transient retry (3 attempts) for 429/5xx/timeout, immediate failover for 404/402 | Two retry budgets tested separately: transient vs permanent, and a test asserts a 402 fails over at once |
| 6.4 | **One model per provider is a single point of failure** | With Groq unkeyed and Cerebras answering 402, one overloaded Gemini model meant a gate outage | The chain had nothing to fail over *to* | A second Gemini model in the chain, documented as a deviation with the live evidence | The chain is asserted by distinct-provider order, so an extra model cannot silently reorder providers |

**And two failures in the drill itself, which are the same class as §1 — a check that measured
nothing:**

* **Probes interfered through the cache.** A previous run's probe-2 call had warmed that exact
  key, so the next run's failover probe was "answered by cache" — green-looking and empty. Each
  probe now carries a fresh nonce; only the cache probe deliberately reuses probe 1's input.
* **`_TRANSIENT_ATTEMPTS` was a constant that lied.** Both retry rules (schema-once,
  transient-three) shared one 2-iteration loop, so the transient budget was unreachable. A test
  comparing the recorded attempt count against the constant caught it.

| 6.5 | **The free tier is smaller than the spec's budgets assume** | After ~9.4k tokens of drill runs, Gemini answers HTTP 429 for every request | Spec §6.2's caps (Judge ~20 calls, Writer ~30) are runaway backstops, not free-tier planning: one night is ~14k tokens | The gate degraded correctly with a reason; the *budget numbers* are now P4's decision (shrink to the tier, or fund a key, or let the 30-day cache carry the load) | The token log measures real spend per gate, so the next nightly plan is built on measured numbers instead of the spec's estimates |

**The rule this class earned:** *a mocked provider verifies the gateway; only a live provider
verifies the configuration.* The gateway logic was right the whole time — the model names, the
credits and the load were not, and no mock could have said so.

---

## Open items (known, with the phase that addresses them)

| # | Item | Why it is open | Phase |
|---|---|---|---|
| O1 | **Sub-scores clustered at 50 on a thin lake.** With 3 of 15 sources, most categories have 1-5 candidates and identical inputs, so percentiles are degenerate by construction | Not a code defect: the scorer is correct at this n, and it reports `small_sample`. It cannot be fixed by better maths | P4 (more sources) |
| O2 | **`SS` and `MP` are proxies.** Saturation is inferred from volume and corroboration; price from a category band | Neither is observable in the current sources | P4 (Tier-A: sold-vs-listed, reviews, prices) |
| O3 | **Eval cases have no runner.** 10 cases seeded; nothing executes them yet | The runner needs the LLM-judge gate to exist | P5 (`evals/run_evals.py`) |
| O4 | **Taxonomy coverage is 1.0%** against the real lake (Reddit 9.9%) | The taxonomy is aimed at product talk; the lake is mostly news and celebrity pageviews | P4, and re-measured after each collection change (`scripts/category_coverage.py`) |
| O5 | **No alerting.** The pipeline's failures are visible in health output and the ledger, but nothing pushes them | Deliberate: alerts without a runbook create noise | P5 (`monitor/alerts.py`) + the incident runbooks |
| O6 | **One operator, one machine.** Nothing pins the Postgres version, extensions or the Python patch level into a reproducible environment description | Acceptable while the system runs on one laptop; it is the first thing that breaks on a second | P5 (CI + environment lock) |

---

## The lessons, condensed

1. **Silent success is the worst outcome.** A run that reports `ok` while collecting 3 of 15
   subreddits is more dangerous than one that crashes: nobody looks. Every count that reaches
   a human must be reconcilable, and "0" should be harder to produce than an error.
2. **A check that cannot fail is not a check.** The P2 probe passed with empty hashes. Any
   assertion that compares two derived values must also assert they are non-empty — otherwise
   it only proves that two failures are equal.
3. **Constraints are cheaper than review.** The database's CHECK constraint rejected a fourth
   fad label at the first write, and caught a config drift that documentation had missed for a
   phase. Push invariants into the schema, then into types, then into tests, and only then
   into prose.
4. **Make the system able to say "I don't know".** `small_sample`, `skipped`, `fad_label`,
   `population`, a `None` watermark, exit code 2, `not_modified` — each is a place where the
   code refuses to guess. A pipeline that must always produce a number will produce a wrong one.
5. **Units and identities must be named.** Two of this project's worst bugs were a key whose
   field order lived in two people's heads and a sum of values that were never the same
   quantity. Name the transformation or it will be done differently in the next file.
6. **Every bug gets a class, and the class gets a guard.** The local fix restores the night's
   data; the guard is what keeps the next phase from re-earning the lesson.
7. **Environment facts do not belong in conversation.** The 130-second IPv6 connection, the
   WSL idle kill, the heredoc nesting, `alembic -x` placement — each cost real time twice,
   because the second time nobody remembered the first. They are written down here, in scripts,
   and where possible encoded as checks.
8. **Trust the number, not the report.** Three of these bugs were invisible in logs and
   obvious in a count. Reading output is a debugger; assuming output is a liability.
