"""Schema tests against real Postgres: the guards, the constraints and the CAS.

These are `db`-marked and run against the isolated `trend_analyst_test` database (see
conftest.py). They assert behaviour the application cannot bypass: append-only history,
double-spend prevention and the Tier-A/L0 invariant — all enforced by the database, not
by a convention in Python.

Every expected failure goes through one of the small `flush_*` helpers below: a single
statement inside `pytest.raises`, wrapped in a SAVEPOINT so that a rejected statement
does not poison the surrounding test transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    check_database,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from trend_analyst.store.models import (
    Brief,
    Candidate,
    EvalCase,
    LLMCache,
    RawItem,
    Run,
    RunSourceLog,
    Score,
    SignalRow,
    Source,
)

pytestmark = pytest.mark.db

#: The specification's §7 table list, plus one documented addition:
#: `judgements` (deviation D12) — §6.2 requires the Judge's cited verdicts to be durable and §6.3
#: makes grounding a code-enforced rule, but the table list has nowhere to put either, and the LLM
#: cache expires after 30 days. It is asserted here rather than assumed, so the deviation is
#: visible to anyone comparing this test with the specification.
EXPECTED_TABLES = {
    "briefs",
    "candidates",
    "eval_cases",
    "judgements",
    "llm_cache",
    "raw_items",
    "run_source_log",
    "runs",
    "scores",
    "signals",
    "sources",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def flush_objects(session: Session, *objects: Any) -> None:
    """Add objects and flush — one call, so `pytest.raises` sees a single statement."""
    for obj in objects:
        session.add(obj)
    session.flush()


def flush_sql(session: Session, sql: str, **params: Any) -> None:
    """Execute SQL and flush."""
    session.execute(text(sql), params)
    session.flush()


def flush_action(session: Session, action: Callable[[Session], None]) -> None:
    """Run a mutating action and flush (for ORM deletes and updates)."""
    action(session)
    session.flush()


def make_run(session: Session, **overrides: object) -> Run:
    payload: dict[str, object] = {"status": "running", "trigger": "manual", **overrides}
    run = Run(**payload)  # type: ignore[arg-type]
    session.add(run)
    session.flush()
    return run


def make_candidate(
    session: Session, phrase: str = "standing desk", **overrides: object
) -> Candidate:
    payload: dict[str, object] = {"phrase": phrase, "category": "home-office", **overrides}
    candidate = Candidate(**payload)  # type: ignore[arg-type]
    session.add(candidate)
    session.flush()
    return candidate


def make_score(session: Session, run: Run, candidate: Candidate, **overrides: object) -> Score:
    payload: dict[str, object] = {
        "candidate_id": candidate.id,
        "run_id": run.id,
        "weights_version": "v1",
        "demand_velocity": 80.0,
        "saturation": 30.0,
        "buyer_pain": 70.0,
        "money": 60.0,
        "feasibility": 90.0,
        "mgs": 71.5,
        "fad_probability": 0.2,
        "fad_label": "trend",
        "revenue_p10": 7_000.0,
        "revenue_p50": 18_000.0,
        "revenue_p90": 34_000.0,
        **overrides,
    }
    score = Score(**payload)  # type: ignore[arg-type]
    session.add(score)
    session.flush()
    return score


def make_source(**overrides: object) -> Source:
    payload: dict[str, object] = {
        "id": "src",
        "role": "r",
        "tier": "S",
        "layers": ["L0"],
        "schedule": "nightly",
        "budget_per_day": 10,
        "rps": 1.0,
        "cache_ttl_h": 24,
        "enabled": True,
        "domains": ["x.test"],
        **overrides,
    }
    return Source(**payload)  # type: ignore[arg-type]


def make_brief(score: Score, candidate: Candidate, run: Run, **overrides: object) -> Brief:
    payload: dict[str, object] = {
        "score_id": score.id,
        "candidate_id": candidate.id,
        "run_id": run.id,
        "verdict": "worth building",
        "body_md": "# verdict",
        "model": "m",
        "citations": [],
        **overrides,
    }
    return Brief(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------
def test_database_has_exactly_the_spec_tables(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES | {"alembic_version"}


def test_append_only_triggers_exist(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        triggers = set(
            connection.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            ).scalars()
        )
    assert triggers.issuperset(
        {
            "trg_scores_append_only",
            "trg_briefs_append_only",
            "trg_judgements_append_only",
        }
    )


def test_check_database_reports_version_and_pgvector(db_engine: Engine) -> None:
    status = check_database(db_engine)
    assert status.ok is True
    assert status.server_version is not None
    assert status.server_version.startswith("18")
    assert status.pgvector_version is not None
    assert status.detail is None


def test_check_database_reports_down_instead_of_raising() -> None:
    """Health must be able to print "down, with a reason" — never crash (spec §9)."""
    unreachable = create_db_engine(url="postgresql+psycopg://nobody:nope@127.0.0.1:59999/none")
    status = check_database(unreachable)
    assert status.ok is False
    assert status.detail is not None


def test_engine_refuses_to_guess_a_database_url() -> None:
    class NoDatabase:
        class db:  # noqa: N801
            configured = False
            dsn = ""

    with pytest.raises(DatabaseNotConfiguredError, match=r"provision_pg\.sh"):
        create_db_engine(NoDatabase())  # type: ignore[arg-type]


def test_session_factory_scope_rolls_back_on_error(db_engine: Engine) -> None:
    def boom(session: Session) -> None:
        session.add(make_source(id="temp_source"))
        raise RuntimeError("boom")

    factory = create_session_factory(db_engine)
    with pytest.raises(RuntimeError), session_scope(factory) as session:
        boom(session)

    with session_scope(factory) as session:
        assert session.get(Source, "temp_source") is None, "the failed unit of work persisted"


# ---------------------------------------------------------------------------
# Constraints that mirror the registry (spec §4.3)
# ---------------------------------------------------------------------------
def test_tier_a_in_l0_is_rejected_by_the_database(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(db_session, make_source(id="bad_source", tier="A", layers=["L0"]))


@pytest.mark.parametrize(
    ("field", "value"),
    [("budget_per_day", 0), ("rps", 0.0), ("tier", "Z")],
)
def test_source_constraints_reject_nonsense(db_session: Session, field: str, value: object) -> None:
    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(db_session, make_source(**{field: value}))


def test_scores_constraints_reject_unordered_revenue(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    with pytest.raises(IntegrityError), db_session.begin_nested():
        make_score(db_session, run, candidate, revenue_p10=50_000.0, revenue_p50=18_000.0)


def test_scores_constraints_reject_impossible_mgs(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    with pytest.raises(IntegrityError), db_session.begin_nested():
        make_score(db_session, run, candidate, mgs=140.0)


# ---------------------------------------------------------------------------
# Append-only history (spec §5.3) — the guards the application cannot bypass
# ---------------------------------------------------------------------------
def test_scores_reject_update(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    score = make_score(db_session, run, candidate)

    with pytest.raises(DBAPIError, match="append-only"), db_session.begin_nested():
        flush_sql(db_session, "UPDATE scores SET mgs = 1.0 WHERE id = :id", id=score.id)


def test_scores_reject_delete(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    score = make_score(db_session, run, candidate)

    with pytest.raises(DBAPIError, match="append-only"), db_session.begin_nested():
        flush_action(db_session, lambda session: session.delete(score))


def test_briefs_reject_update(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    score = make_score(db_session, run, candidate)
    brief = make_brief(score, candidate, run)
    flush_objects(db_session, brief)

    with pytest.raises(DBAPIError, match="append-only"), db_session.begin_nested():
        flush_sql(db_session, "UPDATE briefs SET verdict = 'changed' WHERE id = :id", id=brief.id)


def test_briefs_cannot_be_emitted_twice_for_one_run(db_session: Session) -> None:
    run = make_run(db_session)
    candidate = make_candidate(db_session)
    score = make_score(db_session, run, candidate)
    other_score = make_score(db_session, run, candidate, weights_version="v2")
    flush_objects(db_session, make_brief(score, candidate, run))

    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(db_session, make_brief(other_score, candidate, run))


def test_raw_items_dedup_key_blocks_the_same_payload_twice(db_session: Session) -> None:
    """Spec §5.2: an identical hash means skip parse, skip score, skip LLM."""
    def item() -> RawItem:
        return RawItem(
            source_id="hn_firebase",
            content_hash="a" * 64,
            fetched_at=datetime.now(UTC),
            byte_size=3,
            payload={"items": []},
        )

    flush_objects(db_session, item())
    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(db_session, item())


def test_run_source_log_is_unique_per_run_and_source(db_session: Session) -> None:
    run = make_run(db_session)
    flush_objects(db_session, RunSourceLog(run_id=run.id, source_id="hn_firebase", status="ok"))

    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(
            db_session, RunSourceLog(run_id=run.id, source_id="hn_firebase", status="failed")
        )


# ---------------------------------------------------------------------------
# The remaining tables round-trip
# ---------------------------------------------------------------------------
def test_signals_lake_cache_and_eval_cases_round_trip(db_session: Session) -> None:
    signal = SignalRow(
        source_id="hn_firebase", entity="standing desk", metric="mentions",
        value=12.0, ts=datetime.now(UTC), category="home-office",
    )
    cache = LLMCache(
        cache_key="c" * 64, gate="judge", model="gemini-flash",
        output={"keep": True}, expires_at=datetime.now(UTC),
    )
    case = EvalCase(
        id="case-001", category="home-office", input_signals=[], expected_keep=True,
        expected_fad_label="trend", score_min=60.0, score_max=80.0, notes="seeded in P2",
    )
    flush_objects(db_session, signal, cache, case)

    # NB: assert on the object's own id — a bigserial keeps counting across rolled-back
    # transactions, so "id == 1" is not a safe assumption.
    assert signal.id is not None
    assert db_session.get(SignalRow, signal.id) is not None
    assert db_session.get(LLMCache, "c" * 64) is not None
    assert db_session.get(EvalCase, "case-001") is not None


def test_llm_cache_gate_is_constrained(db_session: Session) -> None:
    cache = LLMCache(
        cache_key="d" * 64, gate="not-a-gate", model="m", output={},
        expires_at=datetime.now(UTC),
    )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        flush_objects(db_session, cache)


def test_run_status_is_constrained(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin_nested():
        make_run(db_session, status="finished-ish")
