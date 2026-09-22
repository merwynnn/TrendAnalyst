"""The nightly run: every layer in order, single-shot.

    uv run python -m scripts.nightly                 # the real thing
    uv run python -m scripts.nightly --offline       # fixtures + recorded gate answers, no network
    uv run python -m scripts.nightly --dry-run       # walk the plan, write nothing

Order is the specification's (§2): collect, mine, score, judge, write, then the TTL job.
A crash means re-running from scratch — dedup and idempotent inserts keep it cheap.

`--offline` is not a toy: it is how the gate exercises this whole path on every run, with recorded
HTTP fixtures for the sources and recorded provider answers for the gates. The free tier's quota is
a fact of life (LESSONS §6.5), so the nightly path has to be verifiable without spending anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from config.categories import TaxonomyError, default_taxonomy
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.llm.extract import heuristic_sender
from trend_analyst.llm.gates import judge_candidates, pending_judgements
from trend_analyst.llm.providers import settings_sender
from trend_analyst.llm.replay import ReplayMissError, load_fixture, replay_sender
from trend_analyst.llm.schemas import JudgeBatch, WriterBrief
from trend_analyst.llm.writer import stored_briefs, write_briefs
from trend_analyst.net import HttpPolicy, HttpxClient, fixture_path
from trend_analyst.pipeline.decide import run_decide
from trend_analyst.pipeline.layers.l2 import L2Report, enrich_candidates
from trend_analyst.pipeline.orchestrator import (
    _client_factory,
    _clock_factory,
    _limits_factory,
    run_l0,
)
from trend_analyst.pipeline.runs import close_run, open_run
from trend_analyst.sources.base import (
    FetchContext,
    PluginContractError,
    SourceBudget,
    SystemClock,
    load_plugin,
)
from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import Candidate, SignalRow
from trend_analyst.store.ttl import expire

__all__ = ["NightlyReport", "main", "run_nightly"]

#: Where the recorded gate answers live, for `--offline`.
JUDGE_FIXTURE = Path("tests/data/llm/judge_verdict.json")
WRITER_FIXTURE = Path("tests/data/llm/writer_brief.json")


@dataclass(slots=True)
class NightlyReport:
    """One night, from collection to reconciliation."""

    started_at: datetime
    offline: bool = False
    dry_run: bool = False
    l0_status: str = ""
    l0_items_new: int = 0
    decide_status: str = ""
    candidates_scored: int = 0
    l2_status: str = "skipped"
    l2_enriched: int = 0
    l2_spend: int = 0
    judge_status: str = ""
    judged: int = 0
    writer_status: str = ""
    briefs_written: int = 0
    ttl_summary: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Nothing crashed: collection and scoring finished, whatever they found."""
        return self.l0_status not in {"failed", ""} and self.decide_status not in {
            "failed",
            "",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "offline": self.offline,
            "dry_run": self.dry_run,
            "l0": {"status": self.l0_status, "items_new": self.l0_items_new},
            "decide": {"status": self.decide_status, "scored": self.candidates_scored},
            "l2": {
                "status": self.l2_status,
                "enriched": self.l2_enriched,
                "spend": self.l2_spend,
            },
            "judge": {"status": self.judge_status, "judged": self.judged},
            "writer": {"status": self.writer_status, "briefs": self.briefs_written},
            "ttl": self.ttl_summary,
            "notes": self.notes,
        }

    def render(self) -> str:
        lines = [
            f"nightly run — {'OFFLINE' if self.offline else 'LIVE'}"
            f"{' (dry run)' if self.dry_run else ''}",
            f"  L0 collect   {self.l0_status:<8} new signals: {self.l0_items_new}",
            f"  L1/L3 decide {self.decide_status:<8} candidates scored: {self.candidates_scored}",
            f"  L2 enrich    {self.l2_status:<8} candidates: {self.l2_enriched} "
            f"(spend {self.l2_spend})",
            f"  L3 judge     {self.judge_status:<8} judged: {self.judged}",
            f"  L3 writer    {self.writer_status:<8} briefs: {self.briefs_written}",
            f"  TTL          {self.ttl_summary}",
        ]
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.append("NIGHTLY: PASS" if self.ok else "NIGHTLY: FAIL — a layer failed")
        return "\n".join(lines)


