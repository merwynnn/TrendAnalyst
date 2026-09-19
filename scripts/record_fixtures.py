"""Record HTTP fixtures for a source plugin — the ONLY thing here that touches the web.

    uv run python -m scripts.record_fixtures --source hn_firebase
    uv run python -m scripts.record_fixtures --all

Why this exists: tests must never call the network (build brief §2), and a hand-written
JSON blob proves nothing about the endpoint it pretends to describe. So the payloads are
recorded once, from the real API, committed as `tests/data/http/<source>.json`, and replayed
forever after. When an API changes shape, re-recording is a deliberate, reviewable act —
and the diff shows exactly what changed.

Politeness: the recorder drives the *real* plugin through the same budget the pipeline
uses, so it makes exactly the requests a run would, at the registry's rate limit, and
stops at the source's daily budget. It is bounded: item limits are deliberately small.

Safety: only `content-type` and `retry-after` headers are kept, so no cookie or
authorization header can ever reach a committed file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from config.settings import default_config_dir
from trend_analyst.net import HttpPolicy, HttpxClient, RecordingHttpClient, fixture_path
from trend_analyst.sources.base import (
    FetchContext,
    SourceBudget,
    SourcePlugin,
    SystemClock,
    load_plugin,
)
from trend_analyst.sources.registry import SourceEntry, default_registry_path, load_registry
from trend_analyst.store.db import create_session_factory  # noqa: F401  (kept for parity)

__all__ = ["main", "record_source"]

#: Enough to exercise every parse branch, small enough to review in a diff. Per source,
#: because one Reddit post is ~3 KB while one HN story is ~0.5 KB.
RECORD_ITEM_LIMITS: dict[str, int] = {
    "hn_firebase": 12,
    "wiki_pageviews": 3,   # days, not items: the API returns a fixed top-1000 per day
    "arctic_shift": 5,     # posts per subreddit
}
DEFAULT_RECORD_ITEM_LIMIT = 8


def record_source(
    entry: SourceEntry,
    *,
    plugin: SourcePlugin,
    fixtures_dir: Path,
    budget_per_day: int | None = None,
    passes: int = 2,
) -> Path:
    """Run one plugin against the real API and write its fixture file.

    Two passes by default. The first is a cold start (no watermark); the second runs with
    the cursor the first one produced, which is the *incremental* request shape the
    pipeline will use every night afterwards. Recording only the cold start would leave
    the watermark path untested — and the watermark path is most of P1.
    """
    clock = SystemClock()
    budget = SourceBudget(
        source_id=entry.id,
        # A recording is not a run: a small budget keeps a mistake from hammering an API,
        # while still covering both passes.
        budget_per_day=budget_per_day or 60,
        rps=entry.rps,
        clock=clock,
    )
    recorder = RecordingHttpClient(
        HttpxClient(
            source_id=entry.id,
            allowed_domains=entry.domains,
            policy=HttpPolicy(timeout_s=30.0, max_attempts=2),
            on_retry=lambda url, attempt, wait, detail: print(
                f"    retry {attempt} in {wait:.0f}s ({detail})", file=sys.stderr
            ),
        ),
        source_id=entry.id,
    )
    limit = RECORD_ITEM_LIMITS.get(entry.id, DEFAULT_RECORD_ITEM_LIMIT)
    run_id = f"record-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    journal: list[dict[str, object]] = []
    cursor: str | None = None
    last_batch = None
    try:
        for pass_index in range(1, passes + 1):
            ctx = FetchContext(
                run_id=f"{run_id}-p{pass_index}",
                source_id=entry.id,
                client=recorder,
                budget=budget,
                clock=clock,
                cursor=cursor,
                max_items=limit,
            )
            batch = plugin.fetch(ctx)
            signals = plugin.parse(batch)
            journal.append(
                {
                    "pass": pass_index,
                    "cursor_in": cursor,
                    "cursor_out": batch.cursor,
                    "not_modified": batch.not_modified,
                    "items": batch.item_count,
                    "requests": batch.request_count,
                    "signals": len(signals),
                }
            )
            cursor = batch.cursor
            last_batch = batch
    finally:
        inner = recorder._inner
        close = getattr(inner, "close", None)
        if callable(close):
            close()

    path = fixture_path(fixtures_dir, entry.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = recorder.fixture(recorded_at=datetime.now(UTC).isoformat(timespec="seconds"))
    payload["note"] = (
        "Recorded from the live API by scripts/record_fixtures.py. Replayed by tests; "
        "never fetched by them. Header values are limited to content-type and retry-after."
    )
    payload["passes"] = journal
    payload["max_items"] = limit
    payload["signals_parsed"] = journal[-1]["signals"] if journal else 0
    payload["items"] = last_batch.item_count if last_batch is not None else 0
    payload["source_id"] = entry.id
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.record_fixtures",
        description="Record real HTTP responses into tests/data/http/ (needs network).",
    )
    parser.add_argument("--source", action="append", default=[], help="source id; repeatable")
    parser.add_argument("--all", action="store_true", help="record every source that has a plugin")
    parser.add_argument("--config-dir", default=None, help="directory holding sources.yaml")
    parser.add_argument("--fixtures-dir", default="tests/data", help="where fixtures are written")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    registry = load_registry(default_registry_path(config_dir))
    fixtures_dir = Path(args.fixtures_dir)

    wanted = set(args.source)
    targets = [
        entry
        for entry in registry.sources
        if (args.all or entry.id in wanted) and entry.layers and entry.schedule != "on_demand"
    ]
    if not targets:
        print("nothing to record: pass --source <id> or --all", file=sys.stderr)
        return 1

    failures = 0
    for entry in targets:
        print(f"recording {entry.id} ({entry.tier}-tier, {len(entry.domains)} host(s))…")
        try:
            plugin = load_plugin(entry)
        except Exception as exc:
            print(f"  skipped: {type(exc).__name__}: {exc}")
            continue
        try:
            path = record_source(entry, plugin=plugin, fixtures_dir=fixtures_dir)
        except Exception as exc:
            failures += 1
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue
        print(f"  wrote {path}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
