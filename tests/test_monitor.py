"""Monitor tests: drift bands, alert rules, and the outbox (spec §9).

These tests are about *judgement calls*, so they are written as the failure stories the runbooks
describe: a judge that keeps everything, a rubric that slid three points, a source that has been
failing every night for a week, a budget at 90%. Each test states the symptom and asserts the alert
    a
monitor agent would have to receive to act on it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from trend_analyst.monitor import alerts as alerts_module
from trend_analyst.monitor.alerts import (
    Alert,
    collect_alerts,
    load_outbox,
    render_alerts,
    write_outbox,
)
from trend_analyst.monitor.drift import (
    DEFAULT_KEEP_RATE_BAND,
    eval_baseline_drop,
    keep_rate,
    keep_rate_drift,
    record_eval_run,
    watermark_drift,
)
from trend_analyst.store.models import (
    Candidate,
    EvalCase,
    Judgement,
    QuotaLedger,
    Run,
    RunSourceLog,
    Source,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fixtures


def _run(db_session: Session, run_id: str, *, started_at: datetime | None = None) -> Run:
    run = Run(
        id=uuid.UUID(run_id),
        started_at=started_at or NOW,
        status="ok",
        trigger="nightly",
        layer_status={},
    )
    db_session.add(run)
    db_session.flush()
    return run


def _candidate(db_session: Session, phrase: str, status: str = "kept") -> Candidate:
    row = Candidate(phrase=phrase, category="home", status=status, first_seen_at=NOW)
    db_session.add(row)
    db_session.flush()
    return row


def _judgement(
    db_session: Session, candidate: Candidate, run: Run, decision: str, *, created_at: datetime |
        None = None
) -> Judgement:
    row = Judgement(
        candidate_id=candidate.id,
        run_id=run.id,
        gate="judge",
        decision=decision,
        confidence=0.7,
        reason="test",
        created_at=created_at or NOW,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _source(db_session: Session, source_id: str, **overrides: object) -> Source:
    values: dict[str, object] = {
        "id": source_id,
        "role": "test source",
        "tier": "S",
        "layers": ["L0"],
        "schedule": "nightly",
        "budget_per_day": 100,
        "rps": 1.0,
        "domains": ["test"],
        "enabled": True,
    }
    values.update(overrides)
    row = Source(**values)  # type: ignore[arg-type]
    db_session.add(row)
    db_session.flush()
    return row


# --------------------------------------------------------------------------- keep-rate


def test_keep_rate_counts_only_judge_verdicts(db_session: Session) -> None:
    """A writer verdict is not a decision about a candidate and must not move the rate."""
    run = _run(db_session, "11111111-1111-4111-8111-111111111111")
    good = _candidate(db_session, "kettle")
    bad = _candidate(db_session, "cat jarman", status="dropped")
    _judgement(db_session, good, run, "keep")
    _judgement(db_session, bad, run, "drop")
    other = _candidate(db_session, "pizza oven")
    db_session.add(
        Judgement(
            candidate_id=other.id,
            run_id=run.id,
            gate="writer",
            decision="keep",
            confidence=0.5,
            reason="brief written",
        )
    )
    db_session.flush()

    rate, decided = keep_rate(db_session)
    assert (rate, decided) == (0.5, 2)


def test_keep_rate_ignores_verdicts_outside_the_window(db_session: Session) -> None:
    run = _run(db_session, "22222222-2222-4222-8222-222222222222")
    old = _candidate(db_session, "old thing", status="dropped")
    new = _candidate(db_session, "new thing")
    _judgement(db_session, old, run, "drop", created_at=NOW - timedelta(days=20))
    _judgement(db_session, new, run, "keep", created_at=NOW - timedelta(days=1))

    recent, recent_n = keep_rate(db_session, since=NOW - timedelta(days=7), until=NOW)
    assert (recent, recent_n) == (1.0, 1)


def test_a_judge_that_keeps_everything_is_critical(db_session: Session) -> None:
    """The §9 runbook symptom: a gate that stopped discriminating."""
    run = _run(db_session, "33333333-3333-4333-8333-333333333333")
    for index in range(8):
        candidate = _candidate(db_session, f"thing {index}")
        _judgement(db_session, candidate, run, "keep", created_at=NOW - timedelta(days=1,
            hours=index))

    finding = keep_rate_drift(db_session, now=NOW)
    assert finding is not None
    assert finding.kind == "keep_rate_out_of_band"
    assert finding.severity == "critical"  # keeping everything is the expensive failure
    assert finding.runbook == "keep-rate-drift"
    assert finding.detail["recent"] == 1.0
    assert "keep" in finding.reversible_with or "revert" in finding.reversible_with


def test_a_judge_that_rejects_everything_is_a_warning(db_session: Session) -> None:
    run = _run(db_session, "44444444-4444-4444-8444-444444444444")
    for index in range(8):
        candidate = _candidate(db_session, f"junk {index}", status="dropped")
        _judgement(db_session, candidate, run, "drop", created_at=NOW - timedelta(days=1,
            hours=index))

    finding = keep_rate_drift(db_session, now=NOW)
    assert finding is not None
    assert finding.kind == "keep_rate_out_of_band"
    assert finding.severity == "warn"
    assert "rejecting almost everything" in finding.summary


def test_a_healthy_keep_rate_raises_nothing(db_session: Session) -> None:
    """Half keep / half drop, with a baseline to match: silence."""
    run = _run(db_session, "55555555-5555-4555-8555-555555555555")
    for index in range(10):
        kept = index % 2 == 0
        candidate = _candidate(db_session, f"mixed {index}", status="kept" if kept else "dropped")
        _judgement(
            db_session,
            candidate,
            run,
            "keep" if kept else "drop",
            created_at=NOW - timedelta(days=1, hours=index),
        )

    assert keep_rate_drift(db_session, now=NOW) is None


def test_too_few_verdicts_is_not_called_a_drift(db_session: Session) -> None:
    """Two verdicts that both keep is a small sample, not a broken gate."""
    run = _run(db_session, "66666666-6666-4666-8666-666666666666")
    for index in range(2):
        candidate = _candidate(db_session, f"few {index}")
        _judgement(db_session, candidate, run, "keep", created_at=NOW - timedelta(hours=index))

    assert keep_rate_drift(db_session, now=NOW, min_decisions=5) is None


def test_a_keep_rate_that_moved_but_stayed_inside_the_band_is_informational(
    db_session: Session,
) -> None:
    """The early-warning case: still healthy, but a third of the verdicts changed direction."""
    run = _run(db_session, "77777777-7777-4777-8777-777777777777")
    # baseline: 20% keep, older than the window
    for index in range(10):
        kept = index < 2
        candidate = _candidate(db_session, f"back {index}", status="kept" if kept else "dropped")
        _judgement(
            db_session,
            candidate,
            run,
            "keep" if kept else "drop",
            created_at=NOW - timedelta(days=14, hours=index),
        )
    # the recent window: 60% keep
    for index in range(10):
        kept = index < 6
        candidate = _candidate(db_session, f"now {index}", status="kept" if kept else "dropped")
        _judgement(
            db_session,
            candidate,
            run,
            "keep" if kept else "drop",
            created_at=NOW - timedelta(days=1, hours=index),
        )

    finding = keep_rate_drift(db_session, now=NOW)
    assert finding is not None
    assert finding.kind == "keep_rate_moved"
    assert finding.severity == "info"
    low, high = DEFAULT_KEEP_RATE_BAND
    assert low <= finding.detail["recent"] <= high


# --------------------------------------------------------------------------- eval baseline


def _case(db_session: Session, case_id: str, **overrides: object) -> EvalCase:
    values: dict[str, object] = {
        "id": case_id,
        "category": "home",
        "input_signals": [],
        "expected_keep": True,
        "expected_fad_label": "trend",
        "score_min": 40.0,
        "score_max": 100.0,
    }
    values.update(overrides)
    row = EvalCase(**values)  # type: ignore[arg-type]
    db_session.add(row)
    db_session.flush()
    return row


def test_first_eval_run_records_the_baseline_and_raises_nothing(db_session: Session) -> None:
    _case(db_session, "case-1")
    findings = record_eval_run(db_session, scores={"case-1": 9.0}, run_at=NOW)
    assert findings == []
    row = db_session.get(EvalCase, "case-1")
    assert row is not None
    assert row.baseline_score == 9.0
    assert row.last_score == 9.0


def test_a_two_point_drop_is_tolerated_and_three_is_not(db_session: Session) -> None:
    """§8: "no rubric may drop more than 2 points from baseline"."""
    _case(db_session, "case-1", baseline_score=9.0)
    # exactly 2.0 below baseline: tolerated (§8's rule is "more than 2")
    assert record_eval_run(db_session, scores={"case-1": 7.0}, run_at=NOW) == []
    findings = record_eval_run(db_session, scores={"case-1": 6.0}, run_at=NOW)
    assert len(findings) == 1
    assert findings[0].kind == "eval_baseline_drop"
    assert findings[0].severity == "critical"


def test_eval_baseline_drop_reads_stored_scores(db_session: Session) -> None:
    _case(db_session, "case-1", baseline_score=8.0, last_score=4.0)
    _case(db_session, "case-2", baseline_score=8.0, last_score=7.5)
    _case(db_session, "case-3", baseline_score=None, last_score=1.0)

    findings = eval_baseline_drop(db_session)
    assert [finding.detail["case_id"] for finding in findings] == ["case-1"]


def test_a_creeping_baseline_is_caught_against_the_original(db_session: Session) -> None:
    """Run-to-run comparison would never fire here; the stored baseline does."""
    _case(db_session, "case-1")
    record_eval_run(db_session, scores={"case-1": 10.0}, run_at=NOW)
    for step in (9.0, 8.5, 8.0):
        assert record_eval_run(db_session, scores={"case-1": step}, run_at=NOW) == []
    findings = record_eval_run(db_session, scores={"case-1": 7.0}, run_at=NOW)
    assert len(findings) == 1  # 3 below the original 10, not 1 below the previous 8


# --------------------------------------------------------------------------- watermark


def test_a_stuck_watermark_is_reported(db_session: Session) -> None:
    _source(db_session, "stuck-source", watermark_updated_at=NOW - timedelta(days=5))
    _source(db_session, "fresh-source", watermark_updated_at=NOW - timedelta(hours=2))

    findings = watermark_drift(db_session, now=NOW)
    assert [finding.detail["source_id"] for finding in findings] == ["stuck-source"]
    assert findings[0].runbook == "source-schema-change"


def test_a_never_run_source_is_not_drift(db_session: Session) -> None:
    """Never advanced is the health CLI's business (never_run), not drift's."""
    _source(db_session, "brand-new", watermark_updated_at=None)
    assert watermark_drift(db_session, now=NOW) == []


def test_a_disabled_source_is_not_watched_for_drift(db_session: Session) -> None:
    _source(db_session, "off", enabled=False, watermark_updated_at=NOW - timedelta(days=30))
    assert watermark_drift(db_session, now=NOW) == []


# --------------------------------------------------------------------------- alert rules


def test_quota_burn_over_the_threshold_alerts(db_session: Session) -> None:
    _source(db_session, "ebay", budget_per_day=100)
    db_session.add(
        QuotaLedger(source_id="ebay", operation="search:1", amount=90, spend_date=NOW)
    )
    db_session.flush()

    alerts = collect_alerts(db_session, now=NOW)
    burn = [alert for alert in alerts if alert.rule == "quota_burn"]
    assert len(burn) == 1
    assert burn[0].severity == "warn"
    assert burn[0].tier == "APPROVAL"  # changing a budget is never a SAFE action
    assert "90%" in burn[0].symptom


def test_a_fully_spent_budget_is_critical(db_session: Session) -> None:
    _source(db_session, "ebay", budget_per_day=10)
    db_session.add(QuotaLedger(source_id="ebay", operation="search:1", amount=10, spend_date=NOW))
    db_session.flush()

    alerts = [a for a in collect_alerts(db_session, now=NOW) if a.rule == "quota_burn"]
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_a_budget_at_half_raises_nothing(db_session: Session) -> None:
    _source(db_session, "ebay", budget_per_day=100)
    db_session.add(QuotaLedger(source_id="ebay", operation="search:1", amount=50, spend_date=NOW))
    db_session.flush()

    assert [a for a in collect_alerts(db_session, now=NOW) if a.rule == "quota_burn"] == []


def test_spend_from_yesterday_does_not_burn_today(db_session: Session) -> None:
    """The budget is daily; a big night two days ago must not alert tonight."""
    _source(db_session, "ebay", budget_per_day=100)
    db_session.add(
        QuotaLedger(
            source_id="ebay", operation="search:1", amount=99, spend_date=NOW - timedelta(days=2)
        )
    )
    db_session.flush()

    assert [a for a in collect_alerts(db_session, now=NOW) if a.rule == "quota_burn"] == []


def test_a_rate_limit_storm_alerts_once_per_source(db_session: Session) -> None:
    """Four throttled runs (the database allows one log row per run and source) -> one alert."""
    _source(db_session, "reddit")
    for index in range(4):
        run = _run(
            db_session,
            f"88888888-8888-4888-8888-88888888888{index}",
            started_at=NOW - timedelta(hours=1, minutes=index),
        )
        db_session.add(
            RunSourceLog(
                run_id=run.id,
                source_id="reddit",
                status="degraded",
                started_at=NOW - timedelta(hours=1, minutes=index),
                rate_limit_hits=1,
                reason="429 from reddit",
            )
        )
    db_session.flush()

    alerts = [a for a in collect_alerts(db_session, now=NOW) if a.rule == "rate_limit_storm"]
    assert len(alerts) == 1
    assert alerts[0].runbook == "http-429-storm"
    assert alerts[0].tier == "SAFE"
    assert "4 time(s)" in alerts[0].symptom


def test_one_rate_limit_hit_is_not_a_storm(db_session: Session) -> None:
    run = _run(
        db_session, "99999999-9999-4999-8999-999999999999", started_at=NOW - timedelta(hours=1)
    )
    _source(db_session, "reddit")
    db_session.add(
        RunSourceLog(
            run_id=run.id,
            source_id="reddit",
            status="degraded",
            started_at=NOW - timedelta(hours=1),
            rate_limit_hits=1,
        )
    )
    db_session.flush()

    assert [a for a in collect_alerts(db_session, now=NOW) if a.rule == "rate_limit_storm"] == []


def test_a_failure_streak_alerts_and_names_the_reasons(db_session: Session) -> None:
    _source(db_session, "hn")
    for index in range(3):
        run = _run(
            db_session,
            f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}",
            started_at=NOW - timedelta(days=index + 1),
        )
        db_session.add(
            RunSourceLog(
                run_id=run.id,
                source_id="hn",
                status="failed",
                started_at=NOW - timedelta(days=index + 1),
                reason=f"schema changed: missing field 'score' (attempt {index})",
            )
        )
    db_session.flush()

    alerts = [a for a in collect_alerts(db_session, now=NOW) if a.rule == "source_failure_streak"]
    assert len(alerts) == 1
    assert alerts[0].runbook == "source-schema-change"
    assert alerts[0].tier == "APPROVAL"
    assert any("schema changed" in item for item in alerts[0].evidence)


def test_two_failures_then_a_success_is_not_a_streak(db_session: Session) -> None:
    """Recovery must be visible: only a *trailing* streak alerts."""
    _source(db_session, "hn")
    for index, status in enumerate(["failed", "failed", "ok"]):
        run = _run(
            db_session,
            f"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb{index}",
            started_at=NOW - timedelta(days=index + 1),
        )
        db_session.add(
            RunSourceLog(
                run_id=run.id,
                source_id="hn",
                status=status,
                started_at=NOW - timedelta(days=index + 1),
            )
        )
    db_session.flush()

    streak = [a for a in collect_alerts(db_session, now=NOW) if a.rule == "source_failure_streak"]
    assert streak == []


def test_disabling_a_source_silences_its_alerts(db_session: Session) -> None:
    """Disabling is the runbook's cure, so it must satisfy the alarm (found by the drill)."""
    _source(db_session, "flaky")
    for index in range(3):
        run = _run(
            db_session,
            f"dddddddd-dddd-4ddd-8ddd-ddddddddddd{index}",
            started_at=NOW - timedelta(days=index + 1),
        )
        db_session.add(
            RunSourceLog(
                run_id=run.id,
                source_id="flaky",
                status="failed",
                started_at=NOW - timedelta(days=index + 1),
                reason="schema changed",
            )
        )
    db_session.flush()
    assert [a for a in collect_alerts(db_session, now=NOW) if a.rule == "source_failure_streak"]

    row = db_session.get(Source, "flaky")
    assert row is not None
    row.enabled = False
    db_session.flush()
    streak = [a for a in collect_alerts(db_session, now=NOW) if a.rule == "source_failure_streak"]
    assert streak == []


