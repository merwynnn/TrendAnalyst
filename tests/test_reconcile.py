"""Reconciliation tests: the brief's P4 bar, made checkable.

*"Full nightly run completes inside budget with quota ledger balanced to zero unexplained spend."*
These tests are what turns that sentence into something a machine can disagree with: every way a
spend row can be unattributable is constructed here, and the report must catch each one.

The complement matters as much. A reconciliation that flags everything is as useless as one that
flags nothing, so there are tests for spend that *is* explainable — including two cases that look
suspicious and are not: a source collected in several runs (compared per run, not per source), and a
prompt paid for twice in different runs (attributed, therefore a note rather than unexplained).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.monitor.reconcile import accounted_totals, classify, reconcile
from trend_analyst.store.models import LLMCache, QuotaLedger, Run, RunSourceLog, Source

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.fixture
def sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def seed_source(session: Session, source_id: str = "arctic_shift") -> None:
    """A registry mirror row: the ledger's source must exist to be explainable."""
    if session.get(Source, source_id) is None:
        session.add(
            Source(
                id=source_id,
                role="test",
                tier="S",
                layers=["L0"],
                schedule="nightly",
                budget_per_day=1000,
                rps=1.0,
                enabled=True,
                domains=["example.com"],
            )
        )
        session.flush()


def seed_cache(session: Session, key: str, prompt: int, completion: int) -> None:
    session.add(
        LLMCache(
            cache_key=key,
            gate="judge",
            model="gemini-flash-latest",
            output={"verdicts": []},
            created_at=NOW,
            expires_at=datetime(2026, 10, 20, tzinfo=UTC),
            prompt_tokens=prompt,
            completion_tokens=completion,
        )
    )
    session.flush()


def seed_run(session: Session) -> Run:
    run = Run(status="running", trigger="manual")
    session.add(run)
    session.flush()
    return run


def seed_l0_spend(session: Session, run: Run, source_id: str, amount: int) -> None:
    seed_source(session, source_id)
    session.add(
        QuotaLedger(
            source_id=source_id,
            run_id=run.id,
            operation=f"l0:{source_id}",
            amount=amount,
            spend_date=NOW,
        )
    )
    session.add(
        RunSourceLog(
            run_id=run.id,
            source_id=source_id,
            status="ok",
            started_at=NOW,
            items_fetched=1,
            items_new=1,
            quota_spent=amount,
        )
    )
    session.flush()