def run_nightly(
    *,
    sessions: sessionmaker[Session],
    registry: Any,
    taxonomy: Any,
    settings: Any = None,
    as_of: datetime,
    engine: Any = None,
    offline: bool = False,
    dry_run: bool = False,
    fixtures_dir: Path | None = None,
    top_k: int = 5,
    judge_batch_size: int = 6,
    source_ids: Sequence[str] | None = None,
    extractor_sender: Any = None,
    judge_sender: Any = None,
    writer_sender: Any = None,
    run_ttl: bool = True,
) -> NightlyReport:
    """Run every layer once, single-shot.

    The gate senders are injected so the offline path can replay recorded answers (or the
    deterministic heuristic stand-in for extraction) and the tests can use stubs; in a
    live run the caller passes the real provider transport.
    """
    report = NightlyReport(started_at=as_of, offline=offline, dry_run=dry_run)
    source_ids, offline_note = _resolve_offline_sources(
        registry=registry, offline=offline, fixtures_dir=fixtures_dir, source_ids=source_ids
    )
    if offline_note:
        report.notes.append(offline_note)

    # --- L0: collect ---------------------------------------------------------
    l0 = run_l0(
        registry=registry,
        sessions=sessions,
        engine=engine,
        client_for=_client_factory(registry=registry, fixtures_dir=fixtures_dir, timeout_s=30.0),
        clock_for=_clock_factory(fixtures_dir=fixtures_dir),
        max_items_for=_limits_factory(fixtures_dir=fixtures_dir),
        trigger="nightly",
        source_ids=list(source_ids) if source_ids else None,
        dry_run=dry_run,
    )
    report.l0_status = l0.status
    report.l0_items_new = l0.items_new

    # --- L1/L3: extract and score --------------------------------------------
    decide = run_decide(
        sessions=sessions,
        taxonomy=taxonomy,
        as_of=as_of,
        trigger="nightly",
        source_ids=list(source_ids) if source_ids else None,
        sender=extractor_sender,
        dry_run=dry_run,
        top=top_k,
    )
    report.decide_status = decide.status
    report.candidates_scored = decide.scoring.scored

    # --- L2: enrich the top-K the Judge kept (only with a Tier-A credential) -----------------
    _record_l2(report, _run_l2(sessions=sessions, registry=registry, settings=settings,
                               offline=offline, dry_run=dry_run, top_k=top_k))

    # --- L3: judge, then write ----------------------------------------------
    with sessions() as session:
        queue = list(pending_judgements(session, limit=judge_batch_size * 2))
        run_id = open_run(session, trigger="nightly")
        session.commit()
        report.judge_status = "empty"
        if queue and judge_sender is not None:
            judged = judge_candidates(
                session,
                run_id=str(run_id),
                sender=judge_sender,
                candidates=queue,
                batch_size=judge_batch_size,
                dry_run=dry_run,
                bypass_cache=offline,
            )
            report.judge_status = judged.status
            report.judged = judged.judged
            report.notes.append(judged.summary())
        elif queue:
            report.judge_status = "skipped"
            report.notes.append("no gate transport available, so nothing was judged")

        kept = list(
            session.execute(
                select(Candidate).where(Candidate.status == "kept").order_by(Candidate.id)
            ).scalars().all()
        )
        if dry_run:
            # Close the run this stage opened: a dry run writes nothing, but a run row left
            # open would read as a run that never finished.
            close_run(
                session,
                run_id,
                status="aborted",
                notes="DRY RUN: the judge/writer stage wrote nothing",
            )
            session.commit()
        else:
            close_run(session, run_id, status="ok" if kept else "empty")
            session.commit()
        report.writer_status = "empty"
        if kept and writer_sender is not None:
            written = write_briefs(
                session,
                run_id=str(run_id),
                sender=writer_sender,
                candidates=kept[:top_k],
                top_k=top_k,
                dry_run=dry_run,
                bypass_cache=offline,
            )
            report.writer_status = written.status
            report.briefs_written = written.written
            report.notes.append(written.summary())
        elif kept:
            report.writer_status = "skipped"
            report.notes.append("no gate transport available, so no brief was written")

    # --- maintenance ----------------------------------------------------------
    with sessions() as session:
        if run_ttl:
            ttl = expire(session, now=as_of, dry_run=dry_run)
            report.ttl_summary = ttl.summary()
            if not dry_run:
                session.commit()
        else:
            report.ttl_summary = "skipped"
    return report


