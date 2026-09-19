"""Offline judge replay: the whole gate, over a real recorded model answer (no network).

    uv run python -m scripts.judge_replay             # newest decide run's candidates
    uv run python -m scripts.judge_replay --json

Why this exists: the live drill proved the gateway against a provider, but it needs a network and
a quota — and the quota is a real constraint (LESSONS §6.5: a day of drilling exhausted the free
tier). This replays a **real provider answer** captured by that drill
(`tests/data/llm/judge_verdict.json`) against the actual candidate table, so the Judge gate can be
exercised end to end at any time, including inside the gate script, without spending anything.

What it does *not* do: pretend the answer is fresh. The verdict is replayed for the candidates it
was answered about, its `recorded_at` and model travel with the output, and any candidate the
recorded verdict does not cover is reported as missing rather than invented.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.llm.gates import judge_candidates, pending_judgements
from trend_analyst.llm.replay import ReplayMissError, load_fixture, replay_sender
from trend_analyst.llm.schemas import JudgeVerdict
from trend_analyst.pipeline.state import finish_run, start_run
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import Candidate, Judgement

__all__ = ["main"]

DEFAULT_FIXTURE = Path("tests/data/llm/judge_verdict.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.judge_replay",
        description="Replay a recorded provider answer through the Judge gate (offline).",
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="do not write judgements")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        load_settings(config_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        engine = create_db_engine()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    try:
        fixture = load_fixture(Path(args.fixture))
    except ReplayMissError as exc:
        print(f"replay unavailable: {exc}", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    recorded_phrase = str(JudgeVerdict.model_validate(fixture["output"]).phrase)
    try:
        with sessions() as session:
            # The recording names one phrase. Judge an explicit batch: that phrase (so the replay
            # applies) plus whatever the newest run left unjudged (so the miss path is exercised).
            named = session.execute(
                select(Candidate).where(Candidate.phrase == recorded_phrase)
            ).scalars().first()
            queue = list(pending_judgements(session, limit=args.limit))
            if named is not None:
                queue = [named] + [item for item in queue if int(item.id) != int(named.id)]

            if not queue:
                print("no candidates to judge: run the collect and decide layers first")
                return 1

            handle = start_run(session, trigger="manual", resume=False)
            session.commit()
            report = judge_candidates(
                session,
                run_id=handle.run_id,
                sender=replay_sender(
                    fixture, covered=[str(candidate.phrase) for candidate in queue]
                ),
                candidates=queue,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                finish_run(
                    session,
                    str(handle.run_id),
                    status="ok" if report.ok else "degraded",
                    layer_status={"L3-judge": report.as_dict()},
                    notes=f"{report.summary()} (replay of {fixture['recorded_at']})",
                )
                session.commit()
            rows = session.execute(select(Judgement)).scalars().all()
    finally:
        engine.dispose()

    lines = [
        "Judge replay (offline, recorded provider answer)",
        f"recording: {args.fixture}",
        f"recorded: {fixture['recorded_at']} model={fixture['model']} "
        f"tokens={fixture['prompt_tokens']}+{fixture['completion_tokens']}",
        f"replayed verdict: {recorded_phrase} -> "
        f"{JudgeVerdict.model_validate(fixture['output']).decision}",
        report.summary(),
        f"judgements now in the table: {len(rows)}",
    ]
    payload = {
        "fixture": args.fixture,
        "recorded_at": fixture["recorded_at"],
        "model": fixture["model"],
        "report": report.as_dict(),
        "judgements": len(rows),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("\n".join(lines))
        print("\nREPLAY: PASS" if report.ok else "\nREPLAY: FAIL")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
