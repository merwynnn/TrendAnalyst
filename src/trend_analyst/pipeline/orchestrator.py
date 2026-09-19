"""The pipeline's entry point: L0 collection (P1) and the L1+L3 decide run (P2).

L0 collects; L1 mines; L3 scores. Each layer's own module holds its logic — this file owns
the CLI, the layer selection and the L0 loop below.

The loop is deliberately dull — deterministic code, checkpoints, no agent framework:

for each enabled L0 source, in the order config/sources.yaml lists them:
    leash -> fetch -> dedup by content hash -> parse -> store -> watermark -> ledger

Four properties are the reason this file exists:

1. **File order is execution order** (spec §4.2). The registry decides; nothing here has
   an opinion about which source matters.
2. **A source that fails does not stop the run** (§4.3). Its failure is recorded with the
   reason and the loop continues, because one dead endpoint must not cost a night's data.
3. **Identical payloads are not parsed twice** (§5.2). The content hash decides, and the
   ledger records zero new items — which is what makes a second run cheap.
4. **A resume executes only unfinished work** (§5.4). "Finished" is a query against the
   ledger, so it survives a crash: `run_l0()` after `run_l0()` picks up where it stopped.

Quota spend is written to `quota_ledger` under a compare-and-swap key per (run, source),
so re-running a source — exactly what a resume does — cannot charge it twice.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from config.categories import TaxonomyError, default_taxonomy
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.llm.gates import judge_candidates, pending_judgements
from trend_analyst.llm.providers import settings_sender
from trend_analyst.llm.replay import ReplayMissError, load_fixture, replay_sender
from trend_analyst.llm.schemas import JudgeVerdict
from trend_analyst.logging import configure_logging, get_logger, log_event
from trend_analyst.net import (
    EgressDeniedError,
    HttpFetchError,
    HttpPolicy,
    HttpxClient,
    RecordedHttpClient,
    fixture_path,
)
from trend_analyst.pipeline.decide import read_ranked, run_decide
from trend_analyst.pipeline.layers.l1 import DEFAULT_MIN_KEEP, DEFAULT_PRUNE_FRACTION
from trend_analyst.pipeline.state import (
    RunHandle,
    advance_watermark,
    finish_run,
    pending_sources,
    read_watermark,
    record_source_result,
    record_spend,
    restore_budgets,
    spent_today,
    start_run,
)
from trend_analyst.sources.base import (
    HTTP_CLIENT_ERROR,
    FetchContext,
    FixedClock,
    PluginContractError,
    RawBatch,
    SourceBudget,
    SourcePlugin,
    SourceSkippedError,
    SystemClock,
    load_plugin,
)
from trend_analyst.sources.registry import (
    Registry,
    SourceEntry,
    default_registry_path,
    load_registry,
)
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import Candidate, RawItem, SignalRow
from trend_analyst.store.sync import sync_sources

__all__ = ["RunReport", "SourceOutcome", "collect_one", "run_l0"]

log = get_logger("pipeline.orchestrator")

#: How many of the newest signals in a batch are considered "new" when the same payload
#: arrives twice. The dedup key in the database is what actually prevents duplicates.
_MAX_PARTS_IN_REF = 64


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """What happened to one source in one run — the ledger row, in memory."""

    source_id: str
    status: str
    items_fetched: int = 0
    items_new: int = 0
    signals_parsed: int = 0
    quota_spent: int = 0
    rate_limit_hits: int = 0
    not_modified: bool = False
    reason: str | None = None

    @property
    def deduplicated(self) -> bool:
        return self.not_modified or (self.items_fetched > 0 and self.signals_parsed == 0)


@dataclass(frozen=True, slots=True)
class RunReport:
    """The run's outcome, in the shape the CLI prints and the tests assert on."""

    run_id: str
    resumed: bool
    status: str
    outcomes: tuple[SourceOutcome, ...]

    @property
    def items_new(self) -> int:
        return sum(outcome.items_new for outcome in self.outcomes)

    @property
    def signals_parsed(self) -> int:
        return sum(outcome.signals_parsed for outcome in self.outcomes)

    @property
    def quota_spent(self) -> int:
        return sum(outcome.quota_spent for outcome in self.outcomes)

    def by_id(self, source_id: str) -> SourceOutcome:
        for outcome in self.outcomes:
            if outcome.source_id == source_id:
                return outcome
        raise KeyError(source_id)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return {
            "run_id": self.run_id,
            "resumed": self.resumed,
            "status": self.status,
            "sources": counts,
            "items_new": self.items_new,
            "signals_parsed": self.signals_parsed,
            "quota_spent": self.quota_spent,
        }

    def render(self) -> str:
        lines = [
            f"L0 run {self.run_id} — {self.status}"
            + (" (resumed)" if self.resumed else ""),
            f"  sources: {len(self.outcomes)} · new signals: {self.items_new} · "
            f"parsed: {self.signals_parsed} · requests: {self.quota_spent}",
        ]
        for outcome in self.outcomes:
            detail = f" ({outcome.reason})" if outcome.reason else ""
            flag = "not-modified" if outcome.not_modified else f"{outcome.items_new} new"
            lines.append(
                f"  {outcome.source_id:<22} {outcome.status:<9} "
                f"items={outcome.items_fetched:<3} {flag:<13} "
                f"requests={outcome.quota_spent}{detail}"
            )
        return "\n".join(lines)