def _record_l2(report: NightlyReport, l2: L2Report) -> None:
    """Copy an L2 outcome into the nightly report, noting it when it was not a full run."""
    report.l2_status = l2.status
    report.l2_enriched = l2.enriched
    report.l2_spend = l2.quota_spent
    if l2.status not in {"ok", "empty"}:
        report.notes.append(l2.reason or l2.summary())


def _tier_a_plugins(
    registry: Any, settings: Any
) -> tuple[dict[str, Any], dict[str, str]]:
    """Load every enabled Tier-A plugin, with credentials injected where asked.

    Returns (plugins, skipped): a source whose module is still a stub lands in skipped
    with the reason — an enabled entry that cannot run is a configuration fact, and the
    nightly report names it instead of silently narrowing coverage.
    """
    plugins: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for entry in registry.by_tier("A"):
        if not entry.enabled:
            continue
        try:
            plugins[entry.id] = load_plugin(entry, settings=settings)
        except PluginContractError as exc:
            skipped[entry.id] = f"no plugin yet: {exc}"
    return plugins, skipped


def _run_l2(
    *,
    sessions: Any,
    registry: Any,
    settings: Any,
    offline: bool,
    dry_run: bool,
    top_k: int,
) -> Any:
    """Run L2 enrichment when a Tier-A source is both registered and credentialed.

    Offline runs skip it entirely: a Tier-A source needs a network and a credential, and neither
    exists in a replay. Saying so is the point — an "empty" L2 that silently did nothing would read
    as "no candidate needed enrichment".
    """
    if offline:
        return L2Report(status="skipped", reason="offline run: Tier-A sources need a network")
    plugins, skipped = _tier_a_plugins(registry, settings)
    if not plugins:
        return L2Report(status="skipped", reason="no enabled Tier-A plugin to run")
    by_id = {entry.id: entry for entry in registry.by_tier("A")}
    budgets = {
        source_id: SourceBudget(
            source_id=source_id,
            budget_per_day=by_id[source_id].budget_per_day,
            rps=by_id[source_id].rps,
            clock=SystemClock(),
        )
        for source_id in plugins
    }

    def client_for(source_id: str) -> Any:
        return HttpxClient(
            source_id=source_id,
            allowed_domains=by_id[source_id].domains,
            policy=HttpPolicy(timeout_s=30.0),
        )

    with sessions() as session:
        run_id = open_run(session, trigger="nightly")
        session.commit()
        report = enrich_candidates(
            session,
            run_id=run_id,
            plugins=plugins,
            source_ids=[entry.id for entry in registry.by_tier("A") if entry.id in plugins],
            top_k=top_k,
            budgets=budgets,
            dry_run=dry_run,
            client_for=client_for,
            clock=SystemClock(),
            context_factory=FetchContext,
        )
        for source_id, reason in skipped.items():
            report.skipped_sources[source_id] = reason
        if not dry_run:
            session.commit()
        else:
            close_run(session, run_id, status="aborted", notes="DRY RUN: L2 wrote nothing")
            session.commit()
    return report


def _offline_sources(fixtures_dir: Path, registry: Any) -> list[str]:
    """The enabled sources that have a recorded HTTP fixture, in registry order."""
    out: list[str] = []
    for entry in registry.sources:
        if not entry.enabled:
            continue
        if fixture_path(fixtures_dir, entry.id).is_file():
            out.append(entry.id)
    return out


