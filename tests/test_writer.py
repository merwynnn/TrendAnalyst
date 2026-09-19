"""The Writer gate: top-K selection, citations, idempotency, degradation, rendering.

Same discipline as the Judge's tests: a stub transport, no network, no quota. What is asserted here
is what would be expensive to get wrong —

* only `kept` candidates get a page, and only the top-K of them (spec §6.2's ~30-call budget);
* a brief's citations must come from the evidence the brief is *about*, plus quotes the Judge
  already verified — a writer may not invent a source;
* a brief whose citations all vanish is stored, marked, and rendered with the warning at the top;
* a resume writes nothing, and a degraded gate leaves the archive untouched;
* the rendered page carries the numbers it was written about (MGS, revenue triple, weights version).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.llm.gates import judge_candidates
from trend_analyst.llm.gateway import GateBudget, ProviderSpec
from trend_analyst.llm.schemas import JudgeBatch, JudgeVerdict, WriterBrief
from trend_analyst.llm.writer import (
    DEFAULT_TOP_K,
    brief_markdown,
    pending_briefs,
    stored_briefs,
    write_briefs,
)
from trend_analyst.pipeline.briefs import render_brief, render_briefs_table
from trend_analyst.store.models import Brief, Candidate, Judgement, Run, Score, SignalRow

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
URL_A = "https://www.reddit.com/r/Tools/comments/abc/circ_saw_guard/"
URL_B = "https://news.ycombinator.com/item?id=49734467"
INVENTED = "https://example.com/nobody-collected-this"
PHRASES = ("circ saw", "saw blade", "rebar cutter", "larger mug")


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


def seed(
    session: Session, phrases: Sequence[str] = PHRASES, *, status: str = "kept"
) -> list[int]:
    """Candidates with scores and evidence — the state a decided night leaves behind."""
    run = Run(status="running", trigger="manual")
    session.add(run)
    session.flush()
    ids: list[int] = []
    for index, phrase in enumerate(phrases):
        candidate = Candidate(phrase=phrase, category="tools_diy", mentions=3, status=status)
        session.add(candidate)
        session.flush()
        ids.append(int(candidate.id))
        session.add(
            Score(
                candidate_id=candidate.id,
                run_id=run.id,
                weights_version="v1",
                demand_velocity=50.0,
                saturation=45.0,
                buyer_pain=60.0,
                money=55.0,
                feasibility=77.0,
                mgs=60.0 - index,
                fad_probability=0.3,
                fad_label="trend",
                revenue_p10=25.0,
                revenue_p50=180.0,
                revenue_p90=1000.0,
            )
        )
        session.add(
            SignalRow(
                source_id="arctic_shift",
                entity=f"my {phrase} broke, the guard is flimsy",
                metric="reddit_score",
                value=4.0,
                ts=NOW - timedelta(days=index),
                category="tools_diy",
                quote="the guard cracked in a week",
                url=URL_A,
            )
        )
    session.flush()
    return ids


def brief_payload(phrase: str, *, url: str = URL_A, quotes: int = 2) -> dict:
    return {
        "phrase": phrase,
        "verdict": (
            f"{phrase} is a real gap: buyers complain about the guard and the price band fits."
        ),
        "players": ["Incumbent A", "Incumbent B"],
        "risks": ["seasonality", "shipping"],
        "angles": ["a better guard", "a bundle"],
        "revenue_reasoning": "At a $90 median and a 5% conversion the estimate holds.",
        "quotes": [
            {"text": f"quote {index}", "url": url, "source_id": "arctic_shift"}
            for index in range(quotes)
        ],
    }


def sender_returning(payloads: dict[str, dict], *, tokens: tuple[int, int] = (200, 120)):
    """A stub that answers per phrase, exactly the way a batched provider would."""

    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        for phrase, payload in payloads.items():
            if f'"phrase": "{phrase}"' in prompt:
                return json.dumps(payload), tokens[0], tokens[1]
        return json.dumps(payloads[next(iter(payloads))]), tokens[0], tokens[1]

    return send


def failing_sender(message: str = "503 provider down"):
    def send(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        raise RuntimeError(message)

    return send


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def test_pending_briefs_takes_only_the_top_k(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        queue = pending_briefs(session, top_k=2)
        assert [str(candidate.phrase) for candidate in queue] == ["circ saw", "saw blade"]
        # The order is by MGS: the Writer's budget is the scarcest, so the best candidates first.
        assert float(queue[0].mentions) > 0


def test_pending_briefs_ignores_candidates_that_were_not_kept(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session, status="dropped")
        assert pending_briefs(session, top_k=5) == []


def test_a_candidate_without_a_score_is_not_queued(sessions: sessionmaker[Session]) -> None:
    """Briefs attach to a score snapshot, so an unscored candidate has nothing to attach to.

    The score is simply never created here — deleting one is impossible by design: `scores` is
    append-only and the trigger refuses it (which is why this test does not try).
    """
    with sessions() as session:
        candidate = Candidate(phrase="circ saw", category="tools_diy", status="kept")
        session.add(candidate)
        session.flush()
        assert pending_briefs(session, top_k=5) == []


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------
def test_write_briefs_stores_a_page_and_marks_the_candidate(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        payloads = {phrase: brief_payload(phrase) for phrase in PHRASES}
        report = write_briefs(
            session, run_id=run.id, sender=sender_returning(payloads), top_k=2, now=NOW
        )
        assert report.ok, report.reason
        assert report.written == 2
        assert report.prompt_tokens == 400  # two calls of 200
        assert report.ungrounded == 0
        rows = session.execute(select(Brief).order_by(Brief.id)).scalars().all()
        assert len(rows) == 2
        assert rows[0].citations
        assert rows[0].citations[0]["url"] == URL_A
        assert "circ saw" in rows[0].body_md
        assert rows[0].prompt_tokens == 200
        statuses = {
            str(candidate.phrase): str(candidate.status)
            for candidate in session.execute(select(Candidate)).scalars()
        }
        assert statuses["circ saw"] == "briefed"
        assert statuses["rebar cutter"] == "kept"  # outside the top-K, untouched


def test_the_rendered_page_carries_its_numbers(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            top_k=1,
            now=NOW,
        )
        body = brief_markdown(session, "circ saw")
        assert body is not None
        assert "# circ saw" in body
        assert "MGS" in body
        assert "60.0" in body
        assert "$25 - $180 - $1,000" in body
        assert "Weights" in body
        assert "v1" in body
        assert "## Citations" in body
        assert URL_A in body
        assert "written by gemini" in body


def test_brief_reuse_of_a_judged_citation_is_allowed(sessions: sessionmaker[Session]) -> None:
    """The Judge's verified quotes are evidence the writer may cite; invented sources are not."""
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        candidate = session.execute(select(Candidate)).scalars().one()
        session.add(
            Judgement(
                candidate_id=int(candidate.id),
                run_id=run.id,
                gate="judge",
                decision="keep",
                confidence=0.8,
                quotes=[{"text": "guard cracked", "url": URL_B, "source_id": "hn_firebase"}],
                enrich=["ebay sold listings"],
            )
        )
        session.flush()
        report = write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw", url=URL_B)}),
            top_k=1,
            now=NOW,
        )
        assert report.written == 1
        assert report.ungrounded == 0
        row = session.execute(select(Brief)).scalars().one()
        assert row.citations[0]["url"] == URL_B