def _recent_hashes(session: Session, source_id: str, window: int = 50) -> set[str]:
    """The content hashes this source has stored recently — the dedup set (spec §5.2).

    A *set*, not just the newest hash. Comparing against the newest row only works while a source
    emits one payload per run: with several, the last one stored masks the others and every
    subsequent run re-parses payloads it has already seen (the gate's L0 probe caught exactly
    that, reporting a second run that parsed 12 payloads it should have skipped).
    """
    rows = session.execute(
        select(RawItem.content_hash)
        .where(RawItem.source_id == source_id)
        .order_by(RawItem.id.desc())
        .limit(window)
    ).scalars().all()
    return {str(row) for row in rows}


def _store_raw(session: Session, *, run_id: str, batch: RawBatch) -> bool:
    """Keep a *reference* to the payload, never the payload (brief §4: state stores references
    and hashes, never blobs). Returns True when a new row was written.

    `ON CONFLICT DO NOTHING` on (source_id, content_hash), and the conflict is a normal outcome:
    one run can fetch two payloads that hash the same — an unchanged front page between two
    polled moments, or a source repeating a block — and the lake's unique key exists precisely so
    the second write is a no-op. The first version used a plain insert, so a repeated payload
    inside a single run raised a UniqueViolation and took the source down with it.
    """
    statement = (
        pg_insert(RawItem)
        .values(
            source_id=batch.source_id,
            run_id=run_id,
            fetched_at=batch.fetched_at,
            content_hash=batch.content_hash,
            cursor=batch.cursor,
            byte_size=batch.byte_size,
            payload={
                "parts": min(batch.item_count, _MAX_PARTS_IN_REF),
                "statuses": list(batch.status_codes[: _MAX_PARTS_IN_REF]),
                "requests": batch.request_count,
            },
        )
        .on_conflict_do_nothing(constraint="uq_raw_items_source_id")
        .returning(RawItem.id)
    )
    inserted = session.execute(statement).scalar_one_or_none()
    return inserted is not None