def _resolve_offline_sources(
    *,
    registry: Any,
    offline: bool,
    fixtures_dir: Path | None,
    source_ids: Sequence[str] | None,
) -> tuple[Sequence[str] | None, str]:
    """Narrow an offline run to the sources that were actually recorded, and say what was left out.

    Offline means "replay what was recorded": a source with no fixture cannot be replayed, and
    asking for it would fail with a fixture miss. What is dropped is named, because a silently
    narrowed run is exactly the kind of thing that later reads as full coverage.
    """
    if not (offline and fixtures_dir is not None and not source_ids):
        return source_ids, ""
    replayable = _offline_sources(fixtures_dir, registry)
    enabled = [entry.id for entry in registry.sources if entry.enabled]
    missing = ", ".join(sorted(set(enabled) - set(replayable)))
    return replayable, (
        f"offline: replaying {len(replayable)} recorded source(s) of {len(enabled)} enabled; "
        f"not recorded: {missing}"
    )


def _run_ttl(session: Session, *, as_of: datetime, dry_run: bool) -> Any:
    """Expire what the TTL policy allows, and hand back the report for the nightly summary."""
    return expire(session, now=as_of, dry_run=dry_run)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.nightly",
        description="Run every pipeline layer once, single-shot.",
    )
    parser.add_argument(
        "--offline", action="store_true", help="fixtures and recorded gate answers"
    )
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--fixtures", default="tests/data", help="fixture directory for --offline")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--judge-batch-size", type=int, default=6)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--no-ttl", action="store_true", help="skip the retention job")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        settings = load_settings(config_dir)
        registry = load_registry(default_registry_path(config_dir))
        taxonomy = default_taxonomy(config_dir)
    except (ConfigError, TaxonomyError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        engine = create_db_engine(settings)
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    fixtures_dir = Path(args.fixtures) if args.offline else None
    try:
        extractor_sender, judge_sender, writer_sender = _gate_senders(
            args, settings
        )
    except ReplayMissError as exc:
        print(f"offline replay unavailable: {exc}", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    try:
        as_of = _resolve_as_of(sessions)
        report = run_nightly(
            sessions=sessions,
            registry=registry,
            taxonomy=taxonomy,
            settings=settings,
            as_of=as_of,
            engine=engine,
            offline=args.offline,
            dry_run=args.dry_run,
            fixtures_dir=fixtures_dir,
            top_k=args.top_k,
            judge_batch_size=args.judge_batch_size,
            source_ids=args.source or None,
            extractor_sender=extractor_sender,
            judge_sender=judge_sender,
            writer_sender=writer_sender,
            run_ttl=not args.no_ttl,
        )
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.render())
        briefs = stored_briefs_count(sessions)
        if briefs:
            print(f"  briefs on file: {briefs}")
    return 0 if report.ok else 1


def _gate_senders(args: Any, settings: Any) -> tuple[Any, Any, Any]:
    """The three gate transports: replayed or heuristic offline, live otherwise.

    Extraction has no recorded answers for arbitrary lake chunks, so the offline path
    uses the deterministic heuristic stand-in: valid refs, no spend, plumbing only.
    """
    if args.offline:
        return (
            heuristic_sender(),
            replay_sender(
                load_fixture(JUDGE_FIXTURE), schema=JudgeBatch, covered=["circ saw"]
            ),
            replay_sender(
                load_fixture(WRITER_FIXTURE), schema=WriterBrief, covered=["circ saw"]
            ),
        )
    transport = settings_sender(settings)
    return transport, transport, transport


def _resolve_as_of(sessions: sessionmaker[Session]) -> datetime:
    """The newest signal's timestamp, or now when the lake is empty.

    Using the data's own clock keeps a replay reproducible; `now` is the fallback for the very first
    run, when there is no data yet.
    """
    with sessions() as session:
        statement = select(SignalRow.ts).order_by(SignalRow.ts.desc()).limit(1)
        newest = session.execute(statement).scalar()
    if newest is None:
        return datetime.now(UTC)
    return newest if newest.tzinfo else newest.replace(tzinfo=UTC)


def stored_briefs_count(sessions: sessionmaker[Session]) -> int:
    """How many briefs exist, for the nightly footer."""
    with sessions() as session:
        return len(stored_briefs(session, limit=100))


if __name__ == "__main__":
    raise SystemExit(main())
