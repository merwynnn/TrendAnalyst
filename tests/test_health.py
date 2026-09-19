"""Health CLI tests.

Two halves:

* pure tests (no database) for the status rules and the rendering, and
* `db`-marked tests that drive the real query path against the isolated test database —
  including the two states that matter most: an empty-but-healthy system, and a system
  that must NOT report itself healthy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.monitor.health import (
    DatabaseHealth,
    ExitCode,
    HealthInputs,
    HealthReport,
    RegistryHealth,
    build_report,
    collect_health,
    main,
    render_text,
    source_status,
)
from trend_analyst.sources.registry import load_registry
from trend_analyst.store.models import Candidate, EvalCase, QuotaLedger, Run, RunSourceLog


# ---------------------------------------------------------------------------
# Pure: the status rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "expected_status", "expect_reason"),
    [
        ({"enabled": False, "last_status": None}, "disabled", False),
        ({"enabled": False, "last_status": "failed"}, "disabled", False),
        ({"enabled": True, "last_status": None}, "never_run", True),
        ({"enabled": True, "last_status": "ok"}, "ok", False),
        ({"enabled": True, "last_status": "degraded"}, "degraded", True),
        ({"enabled": True, "last_status": "skipped"}, "degraded", True),
        ({"enabled": True, "last_status": "failed"}, "down", False),
        ({"enabled": True, "last_status": "something-new"}, "degraded", True),
        ({"enabled": True, "last_status": "ok", "rate_limit_hits": 3}, "degraded", True),
        ({"enabled": True, "last_status": "ok", "burn_pct": 91.0}, "degraded", True),
        ({"enabled": True, "last_status": "ok", "burn_pct": 79.9}, "ok", False),
    ],
)
def test_source_status_rules(
    kwargs: dict[str, object], expected_status: str, expect_reason: bool
) -> None:
    status, reasons = source_status(**kwargs)  # type: ignore[arg-type]
    assert status == expected_status
    assert bool(reasons) is expect_reason


def test_rate_limit_reason_names_the_count() -> None:
    _, reasons = source_status(enabled=True, last_status="ok", rate_limit_hits=7)
    assert "rate limited 7x" in reasons[0]


def test_quota_burn_reason_names_the_threshold() -> None:
    _, reasons = source_status(enabled=True, last_status="ok", burn_pct=95.0, alert_pct=80.0)
    assert "95%" in reasons[0]
    assert "80%" in reasons[0]


# ---------------------------------------------------------------------------
# Pure: the report object and its rendering
# ---------------------------------------------------------------------------
def make_report(**overrides: object) -> HealthReport:
    payload: dict[str, object] = {
        "status": "healthy",
        "checked_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "database": DatabaseHealth(ok=True, server_version="18.6", pgvector_version="0.8.1"),
        "registry": RegistryHealth(
            ok=True, path="config/sources.yaml", total=22, enabled=15, disabled=7
        ),
        **overrides,
    }
    return HealthReport(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "exit_code"), [("healthy", 0), ("degraded", 1), ("down", 2)]
)
def test_exit_codes(status: str, exit_code: int) -> None:
    report = make_report(status=status)
    assert report.exit_code == exit_code
    assert int(report.exit_code) == exit_code


def test_render_text_reports_a_healthy_empty_system() -> None:
    text = render_text(make_report())
    assert "HEALTHY (exit 0)" in text
    assert "database: ok — postgres 18.6, pgvector 0.8.1" in text
    assert "last run: none yet" in text
    assert "gates: judge keep-rate n/a" in text
    assert "evals: 0 case(s)" in text


def test_render_text_explains_why_not_healthy() -> None:
    report = make_report(
        status="down",
        reasons=("database unreachable: OperationalError: connection refused",),
        database=DatabaseHealth(ok=False, detail="OperationalError: connection refused"),
    )
    text = render_text(report)
    assert "DOWN (exit 2)" in text
    assert "database: DOWN" in text
    assert "why not healthy:" in text
    assert "connection refused" in text


def test_report_serialises_to_json() -> None:
    payload = json.loads(make_report(status="degraded").model_dump_json())
    assert payload["status"] == "degraded"
    assert payload["database"]["ok"] is True
    assert payload["checked_at"].startswith("2026-01-01T12:00")


# ---------------------------------------------------------------------------
# Pure: failures become statuses, never tracebacks
# ---------------------------------------------------------------------------
def test_missing_database_is_down_with_a_reason(tmp_path: Path, repo_root: Path) -> None:
    report = collect_health(
        HealthInputs(registry_path=repo_root / "config" / "sources.yaml", engine=None)
    )
    assert report.status == "down"
    assert report.exit_code == ExitCode.DOWN
    assert any("database" in reason for reason in report.reasons)


def test_unreachable_database_is_down_with_a_reason(tmp_path: Path, repo_root: Path) -> None:
    # A short connect timeout: this test is about a status, not about waiting out TCP.
    engine = create_engine(
        "postgresql+psycopg://nobody:nope@127.0.0.1:59999/none",
        connect_args={"connect_timeout": 3},
    )
    report = collect_health(
        HealthInputs(registry_path=repo_root / "config" / "sources.yaml", engine=engine)
    )
    assert report.status == "down"
    assert report.database.ok is False
    assert any("unreachable" in reason for reason in report.reasons)


def test_invalid_registry_is_down_with_the_registry_message(
    tmp_path: Path, db_engine: Engine
) -> None:
    bad = tmp_path / "sources.yaml"
    bad.write_text("version: 1\nsources: {}\n", encoding="utf-8")

    report = collect_health(HealthInputs(registry_path=bad, engine=db_engine))

    assert report.status == "down"
    assert report.registry.ok is False
    assert any("registry invalid" in reason for reason in report.reasons)


def test_cli_reports_a_bad_registry_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "sources.yaml"
    bad.write_text("version: 1\nsources: {}\n", encoding="utf-8")

    exit_code = main(["--registry", str(bad), "--json"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "down"
    assert payload["registry"]["ok"] is False


# ---------------------------------------------------------------------------
# Against the database
# ---------------------------------------------------------------------------
pytestmark_db = pytest.mark.db


def report_from(
    session: Session, repo_root: Path, *, alert_pct: float = 80.0, now: datetime | None = None
) -> HealthReport:
    """Build the report from THIS session, so rows the test has not committed are visible.

    `collect_health` opens its own connection by design (the CLI needs one); a test that
    seeds data inside a rolled-back transaction must use the same session instead.
    """
    registry = load_registry(repo_root / "config" / "sources.yaml")
    summary = registry.summary()
    return build_report(
        session,
        registry=registry,
        registry_health=RegistryHealth(
            ok=True,
            path=str(repo_root / "config" / "sources.yaml"),
            total=summary["total"],
            enabled=summary["enabled"],
            disabled=summary["disabled"],
            tier_s=summary["tier_s"],
            tier_a=summary["tier_a"],
        ),
        database=DatabaseHealth(ok=True, server_version="18.6", pgvector_version="0.8.1"),
        alert_pct=alert_pct,
        checked_at=now or datetime.now(UTC),
    )


@pytest.fixture
def health_inputs(db_engine: Engine, repo_root: Path) -> HealthInputs:
    return HealthInputs(registry_path=repo_root / "config" / "sources.yaml", engine=db_engine)


@pytest.mark.db
def test_empty_database_is_healthy(health_inputs: HealthInputs) -> None:
    """The P0 acceptance bar: an empty-but-provisioned system is healthy, exit 0."""
    report = collect_health(health_inputs)

    assert report.status == "healthy"
    assert report.exit_code == ExitCode.HEALTHY
    assert report.reasons == ()
    assert report.database.ok is True
    assert report.registry.ok is True
    assert report.last_run is None

    counts = report.counts()
    assert counts["never_run"] == 15, "every enabled source is waiting for its first run"
    assert counts["disabled"] == 7
    assert counts["down"] == 0


@pytest.mark.db
def test_empty_database_renders_healthy_for_a_human(health_inputs: HealthInputs) -> None:
    text = render_text(collect_health(health_inputs))
    assert "HEALTHY (exit 0)" in text
    assert "0 ok · 0 degraded · 0 down · 15 never_run · 7 disabled" in text
    assert "quota: 0 / 22400 requests today" in text


def seed_run(session: Session, *, status: str = "ok", **overrides: object) -> Run:
    payload: dict[str, object] = {
        "status": status,
        "trigger": "manual",
        "finished_at": datetime.now(UTC),
        **overrides,
    }
    run = Run(**payload)  # type: ignore[arg-type]
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def seeded_session(db_engine: Engine) -> Session:
    """A session whose writes are rolled back after the test.

    Unlike the shared `db_session` fixture this one commits nothing either — it simply
    gives the test a session bound to a transaction it always rolls back.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.mark.db
