"""Build the interactive results explorer: one self-contained HTML file (roadmap P5.5).

    uv run python -m trend_analyst.viewer.build --out docs/viewer/index.html

Why a static file and not a web app: the operator is one person on one machine, the data is
in Postgres a metre away, and a served application would need auth, a port and a process
supervisor to answer a question that a file answers. The page embeds its data as JSON, uses no
external assets (no CDN, no fonts, no framework — it must open offline and in five years), and
carries exactly the numbers the database holds.

**The rule this page follows: never show a number the system cannot justify.** Every figure is
labelled with where it came from —

* *stored* — read from a score snapshot: what the system decided, including which weights
  version decided it;
* *recomputed* — derived from the raw lake at page-build time, so a later collection can make
  it disagree with the stored snapshot; the page says so rather than quietly mixing the two;
* *not yet available* — the phases that will fill it (Judge verdicts and cited quotes in P3,
  Tier-A market data in P4, briefs in P5). These are rendered as explicit empty states, never
  as zeros, because a zero reads as a measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from config.categories import default_taxonomy
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.pipeline.layers.l3 import load_attention_points
from trend_analyst.scoring.features import extract_features
from trend_analyst.scoring.revenue import MODEL_V1
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import Candidate, Run, RunSourceLog, Score, SignalRow, Source
from trend_analyst.store.snapshots import RankedScore, latest_ranked

__all__ = ["ViewData", "build_view", "main", "render_page"]

TEMPLATE_PATH = Path(__file__).with_name("template.html")

#: How many example texts per item and per source. Enough to judge, few enough to read.
MAX_QUOTES = 6
#: Days of timeline shown per item.
TIMELINE_DAYS = 45


@dataclass(slots=True)
class ViewData:
    """Everything the page renders, already plain data (no ORM objects, no datetimes)."""

    generated_at: str
    as_of: str
    counts: dict[str, int] = field(default_factory=dict)
    #: The decide run the table is showing, or None when the page spans every run.
    view_run: dict[str, Any] | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "counts": self.counts,
            "view_run": self.view_run,
            "runs": self.runs,
            "sources": self.sources,
            "items": self.items,
            "pending": self.pending,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _item_evidence(
    session: Session,
    *,
    phrase: str,
    as_of: datetime,
) -> dict[str, Any]:
    """Recomputed-from-the-lake detail for one phrase: sources, quotes, timeline, features.

    Deliberately separate from the stored snapshot: this is what the lake holds *now*, which
    is what makes the page useful between runs and what makes the labels on it necessary.
    """
    normalized = phrase.lower()
    rows = session.execute(
        select(
            SignalRow.source_id,
            SignalRow.metric,
            SignalRow.value,
            SignalRow.ts,
            SignalRow.entity,
            SignalRow.quote,
            SignalRow.url,
        )
        .where(
            or_(
                func.lower(SignalRow.entity).like(f"%{normalized}%"),
                # A phrase mined from a body text must find its evidence: matching only the
                # title reported "0 signals" beside a feature panel full of computed numbers.
                func.lower(func.coalesce(SignalRow.quote, "")).like(f"%{normalized}%"),
            )
        )
        .order_by(SignalRow.ts.desc())
    ).all()

    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_source.setdefault(
            str(row.source_id),
            {"source": str(row.source_id), "signals": 0, "metrics": set(), "best_value": 0.0,
             "last_seen": None, "quotes": []},
        )
        bucket["signals"] += 1
        bucket["metrics"].add(str(row.metric))
        bucket["best_value"] = max(bucket["best_value"], float(row.value))
        timestamp = _iso(row.ts)
        if bucket["last_seen"] is None or str(timestamp) > str(bucket["last_seen"]):
            bucket["last_seen"] = timestamp
        if len(bucket["quotes"]) < MAX_QUOTES:
            text = " ".join(str(row.entity).split())
            quote = " ".join(str(row.quote).split()) if row.quote else ""
            bucket["quotes"].append(
                {"text": text[:280], "quote": quote[:400], "url": row.url, "ts": timestamp}
            )

    sources = [
        {**bucket, "metrics": sorted(bucket["metrics"])}
        for bucket in sorted(by_source.values(), key=lambda entry: -entry["signals"])
    ]

    # Timeline: signals per day, oldest first, so a spike is visible as a spike.
    start = (as_of - timedelta(days=TIMELINE_DAYS - 1)).date()
    buckets: dict[str, int] = {}
    for row in rows:
        day = row.ts.date()
        if day >= start:
            buckets[day.isoformat()] = buckets.get(day.isoformat(), 0) + 1
    timeline = [
        {"day": (start + timedelta(days=offset)).isoformat(),
         "count": buckets.get((start + timedelta(days=offset)).isoformat(), 0)}
        for offset in range(TIMELINE_DAYS)
    ]

    pending_points = load_attention_points(session, as_of=as_of, window_days=365)
    points = [
        point
        for point in pending_points
        if normalized in point.text.lower() or normalized in point.metric.lower()
    ]
    features: dict[str, Any] = {}
    if points:
        features = dict(extract_features(phrase, points, as_of=as_of).as_dict())

    return {
        "sources": sources,
        "timeline": timeline,
        "features": features,
        "signal_count": len(rows),
        "source_count": len(sources),
        "days_present": sum(1 for day in timeline if day["count"]),
    }


def _score_history(session: Session, candidate_id: int) -> list[dict[str, Any]]:
    """Every stored snapshot for one candidate, oldest first — the trend, not the table."""
    rows = session.execute(
        select(Score, Run)
        .join(Run, Run.id == Score.run_id)
        .where(Score.candidate_id == candidate_id)
        .order_by(Score.scored_at.asc())
    ).all()
    return [
        {
            "scored_at": _iso(score.scored_at),
            "run_id": str(score.run_id),
            "run_status": str(run.status),
            "weights_version": str(score.weights_version),
            "mgs": round(float(score.mgs), 2),
            "sub_scores": {
                "dv": round(float(score.demand_velocity), 2),
                "ss": round(float(score.saturation), 2),
                "sp": round(float(score.buyer_pain), 2),
                "mp": round(float(score.money), 2),
                "fe": round(float(score.feasibility), 2),
            },
            "fad_label": str(score.fad_label),
            "fad_probability": round(float(score.fad_probability), 4),
            "revenue": {
                "p10": round(float(score.revenue_p10), 2),
                "p50": round(float(score.revenue_p50), 2),
                "p90": round(float(score.revenue_p90), 2),
            },
        }
        for score, run in rows
    ]


def _item(
    session: Session,
    ranked: RankedScore,
    *,
    as_of: datetime,
    taxonomy_by_id: dict[str, Any],
) -> dict[str, Any]:
    """One candidate, fully unpacked: stored decision + recomputed evidence + provenance."""
    category = taxonomy_by_id.get(ranked.category)
    evidence = _item_evidence(session, phrase=ranked.phrase, as_of=as_of)
    return {
        "phrase": ranked.phrase,
        "category": ranked.category,
        "category_label": getattr(category, "label", ranked.category),
        "candidate_id": ranked.candidate_id,
        "mentions": ranked.mentions,
        # --- stored: the system's decision, reproducible from the snapshot table ---
        "stored": {
            "mgs": round(ranked.mgs, 2),
            "sub_scores": {
                "dv": round(ranked.demand_velocity, 2),
                "ss": round(ranked.saturation, 2),
                "sp": round(ranked.buyer_pain, 2),
                "mp": round(ranked.money, 2),
                "fe": round(ranked.feasibility, 2),
            },
            "gap": round(ranked.gap, 2),
            "fad": {"label": ranked.fad_label, "probability": round(ranked.fad_probability, 4)},
            "revenue": {
                "p10": round(ranked.revenue_p10, 2),
                "p50": round(ranked.revenue_p50, 2),
                "p90": round(ranked.revenue_p90, 2),
            },
            "weights_version": ranked.weights_version,
            "scored_at": _iso(ranked.scored_at),
            "run_id": str(ranked.run_id),
            "revenue_model": MODEL_V1.as_dict(),
        },
        # --- recomputed: what the lake holds now, labelled as such on the page ---
        "evidence": evidence,
        "history": _score_history(session, ranked.candidate_id),
        # --- the phases that will fill these ---
        "pending": {
            "judge": "P3 — keep/drop verdict with cited quotes",
            "quotes": "P3 — grounding-enforced quotes with URLs",
            "market": "P4 — sold-vs-listed, review counts, price distribution (Tier-A)",
            "brief": "P5 — Writer one-pager: verdict, players, risks, angles",
        },
    }


def _newest_scored_run(session: Session) -> Run | None:
    """The run that most recently wrote snapshots — what "the current results" means."""
    return session.execute(
        select(Run)
        .where(Run.id.in_(select(Score.run_id).distinct()))
        .order_by(Run.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def build_view(
    session: Session,
    *,
    limit: int = 200,
    as_of: datetime | None = None,
    config_dir: Path | None = None,
    all_runs: bool = False,
) -> ViewData:
    """Read everything the page needs. No network, no writes, one pass over the DB."""
    taxonomy = default_taxonomy(config_dir)
    taxonomy_by_id = {category.id: category for category in taxonomy.categories}

    newest_signal = session.execute(select(func.max(SignalRow.ts))).scalar()
    resolved_as_of = as_of or (
        (newest_signal if newest_signal.tzinfo else newest_signal.replace(tzinfo=UTC))
        if newest_signal is not None
        else datetime.now(UTC)
    )

    # Default to the newest decide run. Showing every run's newest snapshot mixes candidates
    # that a fixed mining rule would no longer produce with candidates it would — the first
    # build of this page ranked six phrases that the substring collapse had since retired,
    # which reads as a bug in the scorer rather than as history. History is one flag away.
    current_run = _newest_scored_run(session) if not all_runs else None
    ranked = latest_ranked(session, limit=limit, run_id=current_run.id if current_run else None)
    view_run = (
        {
            "run_id": str(current_run.id),
            "started_at": _iso(current_run.started_at),
            "finished_at": _iso(current_run.finished_at),
            "status": str(current_run.status),
            "trigger": str(current_run.trigger),
            "layer_status": current_run.layer_status or {},
            "notes": current_run.notes,
            "scope": "newest decide run",
        }
        if current_run
        else {"scope": "every run (newest snapshot per candidate)"}
    )
    items = [
        _item(session, row, as_of=resolved_as_of, taxonomy_by_id=taxonomy_by_id) for row in ranked
    ]

    run_rows = session.execute(
        select(Run).order_by(Run.started_at.desc()).limit(15)
    ).scalars().all()
    runs = [
        {
            "run_id": str(run.id),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "status": str(run.status),
            "trigger": str(run.trigger),
            "layer_status": run.layer_status or {},
            "notes": run.notes,
        }
        for run in run_rows
    ]

    source_rows = session.execute(select(Source).order_by(Source.id.asc())).scalars().all()
    last_touch: dict[str, dict[str, Any]] = {}
    touch_query = (
        select(
            RunSourceLog.source_id,
            RunSourceLog.status,
            RunSourceLog.started_at,
            RunSourceLog.items_fetched,
            RunSourceLog.items_new,
            RunSourceLog.reason,
        )
        .order_by(RunSourceLog.started_at.desc())
    )
    for row in session.execute(touch_query).all():
        last_touch.setdefault(
            str(row.source_id),
            {
                "status": str(row.status),
                "at": _iso(row.started_at),
                "items_fetched": int(row.items_fetched),
                "items_new": int(row.items_new),
                "reason": row.reason,
            },
        )
    sources = [
        {
            "id": str(source.id),
            "role": str(source.role),
            "tier": str(source.tier),
            "layers": list(source.layers),
            "schedule": str(source.schedule),
            "enabled": bool(source.enabled),
            "budget_per_day": int(source.budget_per_day),
            "rps": float(source.rps),
            "domains": list(source.domains),
            "watermark": source.watermark,
            "watermark_updated_at": _iso(source.watermark_updated_at),
            "last": last_touch.get(str(source.id)),
        }
        for source in source_rows
    ]

    def count_of(model: Any) -> int:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    counts = {
        "items": len(items),
        "candidates": count_of(Candidate),
        "snapshots": count_of(Score),
        "signals": count_of(SignalRow),
        "sources": len(sources),
        "sources_enabled": sum(1 for source in sources if source["enabled"]),
        "runs": count_of(Run),
    }

    return ViewData(
        generated_at=datetime.now(UTC).isoformat(),
        as_of=resolved_as_of.isoformat(),
        counts=counts,
        view_run=view_run,
        runs=runs,
        sources=sources,
        items=items,
        pending={
            "judge": "P3 — batched LLM keep/drop with cited quotes (nothing stored yet)",
            "enrich": (
                "P4 — Tier-A market data: sold-vs-listed counts, review volume, prices "
                "(no Tier-A source is enabled yet)"
            ),
            "brief": "P5 — Writer briefs, linked to a score snapshot",
            "alerts": "P5 — monitor alerts and incident runbooks",
            "evals": "P5 — the eval runner over the 10 seeded golden cases",
        },
    )


def render_page(data: ViewData) -> str:
    """Render the self-contained page: the template plus its JSON payload.

    The JSON is embedded rather than fetched for one reason: `file://` pages cannot fetch
    sibling files in several browsers, and requiring a local server to read your own results
    is exactly the friction this page exists to remove.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data.as_dict(), ensure_ascii=False, sort_keys=True)
    # `</script>` inside the payload would end the script block early; escaping the slash is
    # the standard fix and keeps the JSON parseable.
    payload = payload.replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trend_analyst.viewer.build",
        description="Build the interactive results explorer (one self-contained HTML file).",
    )
    parser.add_argument("--out", default="docs/viewer/index.html", help="output HTML path")
    parser.add_argument("--limit", type=int, default=200, help="max items to render")
    parser.add_argument(
        "--as-of", default=None, metavar="ISO8601", help="pin the reference instant"
    )
    parser.add_argument("--config-dir", default=None)
    parser.add_argument(
        "--all-runs",
        action="store_true",
        help="span every run instead of the newest decide run's snapshots",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the payload instead of writing HTML"
    )
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

    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            data = build_view(
                session,
                limit=args.limit,
                as_of=as_of,
                config_dir=config_dir,
                all_runs=args.all_runs,
            )
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(data.as_dict(), indent=2, ensure_ascii=False))
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_page(data), encoding="utf-8")
    scope = (data.view_run or {}).get("scope", "every run")
    print(
        f"wrote {out_path} · {data.counts['items']} items · {data.counts['snapshots']} snapshots · "
        f"{data.counts['signals']} signals · {scope} · as of {data.as_of}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