def seed_llm_spend(session: Session, run: Run, key: str, prompt: int, completion: int) -> None:
    seed_cache(session, key, prompt, completion)
    session.add(
        QuotaLedger(
            source_id="llm_judge",
            run_id=run.id,
            operation=f"llm:judge:tokens:{key[:24]}",
            amount=prompt + completion,
            spend_date=NOW,
            reason=f"gemini-flash-latest: prompt={prompt} completion={completion}",
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def test_classify_knows_the_operations_this_code_writes() -> None:
    assert classify("l0:arctic_shift") == ("l0", "arctic_shift")
    assert classify("llm:judge:tokens:abc") == ("llm", "judge")
    assert classify("llm:writer:tokens:abc") == ("llm", "writer")
    assert classify("mystery:thing")[0] == "unknown"
    assert classify("llm:judge")[0] == "unknown"  # no token marker: not a shape we write


# ---------------------------------------------------------------------------
# the balanced case
# ---------------------------------------------------------------------------
def test_a_healthy_ledger_balances(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        seed_l0_spend(session, run, "arctic_shift", 15)
        seed_llm_spend(session, run, "a" * 64, 400, 200)
        report = reconcile(session)
        assert report.balanced, report.as_dict()
        assert report.rows == 2
        assert report.explained == 2
        assert report.spend_total == 615
        assert report.per_source["arctic_shift"]["ledger"] == 15
        assert report.per_source["arctic_shift"]["run_log"] == 15
        assert report.per_gate["judge"]["ledger"] == 600


def test_the_same_source_collected_in_two_runs_is_not_flagged(
    sessions: sessionmaker[Session],
) -> None:
    """The bug this test exists for: a per-source total compared against a per-run log.

    The first implementation accumulated a source's ledger rows across every run and compared the
    total with a single (run, source) log amount, so any source collected twice was reported as
    unexplained spend — 71 false positives on the development database.
    """
    with sessions() as session:
        first = seed_run(session)
        second = seed_run(session)
        seed_l0_spend(session, first, "arctic_shift", 15)
        seed_l0_spend(session, second, "arctic_shift", 15)
        report = reconcile(session)
        assert report.balanced, report.as_dict()
        assert report.per_source["arctic_shift"]["ledger"] == 30
        assert report.per_source["arctic_shift"]["run_log"] == 30


def test_two_runs_paying_for_the_same_prompt_is_attributed_not_unexplained(
    sessions: sessionmaker[Session],
) -> None:
    """A `--fresh` re-run really pays again; the cache row keeps the first call's counts."""
    with sessions() as session:
        first = seed_run(session)
        second = seed_run(session)
        key = "b" * 64
        seed_llm_spend(session, first, key, 400, 200)
        # The second run's call: same prompt, more tokens, and the cache row is not rewritten.
        session.add(
            QuotaLedger(
                source_id="llm_judge",
                run_id=second.id,
                operation=f"llm:judge:tokens:{key[:24]}",
                amount=900,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert report.balanced, report.as_dict()
        assert report.per_gate["judge"]["ledger"] == 1500
        assert report.per_gate["judge"]["unmatched_tokens"] == 300


# ---------------------------------------------------------------------------
# every way a row can be unattributable
# ---------------------------------------------------------------------------
def test_an_unknown_operation_is_unexplained(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        session.add(
            QuotaLedger(
                source_id="mystery", run_id=run.id, operation="mystery:thing", amount=5,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "no subsystem" in report.unexplained[0]["reason"]


def test_spend_with_no_run_log_is_unexplained(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        seed_source(session, "arctic_shift")
        session.add(
            QuotaLedger(
                source_id="arctic_shift", run_id=run.id, operation="l0:arctic_shift", amount=15,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "no run_source_log row" in report.unexplained[0]["reason"]


def test_the_ledger_and_the_run_log_must_agree(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        seed_l0_spend(session, run, "arctic_shift", 15)
        log = session.query(RunSourceLog).one()
        log.quota_spent = 9  # the two records disagree
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "run_source_log says 9" in report.unexplained[0]["reason"]


def test_a_source_outside_the_registry_is_unexplained(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        session.add(
            QuotaLedger(
                source_id="ghost_source", run_id=run.id, operation="l0:ghost_source", amount=3,
                spend_date=NOW,
            )
        )
        session.add(
            RunSourceLog(
                run_id=run.id, source_id="ghost_source", status="ok", started_at=NOW,
                items_fetched=0, items_new=0, quota_spent=3,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "not in the registry mirror" in report.unexplained[0]["reason"]


def test_token_spend_with_no_cached_answer_is_unexplained(
    sessions: sessionmaker[Session],
) -> None:
    """The exact case the brief means by unexplained: we paid and stored nothing."""
    with sessions() as session:
        run = seed_run(session)
        session.add(
            QuotaLedger(
                source_id="llm_judge",
                run_id=run.id,
                operation=f"llm:judge:tokens:{'c' * 24}",
                amount=500,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "no cached answer matches" in report.unexplained[0]["reason"]


def test_token_spend_without_a_cache_key_is_unexplained(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        session.add(
            QuotaLedger(
                source_id="llm_judge", run_id=run.id, operation="llm:judge:tokens:",
                amount=500, spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        # "llm:judge:tokens:" has no key portion, so the shape is unknown rather than keyless
        assert report.unexplained[0]["reason"]


def test_an_unknown_gate_is_unexplained(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        key = "d" * 24
        seed_cache(session, key, 100, 50)
        session.add(
            QuotaLedger(
                source_id="llm_soothsayer", run_id=run.id, operation=f"llm:soothsayer:tokens:{key}",
                amount=150, spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert not report.balanced
        assert "unknown gate" in report.unexplained[0]["reason"]


def test_an_empty_ledger_is_balanced_and_says_so(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        report = reconcile(session)
        assert report.balanced
        assert report.rows == 0
        assert any("ledger is empty" in note for note in report.notes)


def test_accounted_totals_show_the_three_views(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        run = seed_run(session)
        seed_l0_spend(session, run, "arctic_shift", 15)
        seed_llm_spend(session, run, "e" * 64, 400, 200)
        totals = accounted_totals(session)
        assert totals["ledger_by_kind"] == {"l0": 15, "llm": 600}
        assert totals["cache_tokens"] == 600
        assert totals["run_log_spend"] == 15


def test_reconcile_can_be_scoped(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        first = seed_run(session)
        second = seed_run(session)
        seed_l0_spend(session, first, "arctic_shift", 15)
        seed_l0_spend(session, second, "hn_firebase", 7)
        scoped = reconcile(session, run_id=first.id)
        assert scoped.rows == 1
        assert scoped.spend_total == 15
        by_source = reconcile(session, source_ids=["hn_firebase"])
        assert by_source.rows == 1
        assert by_source.spend_total == 7


def test_run_id_none_is_still_explained_by_the_run_log(sessions: sessionmaker[Session]) -> None:
    """The drill and the replays record spend with no run; the ledger must still be attributable."""
    with sessions() as session:
        key = "f" * 64
        seed_cache(session, key, 63, 28)
        session.add(
            QuotaLedger(
                source_id="llm_judge",
                run_id=None,
                operation=f"llm:judge:tokens:{key[:24]}",
                amount=91,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session)
        assert report.balanced, report.as_dict()


def test_an_unrelated_run_id_does_not_break_attribution(sessions: sessionmaker[Session]) -> None:
    """Guard against a future change that starts writing a run id the log does not know."""
    with sessions() as session:
        run = seed_run(session)
        seed_l0_spend(session, run, "arctic_shift", 15)
        stray = uuid.uuid4()
        session.add(
            QuotaLedger(
                source_id="arctic_shift", run_id=stray, operation="l0:arctic_shift", amount=4,
                spend_date=NOW,
            )
        )
        session.flush()
        report = reconcile(session, run_id=run.id)
        assert report.balanced
        assert report.rows == 1
