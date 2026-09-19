"""Seed golden eval cases from real pipeline output (build brief P2: "Seed 10 golden eval
cases from real outputs").

    uv run python -m scripts.seed_evals --count 10

A golden case is a frozen input plus the behaviour the system must show on it. Two things
make these worth having rather than decorative:

* **They come from a real run.** Each case records the run id, the snapshot hash and the
  candidate's actual evidence (mentions, sources, the texts it was mined from), so a case can
  be traced back to the night it was observed. Invented cases test the inventor.
* **They are a BASELINE, not ground truth.** The band around each MGS is ±5 points and the
  keep/drop expectation comes from a documented provisional rule (`KEEP_THRESHOLD`, below)
  rather than from a human judgement — the Judge gate (P3) is what will supply real
  judgements, and its disagreement with these labels is the signal to re-label them. The
  YAML says so per case, so nobody later mistakes the baseline for a verdict.

The runner (`evals/run_evals.py`) lands in P5 with the other 40 cases; this script writes the
file and mirrors it into `eval_cases`, keeping the two in step.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.categories import default_taxonomy
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.pipeline.layers.l1 import DEFAULT_MIN_KEEP, DEFAULT_PRUNE_FRACTION
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import EvalCase, SignalRow
from trend_analyst.store.snapshots import RankedScore, latest_ranked

__all__ = ["KEEP_THRESHOLD_LABEL", "SEED_PREFIX", "build_cases", "main"]

#: Provisional keep/drop rule for the P2 baseline: the **median MGS of the seeded set**.
#:
#: A fixed threshold was tried first (52.0) and produced ten "keep" cases out of ten, because
#: on a thin lake the scores cluster in a three-point range — a case set where everything is
#: "keep" cannot catch the failure that matters most, which is keeping rubbish. Splitting at
#: the median guarantees both labels appear, is stated in the file, and does not pretend to
#: be a judgement about product quality. It is a baseline: the P3 Judge replaces it.
KEEP_THRESHOLD_LABEL = "median of the seeded set"

#: Half-width of the score band. Wide enough that a legitimately improved scores do not fail
#: the case; narrow enough that a broken formula does.
BAND: float = 5.0

SEED_PREFIX = "P2"
#: How many signals to freeze per case. Enough to explain the number, few enough to read.
MAX_SIGNALS = 5


def _evidence(session: Any, phrase: str, as_of: datetime) -> list[dict[str, Any]]:
    """The signals behind one candidate: source, timestamp, value, and a redacted text."""
    rows = session.execute(
        select(SignalRow.source_id, SignalRow.ts, SignalRow.value, SignalRow.entity)
        .where(SignalRow.entity.ilike(f"%{phrase}%"))
        .order_by(SignalRow.ts.desc())
        .limit(MAX_SIGNALS)
    ).all()
    return [
        {
            "source": str(row.source_id),
            "ts": str(row.ts),
            "value": float(row.value),
            "text": " ".join(str(row.entity).split())[:200],
        }
        for row in rows
    ]


def build_cases(
    session: Any,
    *,
    count: int,
    taxonomy_ids: Sequence[str],
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Build ``count`` cases from the newest snapshots, spread across categories.

    Round-robin over categories rather than the top-N by score: ten cases from one category
    would all fail together, and a case set that cannot distinguish a category-specific bug
    from a global one is a case set with a blind spot.
    """
    ranked = latest_ranked(session, limit=500)
    if not ranked:
        return []

    by_category: dict[str, list[RankedScore]] = {}
    for row in ranked:
        if row.category in taxonomy_ids:
            by_category.setdefault(row.category, []).append(row)

    ordered: list[RankedScore] = []
    queues = [list(rows) for _, rows in sorted(by_category.items())]
    while any(queues) and len(ordered) < count:
        for queue in queues:
            if queue and len(ordered) < count:
                ordered.append(queue.pop(0))

    # The median split, computed over the cases that will actually be seeded.
    scores = sorted(row.mgs for row in ordered)
    threshold = scores[len(scores) // 2] if scores else 0.0

    cases: list[dict[str, Any]] = []
    for index, row in enumerate(ordered, start=1):
        signals = _evidence(session, row.phrase, as_of)
        cases.append(
            {
                "id": f"{SEED_PREFIX.lower()}-{index:02d}-{row.category}-"
                f"{row.phrase.replace(' ', '-')[:32]}",
                "category": row.category,
                "phrase": row.phrase,
                "input_signals": signals,
                "expected_keep": row.mgs >= threshold,
                "expected_fad_label": row.fad_label,
                "score_band": {
                    "min": round(max(row.mgs - BAND, 0.0), 2),
                    "max": round(min(row.mgs + BAND, 100.0), 2),
                },
                "baseline_score": round(row.mgs, 2),
                "weights_version": row.weights_version,
                "notes": (
                    f"seeded from run {row.run_id} on {as_of:%Y-%m-%d}; "
                    f"{row.mentions} mentions; observed MGS {row.mgs:.2f}; "
                    "expected_keep from the provisional P2 rule (threshold "
                    f"{threshold:.2f}, the {KEEP_THRESHOLD_LABEL}; the P3 Judge replaces "
                    "this judgement)"
                ),
            }
        )
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_evals",
        description="Seed golden eval cases from the newest score snapshots.",
    )
    parser.add_argument("--count", type=int, default=10, help="how many cases to seed")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--out", default=None, help="cases.yaml path (default evals/cases.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    out_path = Path(args.out) if args.out else root / "evals" / "cases.yaml"
    try:
        load_settings(config_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    taxonomy = default_taxonomy(config_dir)
    try:
        engine = create_db_engine()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            newest = select(SignalRow.ts).order_by(SignalRow.ts.desc()).limit(1)
            as_of = session.execute(newest).scalar()
            if as_of is None:
                print("the lake is empty: run L0 first", file=sys.stderr)
                return 1
            as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
            cases = build_cases(
                session, count=args.count, taxonomy_ids=taxonomy.ids, as_of=as_of
            )
            if not cases:
                print("no score snapshots: run --layers L1,L3 first", file=sys.stderr)
                return 1
            if not args.dry_run:
                # Mirror into eval_cases. Cases are re-seeded as a set, so the table is
                # replaced: a stale case that no longer appears in cases.yaml would fail CI
                # forever with no way to fix it.
                session.execute(delete(EvalCase))
                session.execute(
                    pg_insert(EvalCase).values(
                        [
                            {
                                "id": case["id"],
                                "category": case["category"],
                                "input_signals": case["input_signals"],
                                "expected_keep": case["expected_keep"],
                                "expected_fad_label": case["expected_fad_label"],
                                "score_min": case["score_band"]["min"],
                                "score_max": case["score_band"]["max"],
                                "notes": case["notes"],
                                "baseline_score": case["baseline_score"],
                            }
                            for case in cases
                        ]
                    )
                )
                session.commit()
    finally:
        engine.dispose()

    counts = {
        "keep": sum(1 for case in cases if case["expected_keep"]),
        "drop": sum(1 for case in cases if not case["expected_keep"]),
        "fad": sum(1 for case in cases if case["expected_fad_label"] == "fad"),
    }
    if args.dry_run:
        print(json.dumps(cases, indent=2)[:2000])
        print(f"\n{len(cases)} cases ({counts}) - dry run, nothing written")
        return 0

    threshold = min(
        (case["score_band"]["min"] + BAND for case in cases), default=0.0
    )
    out_path.write_text(_render_yaml(cases, counts, threshold), encoding="utf-8")
    print(f"seeded {len(cases)} cases into {out_path} ({counts}), threshold {threshold:.2f}")
    print(f"mirrored into eval_cases; prune fraction kept at {DEFAULT_PRUNE_FRACTION}, "
          f"min_keep {DEFAULT_MIN_KEEP}")
    return 0


def _render_yaml(cases: list[dict[str, Any]], counts: dict[str, int], threshold: float) -> str:
    """Write the case file with its provenance header.

    The header states the baseline's limits in the file itself, because that is where a
    future reader will meet these numbers.
    """
    header = f"""# ---------------------------------------------------------------------------
# Trend Analyst — golden eval cases (spec §8, build brief P2).
#
# Seeded from REAL pipeline output on {datetime.now(UTC):%Y-%m-%d}: {len(cases)} cases
# ({counts["keep"]} keep, {counts["drop"]} drop, {counts["fad"]} fad). Regenerate with
#       uv run python -m scripts.seed_evals --count 10
#
# READ THIS BEFORE TRUSTING A LABEL: these are BASELINE expectations, not verdicts.
#   * expected_keep comes from a provisional MGS threshold ({threshold:.2f}, the
#     {KEEP_THRESHOLD_LABEL}), not from a human judgement. The P3 Judge gate supplies real
#     judgements; when it disagrees with a label here, the label is what is wrong.
#   * score_band is ±{BAND} points around the observed MGS, so it catches a broken formula, not
#     a better one.
#   * Each case keeps its provenance in `notes` (the run it came from) and its evidence in
#     input_signals, so any label can be re-derived and argued with.
#
# 10 cases in P2, 50 by P5. The runner (evals/run_evals.py) lands in P5.
# ---------------------------------------------------------------------------
version: 1
seeded_at: "{datetime.now(UTC).isoformat()}"
cases:
"""
    body = yaml.safe_dump(
        cases, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100
    )
    return header + body


if __name__ == "__main__":
    raise SystemExit(main())