def _store_signals(session: Session, signals: Sequence[Any]) -> int:
    """Insert signals, ignoring ones already known. Returns how many were new.

    The unique key (source, entity, metric, ts) makes this idempotent, so a resumed or
    repeated run cannot inflate the lake with duplicates of the same fact.
    """
    if not signals:
        return 0
    rows = [
        {
            "source_id": signal.source_id,
            "entity": signal.entity,
            "metric": signal.metric,
            "value": signal.value,
            "ts": signal.ts,
            "category": signal.category,
            "url": signal.url,
            "quote": signal.quote,
            "raw_hash": None,
            "metadata": dict(signal.metadata),
        }
        for signal in signals
    ]
    # RETURNING, not rowcount: for INSERT..ON CONFLICT DO NOTHING psycopg reports -1 as
    # the row count, which would report "-1 new signals" — a silent lie about the work done.
    signals_table = SignalRow.metadata.tables["signals"]
    statement = (
        pg_insert(signals_table)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["source_id", "entity", "metric", "ts"])
        .returning(signals_table.c.id)
    )
    return len(session.execute(statement).scalars().all())


def collect_one(  # noqa: PLR0911 — one return per outcome is clearer than a status variable
    session: Session,
    *,
    entry: SourceEntry,
    run_id: str,
    plugin: SourcePlugin,
    client: Any,
    budget: SourceBudget,
    clock: Any,
    previously_spent: int,
    max_items: int | None = None,
    dry_run: bool = False,
) -> SourceOutcome:
    """Fetch and store one source. Never raises for a source-level problem."""
    cursor = read_watermark(session, entry.id)
    ctx = FetchContext(
        run_id=run_id,
        source_id=entry.id,
        client=client,
        budget=budget,
        clock=clock,
        cursor=cursor,
        max_items=max_items,
    )

    try:
        batch = plugin.fetch(ctx)
    except SourceSkippedError as exc:
        return SourceOutcome(
            source_id=entry.id,
            status="skipped",
            quota_spent=budget.spent_today - previously_spent,
            rate_limit_hits=budget.rate_limit_hits,
            reason=str(exc),
        )
    except HttpFetchError as exc:
        return SourceOutcome(
            source_id=entry.id,
            status="failed",
            quota_spent=budget.spent_today - previously_spent,
            rate_limit_hits=budget.rate_limit_hits,
            reason=str(exc),
        )
    except EgressDeniedError as exc:  # a plugin bug, and a loud one
        return SourceOutcome(
            source_id=entry.id,
            status="failed",
            reason=f"egress denied: {exc}",
        )

    spent = budget.spent_today - previously_spent
    hits = budget.rate_limit_hits

    if batch.not_modified:
        if not dry_run:
            advance_watermark(session, entry.id, batch.cursor)
        return SourceOutcome(
            source_id=entry.id,
            status="ok",
            quota_spent=spent,
            rate_limit_hits=hits,
            not_modified=True,
            reason="the source reports nothing new since the watermark",
        )

    if not batch.parts:
        return SourceOutcome(
            source_id=entry.id,
            status="degraded",
            quota_spent=spent,
            rate_limit_hits=hits,
            reason="the source returned no payloads",
        )

    seen_hashes = _recent_hashes(session, entry.id)
    if batch.is_unchanged_since(seen_hashes):
        if not dry_run:
            advance_watermark(session, entry.id, batch.cursor)
        return SourceOutcome(
            source_id=entry.id,
            status="ok",
            quota_spent=spent,
            rate_limit_hits=hits,
            items_fetched=batch.item_count,
            reason="identical payload hash: parsing, scoring and the LLM are skipped (spec §5.2)",
        )

    signals = plugin.parse(batch)
    if dry_run:
        # A dry run does the work and reports it, but writes nothing at all — no lake row,
        # no signals, no watermark. `items_new` is what a real run would have stored.
        return SourceOutcome(
            source_id=entry.id,
            status="ok",
            quota_spent=spent,
            rate_limit_hits=hits,
            items_fetched=batch.item_count,
            items_new=len(signals),
            signals_parsed=len(signals),
            reason="dry run: nothing written",
        )

    # The lake write is idempotent by key, so a repeated payload in one run is a no-op rather
    # than a UniqueViolation that would take the source down with it.
    _store_raw(session, run_id=run_id, batch=batch)
    new_signals = _store_signals(session, signals)
    advance_watermark(session, entry.id, batch.cursor)

    degraded = any(code >= HTTP_CLIENT_ERROR for code in batch.status_codes) or hits > 0
    return SourceOutcome(
        source_id=entry.id,
        status="degraded" if degraded else "ok",
        quota_spent=spent,
        rate_limit_hits=hits,
        items_fetched=batch.item_count,
        items_new=new_signals,
        signals_parsed=len(signals),
        reason="some requests were refused" if degraded else None,
    )