def test_a_failed_source_makes_the_system_down(
    seeded_session: Session, repo_root: Path
) -> None:
    run = seed_run(seeded_session)
    seeded_session.add(
        RunSourceLog(run_id=run.id, source_id="hn_firebase", status="failed", reason="HTTP 500")
    )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.status == "down"
    assert report.exit_code == ExitCode.DOWN
    hn = next(source for source in report.sources if source.source_id == "hn_firebase")
    assert hn.status == "down"
    assert hn.last_status == "failed"
    assert any("1 source(s) down" in reason for reason in report.reasons)


@pytest.mark.db
def test_a_degraded_source_makes_the_system_degraded(
    seeded_session: Session, repo_root: Path
) -> None:
    run = seed_run(seeded_session, status="degraded")
    seeded_session.add(
        RunSourceLog(run_id=run.id, source_id="wiki_pageviews", status="degraded")
    )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.status == "degraded"
    assert report.exit_code == ExitCode.DEGRADED
    wiki = next(source for source in report.sources if source.source_id == "wiki_pageviews")
    assert wiki.status == "degraded"


@pytest.mark.db
def test_rate_limited_but_successful_source_is_degraded(
    seeded_session: Session, repo_root: Path
) -> None:
    run = seed_run(seeded_session)
    seeded_session.add(
        RunSourceLog(run_id=run.id, source_id="steam_reviews", status="ok", rate_limit_hits=4)
    )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.status == "degraded"
    steam = next(source for source in report.sources if source.source_id == "steam_reviews")
    assert "rate limited 4x" in steam.reasons[0]