def test_an_invented_citation_is_stripped_from_the_brief(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw", url=INVENTED)}),
            top_k=1,
            now=NOW,
        )
        assert report.written == 1
        assert report.ungrounded == 1
        assert report.removed_quotes == 2
        row = session.execute(select(Brief)).scalars().one()
        assert row.citations == []
        assert "No surviving citation" in row.body_md


# ---------------------------------------------------------------------------
# idempotency, caps, degradation
# ---------------------------------------------------------------------------
def test_a_resume_writes_nothing_new(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        sender = sender_returning({"circ saw": brief_payload("circ saw")})
        first = write_briefs(session, run_id=run.id, sender=sender, top_k=1, now=NOW)
        assert first.written == 1
        second = write_briefs(session, run_id=run.id, sender=sender, top_k=1, now=NOW)
        assert second.written == 0
        assert second.skipped_existing in {0, 1}  # queue is empty after the first write
        assert len(session.execute(select(Brief)).scalars().all()) == 1


def test_a_degraded_writer_changes_nothing(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = write_briefs(session, run_id=run.id, sender=failing_sender(), top_k=1, now=NOW)
        assert report.status == "degraded"
        assert "dropping with reason" in report.reason
        assert session.execute(select(Brief)).scalars().all() == []
        assert (
            str(session.execute(select(Candidate)).scalars().one().status) == "kept"
        )  # not marked briefed


def test_the_call_cap_stops_the_writer(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    def counting(provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        return json.dumps(brief_payload("circ saw")), 10, 5

    limits = GateBudget(gate="writer", calls_per_day=1, tokens_per_day=100_000)
    with sessions() as session:
        seed(session, phrases=("circ saw", "saw blade"))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = write_briefs(
            session, run_id=run.id, sender=counting, budget=limits, top_k=2, now=NOW
        )
    assert report.status == "partial"
    assert "call cap reached" in report.reason
    assert len(calls) == 1


def test_dry_run_writes_nothing(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            top_k=1,
            now=NOW,
            dry_run=True,
        )
        assert report.written == 0
        assert session.execute(select(Brief)).scalars().all() == []


def test_no_kept_candidate_is_an_empty_run_not_an_error(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session, status="dropped")
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            now=NOW,
        )
        assert report.status == "empty"
        assert report.ok
        assert "no kept candidate" in report.reason


# ---------------------------------------------------------------------------
# The renderer is pure code: no database, no model
# ---------------------------------------------------------------------------
def test_render_brief_is_deterministic_and_marks_missing_citations() -> None:
    brief = WriterBrief.model_validate(brief_payload("circ saw", quotes=0))
    first = render_brief(brief, category="tools_diy", mgs=60.0, model="gemini:test", as_of=NOW)
    second = render_brief(brief, category="tools_diy", mgs=60.0, model="gemini:test", as_of=NOW)
    assert first.markdown == second.markdown
    assert first.grounded is False
    assert "No surviving citation" in first.markdown
    assert first.sections == ("players", "risks", "angles")


def test_render_briefs_table_handles_nothing() -> None:
    assert "no briefs yet" in render_briefs_table([])
    table = render_briefs_table(
        [{"phrase": "circ saw", "model": "gemini:x", "citations": 2, "revenue_p50": 180.0}]
    )
    assert "circ saw" in table
    assert "180" in table


def test_stored_briefs_returns_the_newest_per_candidate(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            top_k=1,
            now=NOW,
        )
        later = Run(status="running", trigger="manual")
        session.add(later)
        session.flush()
        write_briefs(
            session,
            run_id=later.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            top_k=1,
            now=NOW,
        )
        rows = stored_briefs(session)
        assert len(rows) == 1  # one page per candidate, the newest
        assert rows[0]["phrase"] == "circ saw"
        assert rows[0]["citations"] == 2


def test_the_judge_and_the_writer_compose(sessions: sessionmaker[Session]) -> None:
    """The brief's P3 bar read literally: judge, then writer, on the same inputs."""

    def judge_sender(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        verdicts = [
            {
                "phrase": phrase,
                "category": "tools_diy",
                "decision": "keep" if phrase == "circ saw" else "drop",
                "confidence": 0.8,
                "reason": "guard complaints recur",
                "quotes": [{"text": "guard cracked", "url": URL_A, "source_id": "arctic_shift"}],
                "enrich": ["ebay sold listings"],
            }
            for phrase in ("circ saw", "saw blade")
            if f'"phrase": "{phrase}"' in prompt
        ]
        batch = JudgeBatch(verdicts=[JudgeVerdict.model_validate(v) for v in verdicts])
        return batch.model_dump_json(), 90, 40

    with sessions() as session:
        seed(session, phrases=("circ saw", "saw blade"), status="active")
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        judged = judge_candidates(
            session, run_id=run.id, sender=judge_sender, candidates=None, now=NOW
        )
        assert judged.judged == 2
        assert judged.kept == 1
        written = write_briefs(
            session,
            run_id=run.id,
            sender=sender_returning({"circ saw": brief_payload("circ saw")}),
            top_k=DEFAULT_TOP_K,
            now=NOW,
        )
        assert written.written == 1, written.reason
        assert written.briefs[0]["phrase"] == "circ saw"
        assert written.briefs[0]["citations"] == 2