def _record_outcome(
    session: Session, *, run_id: str, outcome: SourceOutcome, operation: str
) -> None:
    """Persist one outcome: quota first (CAS), then the ledger row."""
    if outcome.quota_spent > 0:
        recorded = record_spend(
            session,
            source_id=outcome.source_id,
            run_id=run_id,
            operation=operation,
            amount=outcome.quota_spent,
            reason=outcome.status,
        )
        if not recorded:
            log_event(
                log,
                "quota.already_recorded",
                source_id=outcome.source_id,
                run_id=run_id,
                note="a resumed run re-issued a spend that is already in the ledger",
            )
    record_source_result(
        session,
        run_id=run_id,
        source_id=outcome.source_id,
        status=outcome.status,
        items_fetched=outcome.items_fetched,
        items_new=outcome.items_new,
        quota_spent=outcome.quota_spent,
        rate_limit_hits=outcome.rate_limit_hits,
        reason=outcome.reason,
    )


def run_l0(
    *,
    registry: Registry,
    sessions: sessionmaker[Session],
    client_for: Callable[[SourceEntry], Any],
    engine: Engine | None = None,
    runner: Callable[[SourceEntry], SourcePlugin] | None = None,
    clock_for: Callable[[SourceEntry], Any] | None = None,
    max_items_for: Callable[[SourceEntry], int | None] | None = None,
    trigger: str = "nightly",
    resume: bool = True,
    source_ids: Sequence[str] | None = None,
    max_sources: int | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Run L0 for every enabled L0 source (or the requested subset), in registry order.

    Args:
        sessions: session factory.
        client_for: builds the HTTP client for a source. Tests pass a fixture-replaying
            factory; production passes the allowlist-enforcing httpx client.
        runner: plugin loader; defaults to `load_plugin`.
        max_sources: stop after N sources without closing the run — how a crash is
            simulated, and how a partially-completed run is left behind on purpose.
        dry_run: do the work but write nothing (no lake rows, no ledger, no watermark).
    """
    make_clock = clock_for or (lambda _entry: SystemClock())
    load = runner or load_plugin

    with sessions() as session:
        # A watermark lives on the source's row, so the registry mirror must exist before
        # anything can be resumed. The sync never touches watermarks, so this is safe to
        # run every time.
        sync_sources(session, registry)
        handle: RunHandle = start_run(session, trigger=trigger, resume=resume)
        session.commit()
        pending = pending_sources(session, handle.run_id, registry)
        if source_ids is not None:
            wanted = set(source_ids)
            pending = tuple(source_id for source_id in pending if source_id in wanted)
        if max_sources is not None:
            pending = pending[:max_sources]

        budgets = restore_budgets(
            session, registry, source_ids=pending, clock_for=make_clock
        )
        outcomes: list[SourceOutcome] = []

        for source_id in pending:
            entry = registry.by_id(source_id)
            budget = budgets[source_id]
            try:
                plugin = load(entry)
            except PluginContractError as exc:
                # A collector that has not been written yet is not a crash: it is a source
                # this phase does not cover, recorded as skipped with the reason why.
                outcome = SourceOutcome(
                    source_id=source_id,
                    status="skipped",
                    reason=f"no plugin yet: {exc}",
                )
                outcomes.append(outcome)
                if not dry_run:
                    _record_outcome(
                        session, run_id=handle.run_id, outcome=outcome, operation=f"l0:{source_id}"
                    )
                    session.commit()
                continue
            spent_before = spent_today(session, source_id)

            outcome = collect_one(
                session,
                entry=entry,
                run_id=handle.run_id,
                plugin=plugin,
                client=client_for(entry),
                budget=budget,
                clock=make_clock(entry),
                previously_spent=spent_before,
                max_items=max_items_for(entry) if max_items_for else None,
                dry_run=dry_run,
            )
            outcomes.append(outcome)
            log_event(
                log,
                "source.collected",
                run_id=handle.run_id,
                source_id=source_id,
                status=outcome.status,
                items_new=outcome.items_new,
            )

            if not dry_run:
                _record_outcome(
                    session, run_id=handle.run_id, outcome=outcome, operation=f"l0:{source_id}"
                )
                session.commit()

        if max_sources is None and not dry_run:
            status = _run_status(outcomes, registry, handle)
            finish_run(
                session,
                handle.run_id,
                status=status,
                layer_status={
                    "L0": {
                        "status": status,
                        "items": sum(outcome.items_fetched for outcome in outcomes),
                        "sources": {outcome.source_id: outcome.status for outcome in outcomes},
                    }
                },
            )
            session.commit()
        elif dry_run:
            status = _run_status(outcomes, registry, handle)
        else:
            status = "partial"

        return RunReport(
            run_id=handle.run_id,
            resumed=handle.resumed,
            status=status,
            outcomes=tuple(outcomes),
        )


def _run_status(outcomes: Sequence[SourceOutcome], registry: Registry, handle: RunHandle) -> str:
    """One word for the run: how badly did it go?

    A failed source degrades a run that otherwise worked, and fails a run that did not —
    "everything is broken" and "one endpoint is down" must not read the same to the monitor
    agent (spec §9).
    """
    failed = sum(1 for outcome in outcomes if outcome.status == "failed")
    succeeded = sum(1 for outcome in outcomes if outcome.status == "ok")
    other = len(outcomes) - failed - succeeded

    if not outcomes:
        # A resumed run whose work was already done is a success, not a failure.
        return "ok" if handle.resumed else "degraded"
    if failed == 0 and other == 0:
        return "ok"
    if failed == len(outcomes):
        return "failed"
    if failed or other:
        return "degraded"
    return "ok"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _client_factory(
    *, registry: Registry, fixtures_dir: Path | None, timeout_s: float
) -> Callable[[SourceEntry], Any]:
    if fixtures_dir is not None:
        def replay(entry: SourceEntry) -> Any:
            return RecordedHttpClient.from_path(
                fixture_path(fixtures_dir, entry.id), allowed_domains=entry.domains
            )

        return replay

    def live(entry: SourceEntry) -> Any:
        return HttpxClient(
            source_id=entry.id,
            allowed_domains=entry.domains,
            policy=HttpPolicy(timeout_s=timeout_s),
        )

    return live


def _clock_factory(
    *, fixtures_dir: Path | None
) -> Callable[[SourceEntry], Any]:
    """A real clock, or — when replaying fixtures — the time they were recorded at."""
    if fixtures_dir is None:
        return lambda _entry: SystemClock()

    def recorded(entry: SourceEntry) -> Any:
        payload = json.loads(
            fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8")
        )
        stamp = datetime.fromisoformat(str(payload["recorded_at"]))
        return FixedClock(stamp.astimezone(UTC))

    return recorded


def _limits_factory(*, fixtures_dir: Path | None) -> Callable[[SourceEntry], int | None] | None:
    """In replay mode, honour the item limit the fixture was recorded with.

    A replay that asked for more items than were recorded would request URLs nobody
    fetched — the plugin derives its requests from this limit, so the replay must use the
    recorded one.
    """
    if fixtures_dir is None:
        return None

    def recorded_limit(entry: SourceEntry) -> int | None:
        payload = json.loads(
            fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8")
        )
        value = payload.get("max_items")
        return int(value) if value is not None else None

    return recorded_limit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trend_analyst.pipeline.orchestrator",
        description="Run pipeline layers: L0 collect (spec §2), L1+L3 mine and score.",
    )
    parser.add_argument(
        "--layers",
        default=None,
        metavar="L0,L1,L3",
        help="layers to run: L0 (collect), L1+L3 (mine, score, snapshot). L2 is P4.",
    )
    parser.add_argument("--source", action="append", default=[], help="limit to these source ids")
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after N sources (crash drill)"
    )
    parser.add_argument("--no-resume", action="store_true", help="always start a new run")
    parser.add_argument("--dry-run", action="store_true", help="do not write anything")
    parser.add_argument(
        "--fixtures",
        default=None,
        metavar="DIR",
        help="replay recorded fixtures from DIR instead of the network",
    )
    parser.add_argument("--config-dir", default=None, help="directory holding sources.yaml")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    # Decide-layer knobs (L1+L3).
    parser.add_argument("--top", type=int, default=10, help="rows in the ranked table")
    parser.add_argument(
        "--min-keep",
        type=int,
        default=DEFAULT_MIN_KEEP,
        help="mining floor: never prune below this many candidates (brief: 95%% prune)",
    )
    parser.add_argument(
        "--prune-fraction",
        type=float,
        default=DEFAULT_PRUNE_FRACTION,
        help="share of mined phrases to keep, by velocity (default 0.05)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="ISO8601",
        help="pin the scoring instant (replay determinism); defaults to now, or to the "
        "newest signal when replaying fixtures",
    )
    parser.add_argument(
        "--rank",
        action="store_true",
        help="print the ranked table from the snapshot history and exit",
    )
    # Judge gate (P3). Live by default when a provider key exists; --judge-replay runs the whole
    # gate over a recorded provider answer, which needs no network and no quota.
    parser.add_argument(
        "--judge",
        action="store_true",
        help="run the Judge gate over the newest decide run's candidates (needs a provider key)",
    )
    parser.add_argument(
        "--judge-replay",
        default=None,
        metavar="FIXTURE",
        help="run the Judge gate offline against a recorded provider answer",
    )
    parser.add_argument(
        "--judge-batch-size", type=int, default=10, help="candidates per Judge call"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore the LLM cache for this run (a replay must exercise the whole path)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Default depends on intent: a plain invocation collects, while --judge/--judge-replay should
    # not drag a full L0 run along with it (the first replay did, drowning the output).
    if args.layers is None:
        args.layers = "L1,L3" if (args.judge or args.judge_replay) else "L0"
    requested = {layer.strip().upper() for layer in args.layers.split(",") if layer.strip()}
    unknown = requested - {"L0", "L1", "L3"}
    if unknown:
        detail = (
            "L2 is P4 (it needs Tier-A sources and budgets)"
            if "L2" in unknown
            else f"unknown layer(s) {sorted(unknown)}"
        )
        print(f"cannot run {args.layers!r}: {detail}", file=sys.stderr)
        return 1

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        settings = load_settings(config_dir)
        registry = load_registry(default_registry_path(config_dir))
        taxonomy = default_taxonomy(config_dir)
    except (ConfigError, TaxonomyError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(
        level=settings.app.log_level,
        json_output=settings.app.log_json,
    )

    try:
        engine = create_db_engine(settings) if settings.db.configured else None
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1
    if engine is None:
        print("database not configured: run scripts/provision_pg.sh", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    if args.rank:
        with sessions() as session:
            ranked = read_ranked(session, limit=args.top)
        if args.json:
            print(json.dumps({"rank": [row.as_dict() for row in ranked]}, indent=2, default=str))
        else:
            print(_render_ranked(ranked))
        engine.dispose()
        return 0

    try:
        exit_code, combined = _dispatch(
            args,
            settings=settings,
            registry=registry,
            taxonomy=taxonomy,
            sessions=sessions,
            engine=engine,
        )
        if args.json and combined:
            print(json.dumps(combined, indent=2, default=str))
    finally:
        engine.dispose()
    return exit_code


def _dispatch(
    args: Any,
    *,
    settings: Any,
    registry: Any,
    taxonomy: Any,
    sessions: sessionmaker[Session],
    engine: Engine,
) -> tuple[int, dict[str, Any]]:
    """Run the phases the invocation asked for, in order, collecting one JSON payload.

    Judging comes after deciding, because the gate judges what the decide layer just produced.
    """
    requested = {layer.strip().upper() for layer in str(args.layers).split(",") if layer.strip()}
    exit_code = 0
    # One command, one JSON document. Printing per phase produced two concatenated documents on
    # stdout, which no consumer can parse — the gate's own probe tripped over it.
    combined: dict[str, Any] = {}

    if "L0" in requested:
        exit_code, l0_payload = _run_l0_cli(args, registry, sessions, engine)
        if l0_payload is not None:
            combined["l0"] = l0_payload

    if requested & {"L1", "L3"}:
        decide = run_decide(
            sessions=sessions,
            taxonomy=taxonomy,
            as_of=_resolve_as_of(args, sessions),
            trigger="manual",
            min_keep=args.min_keep,
            prune_fraction=args.prune_fraction,
            source_ids=args.source or None,
            dry_run=args.dry_run,
            top=args.top,
        )
        if args.json:
            combined["decide"] = decide.summary()
        else:
            print(decide.render(top=args.top))

    # Judging comes after deciding: the gate judges what the decide layer just produced.
    if args.judge or args.judge_replay:
        judge_code, judge_payload = _run_judge_cli(args, settings, sessions, exit_code)
        exit_code = max(exit_code, judge_code)
        if judge_payload is not None:
            combined["judge"] = judge_payload

    return exit_code, combined


def _run_judge_cli(
    args: Any,
    settings: Any,
    sessions: sessionmaker[Session],
    exit_code: int,
) -> tuple[int, dict[str, Any] | None]:
    """Run the Judge gate from the CLI: live when keys exist, replayed when asked.

    Returns (exit code, JSON payload). The payload is returned rather than printed so that a
    command combining phases still emits exactly one JSON document.
    """
    replay_path = Path(args.judge_replay) if args.judge_replay else None
    with sessions() as session:
        if replay_path is not None:
            try:
                fixture = load_fixture(replay_path)
            except ReplayMissError as exc:
                print(f"replay unavailable: {exc}", file=sys.stderr)
                return 1, None
            recorded = JudgeVerdict.model_validate(fixture["output"])
            named = session.execute(
                select(Candidate).where(Candidate.phrase == recorded.phrase)
            ).scalars().first()
            # A recording answers one phrase, so judging a night's worth of candidates with it
            # would report misses for no reason. The replay judges exactly what it can answer.
            queue = [] if named is None else [named]
            if named is None:
                print(
                    f"recording names {recorded.phrase!r}, which is not a candidate in this "
                    "database: nothing to replay",
                    file=sys.stderr,
                )
                return 1, None
            sender = replay_sender(
                fixture, covered=[str(candidate.phrase) for candidate in queue]
            )
            note = f"replayed {fixture['recorded_at']}"
        else:
            queue = list(pending_judgements(session, limit=args.judge_batch_size * 3))
            try:
                sender = settings_sender(settings)
            except Exception as exc:  # a missing transport is a configuration problem
                print(f"judge unavailable: {exc}", file=sys.stderr)
                return 1, None
            note = "live provider call"

        if not queue:
            print("no unjudged candidates: run --layers L0,L1,L3 first")
            return exit_code, None

        handle = start_run(session, trigger="manual", resume=False)
        session.commit()
        report = judge_candidates(
            session,
            run_id=handle.run_id,
            sender=sender,
            candidates=queue,
            batch_size=args.judge_batch_size,
            dry_run=args.dry_run,
            bypass_cache=getattr(args, "fresh", False),
        )
        if not args.dry_run:
            finish_run(
                session,
                str(handle.run_id),
                status="ok" if report.ok else "degraded",
                layer_status={"L3-judge": report.as_dict()},
                notes=f"{report.summary()} ({note})",
            )
            session.commit()

    if not args.json:
        print(f"judge run {handle.run_id} ({note})")
        print(report.summary())
        for entry in report.per_batch:
            print(
                f"  batch {entry['batch']}: {entry['status']} applied={entry['applied']} "
                f"missing={entry['missing']} provider={entry['provider']}"
            )
    payload = {**report.as_dict(), "source": note}
    return (exit_code if report.ok else 1), payload


def _resolve_as_of(args: Any, sessions: sessionmaker[Session]) -> datetime:
    """Pick the scoring instant: --as-of, else the newest signal, else now.

    The newest-signal default is what makes an offline replay deterministic: fixtures are
    recorded once, so scoring against their newest timestamp reproduces the same windows on
    every replay. In production the two are the same instant to within a collection cycle,
    and using the data's own clock still beats the wall clock (spec §5.4).
    """
    if args.as_of:
        return datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    if args.fixtures:
        with sessions() as session:
            newest = session.execute(select(func.max(SignalRow.ts))).scalar_one_or_none()
        if newest is not None:
            return newest if newest.tzinfo else newest.replace(tzinfo=UTC)
    return datetime.now(UTC)




def _render_ranked(ranked: Sequence[Any]) -> str:
    """Print the ranked table from stored snapshots (`--rank`).

    Reads the snapshot history rather than recomputing, which is the point of the table
    being append-only: what this prints is what the database decided, including rows scored
    by an older weights version.
    """
    if not ranked:
        return "no score snapshots yet - run --layers L0,L1,L3 first"
    header = (
        f"{'MGS':>5}  {'DV':>4} {'SS':>4} {'SP':>4} {'MP':>4} {'FE':>4}  "
        f"{'fad':>9}  {'prob':>5}  {'$/mo P10-P50-P90':>24}  phrase"
    )
    rows = [header, "-" * len(header)]
    for score in ranked:
        money = f"{score.revenue_p10:,.0f}-{score.revenue_p50:,.0f}-{score.revenue_p90:,.0f}"
        rows.append(
            f"{score.mgs:5.1f}  {score.demand_velocity:4.0f} {score.saturation:4.0f} "
            f"{score.buyer_pain:4.0f} {score.money:4.0f} {score.feasibility:4.0f}  "
            f"{score.fad_label:>9}  {score.fad_probability:5.2f}  {money:>24}  "
            f"{score.phrase} [{score.category}] w={score.weights_version}"
        )
    rows.append("-" * len(header))
    return "\n".join(rows)


def _run_l0_cli(
    args: Any,
    registry: Any,
    sessions: sessionmaker[Session],
    engine: Engine,
) -> tuple[int, dict[str, Any] | None]:
    """Run L0 from the CLI's arguments. Returns (exit code, JSON payload or None)."""
    fixtures_dir = Path(args.fixtures) if args.fixtures else None
    report = run_l0(
        registry=registry,
        sessions=sessions,
        engine=engine,
        client_for=_client_factory(registry=registry, fixtures_dir=fixtures_dir, timeout_s=30.0),
        clock_for=_clock_factory(fixtures_dir=fixtures_dir),
        max_items_for=_limits_factory(fixtures_dir=fixtures_dir),
        trigger="manual",
        resume=not args.no_resume,
        source_ids=args.source or None,
        max_sources=args.limit,
        dry_run=args.dry_run,
    )
    payload = {
        **report.summary(),
        "outcomes": [asdict(outcome) for outcome in report.outcomes],
    }
    if not args.json:
        print(report.render())
    return (0 if report.status in {"ok", "partial"} else 1), payload


if __name__ == "__main__":
    raise SystemExit(main())