@pytest.mark.db
def test_quota_burn_above_threshold_is_degraded_and_listed(
    seeded_session: Session, repo_root: Path
) -> None:
    """burn_pct is spend / budget, so the threshold can only be crossed with a small budget."""
    run = seed_run(seeded_session)
    seeded_session.add(RunSourceLog(run_id=run.id, source_id="common_crawl_index", status="ok"))
    # common_crawl_index has a budget of 100/day: 85 requests is 85 %.
    for index in range(85):
        seeded_session.add(
            QuotaLedger(
                source_id="common_crawl_index",
                run_id=run.id,
                operation=f"page:{index}",
                amount=1,
            )
        )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    source = next(item for item in report.sources if item.source_id == "common_crawl_index")
    assert source.quota_spent_today == 85
    assert source.burn_pct == pytest.approx(85.0)
    assert source.status == "degraded"
    assert "common_crawl_index" in report.quota.over_threshold
    assert report.status == "degraded"


@pytest.mark.db
def test_quota_spend_outside_today_is_not_counted(
    seeded_session: Session, repo_root: Path
) -> None:
    run = seed_run(seeded_session)
    seeded_session.add(
        QuotaLedger(
            source_id="hn_firebase",
            run_id=run.id,
            operation="yesterday:1",
            amount=5,
            spend_date=datetime.now(UTC) - timedelta(days=1),
        )
    )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    hn = next(source for source in report.sources if source.source_id == "hn_firebase")
    assert hn.quota_spent_today == 0, "yesterday's spend is not today's problem"


@pytest.mark.db
def test_last_run_details_are_reported(seeded_session: Session, repo_root: Path) -> None:
    older = seed_run(
        seeded_session, started_at=datetime.now(UTC) - timedelta(hours=25), status="ok"
    )
    newer = seed_run(
        seeded_session,
        status="degraded",
        layer_status={
            "L0": {"status": "ok", "items": 42},
            "L1": {"status": "degraded", "items": 3},
        },
    )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.last_run is not None
    assert report.last_run.run_id == str(newer.id)
    assert report.last_run.status == "degraded"
    assert {layer.layer for layer in report.last_run.layers} == {"L0", "L1"}
    assert next(layer for layer in report.last_run.layers if layer.layer == "L0").items == 42
    assert str(older.id) != report.last_run.run_id, "the newest run wins"


@pytest.mark.db
def test_judge_keep_rate_is_derived_from_candidates(
    seeded_session: Session, repo_root: Path
) -> None:
    for index in range(6):
        seeded_session.add(
            Candidate(phrase=f"phrase {index}", category="home-office", status="kept")
        )
    for index in range(4):
        seeded_session.add(
            Candidate(phrase=f"drop {index}", category="home-office", status="dropped")
        )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.gates.kept == 6
    assert report.gates.dropped == 4
    assert report.gates.judge_keep_rate == pytest.approx(0.6)


@pytest.mark.db
def test_eval_baseline_is_reported(seeded_session: Session, repo_root: Path) -> None:
    for index, baseline in enumerate((0.8, 0.9)):
        seeded_session.add(
            EvalCase(
                id=f"case-{index:03d}",
                category="home-office",
                input_signals=[],
                expected_keep=True,
                expected_fad_label="trend",
                score_min=60.0,
                score_max=80.0,
                baseline_score=baseline,
            )
        )
    seeded_session.flush()

    report = report_from(seeded_session, repo_root)

    assert report.evals.cases == 2
    assert report.evals.baseline_mean == pytest.approx(0.85)


@pytest.mark.db
def test_cli_json_output_on_an_empty_database(
    db_engine: Engine, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--config-dir",
            str(repo_root / "config"),
            "--registry",
            str(repo_root / "config" / "sources.yaml"),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "healthy"
    assert payload["registry"]["total"] == 22
    assert len(payload["sources"]) == 22
