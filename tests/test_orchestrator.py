"""Run-ledger and orchestrator tests — the P1 acceptance bar, against real Postgres.

Two acceptances from the build brief live here:

* **two consecutive runs, the second doing no new work** — proven by the ledger, not by a
  stopwatch: the second run stores zero new signals and, for a source whose watermark
  says nothing changed, does not even parse.
* **kill-and-resume executes only unfinished work** — a partially completed run is left
  behind on purpose, and the next call must touch exactly the remaining sources.

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
from trend_analyst.pipeline.state import (
    advance_watermark,
    completed_sources,
    pending_sources,
    read_watermark,
    record_source_result,
    record_spend,
    restore_budgets,
    spent_today,
    start_run,
)
from trend_analyst.sources.base import FixedClock, HttpResponse
from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.models import QuotaLedger, RawItem, Run, RunSourceLog, SignalRow, Source

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
    resume: bool = True,
    max_sources: int | None = None,
    touched: list[str] | None = None,
) -> RunReport:
    return run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=client_for(fixtures_dir, touched=touched),  # type: ignore[arg-type]
        clock_for=clock_for(fixtures_dir),  # type: ignore[arg-type]
        max_items_for=limits_for(fixtures_dir),  # type: ignore[arg-type]
        source_ids=IMPLEMENTED,
        resume=resume,
        max_sources=max_sources,
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
        assert read_watermark(session, "hn_firebase") is not None
        assert read_watermark(session, "wiki_pageviews") is not None
        assert read_watermark(session, "arctic_shift") is not None


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
# Acceptance 2: kill and resume
# ---------------------------------------------------------------------------
def test_resume_executes_only_unfinished_work(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """The crash drill: stop after one source, then resume — and touch nothing twice."""
    first_touched: list[str] = []
    partial = run_once(
        registry=registry,
        sessions=sessions,
        fixtures_dir=fixtures,
        max_sources=1,
        touched=first_touched,
    )
    assert partial.status == "partial"
    assert [outcome.source_id for outcome in partial.outcomes] == ["hn_firebase"]
    assert first_touched == ["hn_firebase"]

    second_touched: list[str] = []
    resumed = run_once(
        registry=registry, sessions=sessions, fixtures_dir=fixtures, touched=second_touched
    )

    assert resumed.resumed is True, "the run left behind is adopted, not replaced"
    assert resumed.run_id == partial.run_id
    assert [outcome.source_id for outcome in resumed.outcomes] == [
        "arctic_shift",
        "wiki_pageviews",
    ], "only the unfinished sources run, in registry order"
    assert second_touched == ["arctic_shift", "wiki_pageviews"], (
        "the finished source is not even asked for"
    )
    assert resumed.status == "ok"


def test_resume_retries_a_failed_source(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """A failure is unfinished work: the resume must try it again, not skip it."""
    with sessions() as session:
        handle = start_run(session, trigger="nightly")
        record_source_result(
            session,
            run_id=handle.run_id,
            source_id="hn_firebase",
            status="failed",
            reason="HTTP 503",
        )
        session.commit()

        completed = completed_sources(session, handle.run_id)
        pending = pending_sources(session, handle.run_id, registry)  # type: ignore[arg-type]
        assert "hn_firebase" not in completed, "a failed source is not finished"
        assert set(IMPLEMENTED) <= set(pending), "so it is still pending"

    touched: list[str] = []
    resumed = run_once(
        registry=registry, sessions=sessions, fixtures_dir=fixtures, touched=touched
    )

    assert "hn_firebase" in touched, "the failed source is retried"
    assert resumed.by_id("hn_firebase").status == "ok"


def test_completed_sources_counts_skips_as_done(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """A skipped source was a decision, not an accident: retrying it would defeat the
    back-off it was skipped for."""
    with sessions() as session:
        handle = start_run(session)
        for source_id in IMPLEMENTED:
            record_source_result(
                session, run_id=handle.run_id, source_id=source_id, status="skipped"
            )
        session.commit()

        assert completed_sources(session, handle.run_id) == set(IMPLEMENTED)
        # The twelve collectors P1 does not implement yet are still pending; what matters is
        # that a skipped source is not retried.
        pending = pending_sources(session, handle.run_id, registry)  # type: ignore[arg-type]
        assert set(pending) & set(IMPLEMENTED) == set()


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
# Quota: the ledger, the CAS, and rehydration on resume
# ---------------------------------------------------------------------------
def test_spend_is_written_to_the_ledger_once_per_source(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    report = run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)

    with sessions() as session:
        rows = session.execute(
            select(QuotaLedger).where(QuotaLedger.run_id == report.run_id)
        ).scalars().all()
        assert {row.source_id for row in rows} == set(IMPLEMENTED)
        assert sum(row.amount for row in rows) == report.quota_spent == 31


def test_record_spend_is_compare_and_swap(
    sessions: sessionmaker[Session],
) -> None:
    """The same operation in the same run can only be charged once — spec §5.4."""
    with sessions() as session:
        handle = start_run(session)
        session.commit()

        assert record_spend(
            session,
            source_id="hn_firebase",
            run_id=handle.run_id,
            operation="l0:hn_firebase",
            amount=5,
        )
        assert not record_spend(
            session,
            source_id="hn_firebase",
            run_id=handle.run_id,
            operation="l0:hn_firebase",
            amount=5,
        ), "a resumed run re-issuing the same spend must lose the race"

        assert spent_today(session, "hn_firebase") == 5

        # A different run is a different spend: yesterday's page 1 is not today's.
        other = start_run(session, resume=False)
        session.commit()
        assert record_spend(
            session,
            source_id="hn_firebase",
            run_id=other.run_id,
            operation="l0:hn_firebase",
            amount=7,
        )
        assert spent_today(session, "hn_firebase") == 12


def test_resume_rehydrates_the_budget_from_the_ledger(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    """A crash must not be a way to spend a source's daily budget twice."""
    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures, max_sources=1)

    with sessions() as session:
        ledger_says = spent_today(session, "hn_firebase")
        assert ledger_says == 13, "13 requests are already in the ledger"

        budgets = restore_budgets(session, registry, source_ids=IMPLEMENTED)  # type: ignore[arg-type]
        assert budgets["hn_firebase"].spent_today == ledger_says, "rehydrated, not reset"
        assert budgets["hn_firebase"].remaining == 2000 - 13
        assert budgets["wiki_pageviews"].spent_today == 0, "a source that never ran starts fresh"


