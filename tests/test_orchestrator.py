"""Orchestrator tests — the P1 acceptance bar, against real Postgres.

The acceptance from the build brief that lives here:

* **two consecutive runs, the second doing no new work** — proven by counts, not by a
  stopwatch: the second run stores zero new signals and, for a source whose cursor
  says nothing changed, does not even parse.

Single-shot: every call opens a fresh run and runs every requested source. A crash
means calling again — dedup and idempotent inserts keep the re-run cheap. There is
no resume, no quota ledger.

Everything runs against the isolated test database inside a transaction that is rolled
back, and every request is served from the recorded fixtures: no network, no leftover state.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.net import HttpFetchError, RecordedHttpClient, fixture_path
from trend_analyst.pipeline.orchestrator import RunReport, run_l0
from trend_analyst.pipeline.runs import close_run, open_run, read_cursor, write_cursor
from trend_analyst.sources.base import FixedClock, HttpResponse
from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.models import RawItem, Run, RunSourceLog, SignalRow, Source

pytestmark = pytest.mark.db

#: The three collectors P1 implements. The other twelve L0 sources are still stubs, and a
#: run limited to these three is the honest scope of this phase.
IMPLEMENTED = ("hn_firebase", "wiki_pageviews", "arctic_shift")


@pytest.fixture
def sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A session factory inside one transaction, rolled back after the test.

    `run_l0` commits per source — it has to, so that a crash leaves real progress behind —
    so the outermost transaction is what keeps each test isolated.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def registry(repo_root: Path) -> object:
    return load_registry(default_registry_path(repo_root / "config"))


@pytest.fixture
def fixtures(repo_root: Path) -> Path:
    return repo_root / "tests" / "data"


def client_for(fixtures_dir: Path, *, touched: list[str] | None = None) -> object:
    """A client factory that replays fixtures and records which sources were asked for."""

    def build(entry: object) -> RecordedHttpClient:
        source_id = entry.id
        if touched is not None:
            touched.append(source_id)
        return RecordedHttpClient.from_path(
            fixture_path(fixtures_dir, source_id),
            allowed_domains=entry.domains,
        )

    return build


def clock_for(fixtures_dir: Path) -> object:
    """Pin each source's clock to its fixture's recording time (see FixedClock)."""

    def build(entry: object) -> FixedClock:
        payload = json.loads(
            fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8")
        )
        return FixedClock(datetime.fromisoformat(str(payload["recorded_at"])).astimezone(UTC))

    return build


def limits_for(fixtures_dir: Path) -> object:
    def build(entry: object) -> int | None:
        payload = json.loads(
            fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8")
        )
        value = payload.get("max_items")
        return int(value) if value is not None else None

    return build


def run_once(
    *,
    registry: object,
    sessions: sessionmaker[Session],
    fixtures_dir: Path,
    touched: list[str] | None = None,
) -> RunReport:
    return run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=client_for(fixtures_dir, touched=touched),  # type: ignore[arg-type]
        clock_for=clock_for(fixtures_dir),  # type: ignore[arg-type]
        max_items_for=limits_for(fixtures_dir),  # type: ignore[arg-type]
        source_ids=IMPLEMENTED,
        trigger="nightly",
    )


# ---------------------------------------------------------------------------
# Acceptance 1: two consecutive runs, the second does no new work
# ---------------------------------------------------------------------------
def test_first_run_collects_and_stores(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    report = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    assert report.status == "ok"
    assert report.resumed is False
    assert report.by_id("hn_firebase").items_new == 12
    assert report.by_id("wiki_pageviews").items_new == 2990
    assert report.by_id("arctic_shift").items_new == 71
    assert report.quota_spent == 13 + 3 + 15

    with sessions() as session:
        assert session.execute(select(func.count()).select_from(SignalRow)).scalar_one() == 3073
        assert session.execute(select(func.count()).select_from(RawItem)).scalar_one() == 3
        assert read_cursor(session, "hn_firebase") is not None
        assert read_cursor(session, "wiki_pageviews") is not None
        assert read_cursor(session, "arctic_shift") is not None


def test_second_run_does_no_new_work(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """The acceptance: run it again and the ledger shows nothing new was parsed or stored."""
    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    second = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    assert second.status == "ok"
    assert second.items_new == 0, "no new signals on a repeat run"
    assert second.by_id("hn_firebase").not_modified is True
    assert second.by_id("wiki_pageviews").not_modified is True
    assert second.by_id("hn_firebase").signals_parsed == 0, "not even parsed"
    assert second.by_id("wiki_pageviews").signals_parsed == 0, "not even parsed"
    assert second.by_id("hn_firebase").quota_spent == 1, "one request: the front page"
    assert second.by_id("wiki_pageviews").quota_spent == 0, "no request at all"
    # Arctic Shift legitimately asks a NEW window on the second run, so it parses those
    # posts. What matters for the acceptance is that nothing NEW was stored: the fixtures
    # overlap enough that every parsed signal was already in the lake.
    assert second.by_id("arctic_shift").items_new == 0

    with sessions() as session:
        assert session.execute(select(func.count()).select_from(SignalRow)).scalar_one() == 3073, (
            "the lake did not grow"
        )


def test_the_two_layers_of_dedup(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """There are two defences against redoing work, and the fixtures exercise both.

    * Signal-level: `signals` is keyed by (source, entity, metric, ts), so re-parsing a
      payload that overlaps what is already stored inserts nothing (run 2: parsed 67,
      stored 0). The recorded Reddit windows overlap, which is exactly the real situation —
      a watermark built from a date is a coarse filter.
    * Payload-level: an identical batch hash skips parsing, scoring and the LLM entirely
      (run 3: parsed 0).
    """
    first = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    second = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    third = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    assert first.by_id("arctic_shift").items_new == 71
    assert second.by_id("arctic_shift").signals_parsed == 67
    assert second.by_id("arctic_shift").items_new == 0, "already-stored facts stay stored once"
    assert third.by_id("arctic_shift").items_fetched == 15
    assert third.by_id("arctic_shift").signals_parsed == 0, "the identical batch is not parsed"
    assert third.by_id("arctic_shift").items_new == 0
    assert "identical payload hash" in (third.by_id("arctic_shift").reason or "")


def test_run_row_is_closed_with_layer_status(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    report = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    with sessions() as session:
        run = session.get(Run, report.run_id)
        assert run is not None
        assert run.status == "ok"
        assert run.finished_at is not None
        assert set(run.layer_status["L0"]["sources"]) == set(IMPLEMENTED)
        assert run.layer_status["L0"]["items"] == 12 + 3 + 15


def test_ledger_records_every_source_with_its_counts(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    report = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    with sessions() as session:
        rows = {
            row.source_id: row
            for row in session.execute(
                select(RunSourceLog).where(RunSourceLog.run_id == report.run_id)
            ).scalars()
        }
    assert set(rows) == set(IMPLEMENTED)
    assert rows["hn_firebase"].status == "ok"
    assert rows["hn_firebase"].items_fetched == 12
    assert rows["hn_firebase"].items_new == 12
    assert rows["hn_firebase"].quota_spent == 13
    assert rows["hn_firebase"].finished_at is not None


# ---------------------------------------------------------------------------
# Failure handling: a failure is not sticky, and never stops the run
# ---------------------------------------------------------------------------
def test_a_failed_source_runs_again_next_time(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """Single-shot means no resume — but a failure must not stick either: the next
    run tries every requested source again."""

    def broken(entry: object) -> object:
        class Broken:
            source_id = "hn_firebase"

            def get(
                self, url: str, *, params: object = None, headers: object = None
            ) -> HttpResponse:
                raise HttpFetchError("boom", url=url, attempts=3, last_status=503)

        return Broken()

    failed = run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=broken,  # type: ignore[arg-type]
        clock_for=clock_for(fixtures),  # type: ignore[arg-type]
        max_items_for=limits_for(fixtures),  # type: ignore[arg-type]
        source_ids=("hn_firebase",),
    )
    assert failed.by_id("hn_firebase").status == "failed"

    retried = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    assert retried.by_id("hn_firebase").status == "ok"


def test_a_failed_source_does_not_stop_the_run(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    def clients(entry: object) -> object:
        if entry.id == "hn_firebase":
            class Broken:
                source_id = "hn_firebase"

                def get(
                    self, url: str, *, params: object = None, headers: object = None
                ) -> HttpResponse:
                    raise HttpFetchError("boom", url=url, attempts=3, last_status=503)

            return Broken()
        return client_for(fixtures)(entry)

    report = run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=clients,
        clock_for=clock_for(fixtures),  # type: ignore[arg-type]
        max_items_for=limits_for(fixtures),  # type: ignore[arg-type]
        source_ids=IMPLEMENTED,
    )

    assert report.by_id("hn_firebase").status == "failed"
    assert report.by_id("wiki_pageviews").status == "ok"
    assert report.status == "degraded", "one broken endpoint must not fail the night"
    with sessions() as session:
        row = session.execute(
            select(RunSourceLog).where(RunSourceLog.source_id == "hn_firebase")
        ).scalar_one()
        assert "boom" in (row.reason or "")


# ---------------------------------------------------------------------------
# Each run gets its own log rows — one row per source, no ledger
# ---------------------------------------------------------------------------
def test_each_run_writes_its_own_log_rows(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    first = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    second = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    assert second.run_id != first.run_id, "single-shot: a fresh run every time"
    assert second.resumed is False
    with sessions() as session:
        rows = session.execute(
            select(RunSourceLog).where(RunSourceLog.run_id == second.run_id)
        ).scalars().all()
        assert {row.source_id for row in rows} == set(IMPLEMENTED)
        assert sum(row.quota_spent for row in rows) == second.quota_spent


def test_open_run_mints_a_fresh_id(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        first = open_run(session, trigger="nightly")
        second = open_run(session, trigger="nightly")
        assert first != second


def test_close_run_accepts_every_terminal_status(sessions: sessionmaker[Session]) -> None:
    """`empty` (ran fine, found nothing) is a legitimate terminal state, not a lie.

    The first `--judge-replay` on a night with no candidates died on the status CHECK
    instead of reporting — the constraint knew every outcome except the quiet one.
    """
    with sessions() as session:
        for status in ("ok", "degraded", "failed", "aborted", "empty"):
            run_id = open_run(session, trigger="nightly")
            session.commit()
            close_run(session, run_id, status=status)
            session.commit()
            assert session.get(Run, run_id) is not None
            assert session.get(Run, run_id).status == status


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------
def test_cursor_advances_and_is_never_cleared(
    sessions: sessionmaker[Session], registry: object
) -> None:
    with sessions() as session:
        session.add(
            Source(
                id="hn_firebase", role="r", tier="S", layers=["L0"], schedule="nightly",
                budget_per_day=10, rps=1.0, cache_ttl_h=24, enabled=True, domains=["x.test"],
            )
        )
        session.flush()

        assert write_cursor(session, "hn_firebase", "cursor-1") is True
        assert read_cursor(session, "hn_firebase") == "cursor-1"
        assert write_cursor(session, "hn_firebase", "cursor-1") is False, "no rewrite"
        assert write_cursor(session, "hn_firebase", None) is False, (
            "a source that could not decide where it got to must not erase where it was"
        )
        assert read_cursor(session, "hn_firebase") == "cursor-1"
        assert write_cursor(session, "hn_firebase", "cursor-2") is True
        assert read_cursor(session, "hn_firebase") == "cursor-2"


def test_cursor_survives_a_run(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    with sessions() as session:
        first = read_cursor(session, "hn_firebase")

    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    with sessions() as session:
        assert read_cursor(session, "hn_firebase") == first, (
            "an unchanged source keeps its cursor byte-identically"
        )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    report = run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=client_for(fixtures, touched=[]),  # type: ignore[arg-type]
        clock_for=clock_for(fixtures),  # type: ignore[arg-type]
        max_items_for=limits_for(fixtures),  # type: ignore[arg-type]
        source_ids=IMPLEMENTED,
        dry_run=True,
    )

    # A dry run reports what a real run WOULD store, and writes none of it.
    assert report.items_new == 3073
    assert report.by_id("hn_firebase").reason == "dry run: nothing written"
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(SignalRow)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(RawItem)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(RunSourceLog)).scalar_one() == 0