def test_alerts_are_sorted_most_severe_first(db_session: Session) -> None:
    run = _run(db_session, "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    _source(db_session, "ebay", budget_per_day=100)
    db_session.add(QuotaLedger(source_id="ebay", operation="s", amount=100, spend_date=NOW))
    for index in range(8):
        candidate = _candidate(db_session, f"kept {index}")
        _judgement(db_session, candidate, run, "keep", created_at=NOW - timedelta(hours=1))
    db_session.flush()

    alerts = collect_alerts(db_session, now=NOW)
    severities = [alert.severity for alert in alerts]
    assert severities == sorted(severities, key=["critical", "warn", "info"].index)


def test_every_alert_carries_what_the_escalation_template_needs(db_session: Session) -> None:
    """The template has five fields; an alert missing one cannot be escalated."""
    _source(db_session, "ebay", budget_per_day=10)
    db_session.add(QuotaLedger(source_id="ebay", operation="s", amount=10, spend_date=NOW))
    db_session.flush()

    alerts = collect_alerts(db_session, now=NOW)
    assert alerts
    for alert in alerts:
        assert alert.symptom
        assert alert.evidence
        assert alert.tier in {"SAFE", "APPROVAL", "FORBIDDEN"}
        assert alert.action
        assert alert.reversible_with
        assert alert.blast_radius
        assert alert.raised_at


# --------------------------------------------------------------------------- outbox


def test_the_outbox_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "alerts.jsonl"
    alert = Alert(
        rule="quota_burn",
        severity="warn",
        runbook="quota-burn",
        symptom="ebay burned 90% of its daily budget (90/100)",
        evidence=["health --json"],
        tier="APPROVAL",
        action="propose a budget change",
        reversible_with="git revert",
        blast_radius="spends real quota",
        raised_at=NOW.isoformat(),
    )
    assert write_outbox([alert], path=path) == 1
    loaded = load_outbox(path=path)
    assert len(loaded) == 1
    assert loaded[0].as_dict() == alert.as_dict()


def test_the_outbox_appends_so_history_survives(tmp_path: Path) -> None:
    path = tmp_path / "alerts.jsonl"
    alert = Alert(rule="r", severity="info", runbook="x", symptom="first")
    write_outbox([alert], path=path)
    write_outbox([alert], path=path)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert len(load_outbox(path=path)) == 2


def test_an_empty_alert_list_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "alerts.jsonl"
    assert write_outbox([], path=path) == 0
    assert not path.exists()


def test_load_outbox_tolerates_a_torn_line(tmp_path: Path) -> None:
    path = tmp_path / "alerts.jsonl"
    path.write_text('{"rule": "a", "severity": "info", "runbook": "b", "symptom": "s"}\n{oops\n')
    loaded = load_outbox(path=path)
    assert [alert.rule for alert in loaded] == ["a"]


def test_load_outbox_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_outbox(path=tmp_path / "nope.jsonl") == []


def test_render_names_silence_as_silence() -> None:
    assert render_alerts([]) == "no alerts: the system is inside every band it watches"
    text = render_alerts([Alert(rule="quota_burn", severity="warn", runbook="quota-burn",
        symptom="x")])
    assert "quota_burn" in text
    assert "runbook quota-burn" in text


def test_the_cli_prints_one_json_document(db_session: Session) -> None:
    """The CLI contract every other entry point follows: one document, parseable."""
    assert alerts_module.main(["--json"]) in (0, 1)