def test_source_result_is_upserted_not_duplicated(
    sessions: sessionmaker[Session], registry: object
) -> None:
    with sessions() as session:
        handle = start_run(session)
        record_source_result(
            session, run_id=handle.run_id, source_id="hn_firebase", status="failed"
        )
        record_source_result(
            session, run_id=handle.run_id, source_id="hn_firebase", status="ok", items_new=4
        )
        session.commit()

        rows = session.execute(
            select(RunSourceLog).where(RunSourceLog.run_id == handle.run_id)
        ).scalars().all()
        assert len(rows) == 1, "a retry updates the row instead of adding a second verdict"
        assert rows[0].status == "ok"
        assert rows[0].items_new == 4


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------
def test_watermark_advances_and_is_never_cleared(
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

        assert advance_watermark(session, "hn_firebase", "cursor-1") is True
        assert read_watermark(session, "hn_firebase") == "cursor-1"
        assert advance_watermark(session, "hn_firebase", "cursor-1") is False, "no rewrite"
        assert advance_watermark(session, "hn_firebase", None) is False, (
            "a source that could not decide where it got to must not erase where it was"
        )
        assert read_watermark(session, "hn_firebase") == "cursor-1"
        assert advance_watermark(session, "hn_firebase", "cursor-2") is True
        assert read_watermark(session, "hn_firebase") == "cursor-2"


def test_watermark_survives_a_run(
    registry: object, sessions: sessionmaker[Session], fixtures: Path
) -> None:
    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    with sessions() as session:
        first = read_watermark(session, "hn_firebase")

    run_once(registry=registry, sessions=sessions, fixtures_dir=fixtures)
    with sessions() as session:
        assert read_watermark(session, "hn_firebase") == first, (
            "an unchanged source keeps its watermark byte-identically"
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
        assert session.execute(select(func.count()).select_from(QuotaLedger)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(RunSourceLog)).scalar_one() == 0
