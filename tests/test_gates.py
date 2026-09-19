"""The Judge gate: batching, grounding, verdict application and honest degradation.

Every test here runs with a **stub transport**, so the whole gate is exercised with no network and
no quota — which matters twice over: the free tier ran out during the drill (LESSONS §6.5), and a
test that needs a provider is a test that fails when the provider is busy.

The five properties worth more than the rest:

* a degraded gate changes **nothing** (a provider outage must never look like a mass deletion);
* a verdict naming a phrase outside its batch is **ignored**, not applied to a lookalike;
* a quote citing a URL the pipeline never collected is **stripped**, and the verdict marked;
* caps are **terminal** — an unjudged candidate stays unjudged, visibly;
* a resumed run **re-judges nothing**: the unique key on (candidate, run, gate) is the guard.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.llm.gates import (
    DEFAULT_BATCH_SIZE,
    build_batch,
    candidate_evidence,
    judge_candidates,
    latest_judgements,
    pending_judgements,
)
from trend_analyst.llm.gateway import GateBudget, ProviderSpec
from trend_analyst.store.models import Candidate, Judgement, Run, Score, SignalRow

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 20, tzinfo=UTC)
URL_A = "https://www.reddit.com/r/Tools/comments/abc/circ_saw_guard/"
URL_B = "https://news.ycombinator.com/item?id=49734467"
BROKEN = "https://example.com/invented-study"


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
    session: Session, phrases: Sequence[str] = ("circ saw", "saw blade", "larger mug")
) -> list[int]:
    """Three candidates with scores and evidence — the shape a decide run leaves behind."""
    run = Run(status="running", trigger="manual")
    session.add(run)
    session.flush()
    ids: list[int] = []
    for index, phrase in enumerate(phrases):
        candidate = Candidate(phrase=phrase, category="tools_diy", mentions=2 + index)
        session.add(candidate)
        session.flush()
        ids.append(int(candidate.id))
        session.add(
            Score(
                candidate_id=candidate.id,
                run_id=run.id,
                weights_version="v1",
                demand_velocity=50.0,
                saturation=50.0,
                buyer_pain=60.0,
                money=55.0,
                feasibility=77.0,
                mgs=54.0 - index,
                fad_probability=0.3,
                fad_label="trend",
                revenue_p10=20.0,
                revenue_p50=150.0,
                revenue_p90=900.0,
            )
        )
        session.add(
            SignalRow(
                source_id="arctic_shift" if index % 2 == 0 else "hn_firebase",
                entity=f"my {phrase} broke and the guard is flimsy",
                metric="reddit_score",
                value=3.0,
                ts=NOW - timedelta(days=index),
                category="tools_diy",
                quote=f"the {phrase} guard cracked in a week",
                url=URL_A if index % 2 == 0 else URL_B,
            )
        )
    session.flush()
    return ids


def verdict_payload(
    phrases: Sequence[str],
    *,
    decisions: Sequence[str] | None = None,
    urls: Sequence[str] | None = None,
    extra_phrase: str | None = None,
) -> str:
    choices = list(decisions or ["keep"] * len(phrases))
    url_list = list(urls or [URL_A] * len(phrases))
    verdicts = [
        {
            "phrase": phrase,
            "category": "tools_diy",
            "decision": choices[index % len(choices)],
            "confidence": 0.8,
            "fad_label": "trend",
            "fad_probability": 0.25,
            "reason": f"{phrase} shows repeated complaints about the guard",
            "quotes": [{"text": "guard cracked in a week", "url": url_list[index % len(url_list)],
                        "source_id": "arctic_shift"}],
            "enrich": ["ebay sold listings", "amazon review volume"],
        }
        for index, phrase in enumerate(phrases)
    ]
    if extra_phrase:
        verdicts.append(
            {
                "phrase": extra_phrase,
                "decision": "keep",
                "confidence": 0.9,
                "reason": "invented",
                "quotes": [],
                "enrich": [],
            }
        )
    return json.dumps({"verdicts": verdicts})


def stub_sender(payload: str, *, prompt_tokens: int = 120, completion_tokens: int = 60):
    """A stub that ignores the prompt. Only safe when the batch cannot change between calls.

    The re-ask rounds made that assumption false: a prompt-blind stub answers every round with the
    same phrase, so the re-ask tires itself out and every round strips the same quotes again. Tests
    that exercise rounds use `answering_sender` instead.
    """

    def send(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        return payload, prompt_tokens, completion_tokens

    return send


PHRASE_PATTERN = re.compile(r'"phrase": "([^"]+)"')


def answering_sender(
    *,
    decisions: Sequence[str] = ("keep",),
    urls: Sequence[str] = (URL_A,),
    prompt_tokens: int = 120,
    completion_tokens: int = 60,
):
    """A stub that answers about exactly the candidates named in the prompt.

    This is the shape a real provider is trusted to produce, and the shape the re-ask logic depends
    on: N candidates in, N verdicts out.
    """

    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        phrases = list(dict.fromkeys(PHRASE_PATTERN.findall(prompt)))
        verdicts = [
            {
                "phrase": phrase,
                "category": "tools_diy",
                "decision": decisions[index % len(decisions)],
                "confidence": 0.8,
                "fad_label": "trend",
                "fad_probability": 0.25,
                "reason": f"{phrase} shows repeated complaints",
                "quotes": [
                    {
                        "text": "guard cracked in a week",
                        "url": urls[index % len(urls)],
                        "source_id": "arctic_shift",
                    }
                ],
                "enrich": ["ebay sold listings"],
            }
            for index, phrase in enumerate(phrases)
        ]
        return json.dumps({"verdicts": verdicts}), prompt_tokens, completion_tokens

    return send


def failing_sender(message: str = "503 provider on fire"):
    def send(_provider: ProviderSpec, _prompt: str) -> tuple[str, int, int]:
        raise RuntimeError(message)

    return send


def statuses(session: Session) -> dict[str, str]:
    return {
        str(row.phrase): str(row.status)
        for row in session.execute(select(Candidate)).scalars().all()
    }


# ---------------------------------------------------------------------------
# evidence assembly
# ---------------------------------------------------------------------------
def test_evidence_matches_a_phrase_in_the_title_or_the_body(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session)
        evidence = candidate_evidence(session, phrases=["circ saw", "saw blade"])
        assert evidence["circ saw"], "a phrase in a title must find its evidence"
        first = evidence["circ saw"][0]
        assert first["url"] == URL_A
        assert first["source"] == "arctic_shift"
        assert first["quote"], "the body text travels with the evidence"


def test_build_batch_carries_scores_and_urls(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        candidates = list(session.execute(select(Candidate).order_by(Candidate.id)).scalars())
        batch = build_batch(session, candidates[:2])
        assert [item.phrase for item in batch] == ["circ saw", "saw blade"]
        assert batch[0].mgs > 0
        assert "dv" in batch[0].sub_scores
        assert batch[0].urls, "the batch must expose the URLs grounding checks against"
        payload = batch[0].as_dict()
        assert payload["evidence"], "the Judge must receive the evidence, not just the phrase"


def test_pending_judgements_skips_the_already_judged(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        assert len(pending_judgements(session)) == 3
        run = session.execute(select(Run)).scalars().first()
        candidate = session.execute(select(Candidate).order_by(Candidate.id)).scalars().first()
        assert candidate is not None
        session.add(
            Judgement(
                candidate_id=int(candidate.id),
                run_id=run.id if run else None,
                gate="judge",
                decision="keep",
                confidence=0.5,
                quotes=[],
                enrich=[],
            )
        )
        session.flush()
        remaining = {str(candidate.phrase) for candidate in pending_judgements(session)}
        assert len(remaining) == 2, "a judged candidate must not be judged again"


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------
def test_judge_applies_keep_and_drop_to_candidate_status(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=stub_sender(verdict_payload(["circ saw", "saw blade", "larger mug"],
                                               decisions=["keep", "drop", "keep"])),
            now=NOW,
        )
        assert report.ok, report.reason
        assert report.judged == 3
        assert report.kept == 2
        assert report.dropped == 1
        assert statuses(session) == {
            "circ saw": "kept",
            "saw blade": "dropped",
            "larger mug": "kept",
        }
        rows = session.execute(select(Judgement)).scalars().all()
        assert len(rows) == 3
        assert all(row.gate == "judge" for row in rows)
        assert rows[0].quotes
        assert rows[0].quotes[0]["url"] in {URL_A, URL_B}
        assert rows[0].enrich == ["ebay sold listings", "amazon review volume"]
        assert rows[0].model, "the model that judged is recorded"
        assert rows[0].cache_key, "the cache key links the verdict to its cached answer"


def test_a_batch_is_one_provider_call(sessions: sessionmaker[Session]) -> None:
    """Spec §6.2 budgets the Judge at ~20 calls a night: batching is what makes that possible."""
    calls: list[str] = []

    def counting(provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        phrases = [
            phrase
            for phrase in ("circ saw", "saw blade", "larger mug")
            if f'"phrase": "{phrase}"' in prompt
        ]
        return verdict_payload(phrases), 100, 50

    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session, run_id=run.id, sender=counting, batch_size=DEFAULT_BATCH_SIZE, now=NOW
        )
    assert len(calls) == 1, "one batch of three candidates is one call"
    assert report.batches == 1
    assert report.judged == 3


def test_batching_splits_when_the_batch_size_is_smaller(sessions: sessionmaker[Session]) -> None:
    calls: list[int] = []

    def counting(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        phrases = [
            phrase
            for phrase in ("circ saw", "saw blade", "larger mug")
            if f'"phrase": "{phrase}"' in prompt
        ]
        calls.append(len(phrases))
        return verdict_payload(phrases), 10, 5

    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session, run_id=run.id, sender=counting, batch_size=2, now=NOW
        )
    assert calls == [2, 1], "two candidates then one: batching must respect the size"
    assert report.batches == 2
    assert report.judged == 3


# ---------------------------------------------------------------------------
# grounding, unknown phrases, missing verdicts
# ---------------------------------------------------------------------------
def test_an_invented_citation_is_stripped_and_the_verdict_is_marked(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=answering_sender(urls=(BROKEN,)),
            now=NOW,
        )
        assert report.ungrounded == 1
        assert report.removed_quotes == 1
        row = session.execute(select(Judgement)).scalars().one()
        assert row.quotes == []
        assert row.ungrounded is True
        # The verdict still stands: what cannot stand is presenting it as cited fact.
        assert row.decision == "keep"


def test_a_verdict_for_an_unknown_phrase_is_ignored(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=stub_sender(verdict_payload(["circ saw"], extra_phrase="circular saw blade")),
            now=NOW,
        )
        assert report.unknown_phrase == 1
        assert report.judged == 1
        assert len(session.execute(select(Judgement)).scalars().all()) == 1


def test_a_missing_verdict_is_counted_not_assumed(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session, phrases=("circ saw", "saw blade"))
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session, run_id=run.id, sender=stub_sender(verdict_payload(["circ saw"])), now=NOW
        )
        assert report.judged == 1
        assert report.missing_verdicts == ["saw blade"]
        # The unjudged candidate keeps its status rather than being dropped by omission.
        assert statuses(session)["saw blade"] == "active"


# ---------------------------------------------------------------------------
# degradation and caps
# ---------------------------------------------------------------------------
def test_a_failing_provider_changes_nothing(sessions: sessionmaker[Session]) -> None:
    """The property that keeps an outage from looking like a decision."""
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(session, run_id=run.id, sender=failing_sender(), now=NOW)
        assert report.status == "degraded"
        assert "dropping with reason" in report.reason
        assert all(status == "active" for status in statuses(session).values())
        assert session.execute(select(Judgement)).scalars().all() == []


def test_the_call_cap_is_terminal_and_visible(sessions: sessionmaker[Session]) -> None:
    calls: list[str] = []

    counting = answering_sender(prompt_tokens=1, completion_tokens=1)

    def counting_spy(provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        calls.append(provider.name)
        return counting(provider, prompt)

    limits = GateBudget(gate="judge", calls_per_day=1, tokens_per_day=10_000)
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session, run_id=run.id, sender=counting_spy, budget=limits, batch_size=1, now=NOW
        )
    assert report.status == "partial"
    assert "cap" in report.reason
    assert len(calls) == 1, "a cap is terminal, not an invitation to retry"


def test_the_token_cap_stops_the_gate(sessions: sessionmaker[Session]) -> None:
    limits = GateBudget(gate="judge", calls_per_day=50, tokens_per_day=150)
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=answering_sender(prompt_tokens=100, completion_tokens=60),
            budget=limits,
            batch_size=len(("circ saw", "saw blade", "larger mug")),
            now=NOW,
        )
    assert report.status == "partial"
    assert "token cap reached" in report.reason
    assert report.batches == 1


def test_dry_run_judges_nothing_and_writes_nothing(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=stub_sender(verdict_payload(["circ saw"])),
            now=NOW,
            dry_run=True,
        )
        assert report.batches == 1  # it walked the gate...
        assert report.judgements_written == 0
        assert session.execute(select(Judgement)).scalars().all() == []
        assert all(status == "active" for status in statuses(session).values())


# ---------------------------------------------------------------------------
# idempotency and the append-only guarantee
# ---------------------------------------------------------------------------
def test_a_second_run_judges_nothing_new(sessions: sessionmaker[Session]) -> None:
    """The unique key on (candidate, run, gate) is what makes a resume safe."""
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        payload = verdict_payload(["circ saw", "saw blade", "larger mug"])
        first = judge_candidates(session, run_id=run.id, sender=stub_sender(payload), now=NOW)
        assert first.judged == 3
        second = judge_candidates(session, run_id=run.id, sender=stub_sender(payload), now=NOW)
        assert second.judged == 0
        assert second.status == "empty"
        assert len(session.execute(select(Judgement)).scalars().all()) == 3


def test_a_later_run_records_a_new_judgement(sessions: sessionmaker[Session]) -> None:
    """A re-judged candidate is a new row, never an edit — that is what append-only means.

    The second run's *inputs changed* (fresh mentions), which is what makes it a different
    question. The first version of this test changed only the stub's answer and expected a
    different verdict — the cache correctly served run 1's answer instead, because an identical
    question must get an identical answer. That is the property, not the bug.
    """
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        judge_candidates(
            session, run_id=run.id, sender=stub_sender(verdict_payload(["circ saw", "saw blade",
                                                                        "larger mug"])),
            now=NOW,
        )
        later = Run(status="running", trigger="manual")
        session.add(later)
        session.flush()
        # The inputs move on between runs: candidates accumulate mentions.
        for candidate in session.execute(select(Candidate)).scalars():
            candidate.mentions += 1
        session.flush()
        # Handed over explicitly: this test is about the unique key and append-only history, and
        # queue selection for a second run has its own test below.
        every = list(session.execute(select(Candidate).order_by(Candidate.id)).scalars())
        report = judge_candidates(
            session,
            run_id=later.id,
            candidates=every,
            sender=stub_sender(
                verdict_payload(["circ saw", "saw blade", "larger mug"], decisions=["drop"])
            ),
            now=NOW,
        )
        assert report.judged == 3, report.as_dict()
        assert report.dropped == 3, report.as_dict()
        assert statuses(session)["circ saw"] == "dropped"  # the newest verdict wins on status
        rows = session.execute(select(Judgement)).scalars().all()
        assert len(rows) == 6  # both verdicts are kept
        newest = latest_judgements(session)
        circ_saw = session.execute(
            select(Candidate).where(Candidate.phrase == "circ saw")
        ).scalars().one()
        assert newest[int(circ_saw.id)].decision == "drop"


def test_an_unchanged_question_is_answered_from_the_cache(sessions: sessionmaker[Session]) -> None:
    """Identical work never reaches a provider twice — even a night later (spec §6.3)."""
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        every = list(session.execute(select(Candidate).order_by(Candidate.id)).scalars())
        first = judge_candidates(
            session, run_id=run.id, sender=answering_sender(), candidates=every, now=NOW
        )
        assert first.batches == 1
        later = Run(status="running", trigger="manual")
        session.add(later)
        session.flush()
        # Same candidates, same scores, same evidence: the same question.
        second = judge_candidates(
            session,
            run_id=later.id,
            candidates=every,
            sender=answering_sender(),
            now=NOW,
        )
        assert second.providers
        assert second.providers[0] == "cache", "an identical question must not reach a provider"
        assert second.prompt_tokens == 0
        assert second.completion_tokens == 0


def test_a_second_run_sees_the_candidates_again(sessions: sessionmaker[Session]) -> None:
    """The queue is per run: tonight's verdicts do not silence tomorrow night's gate."""
    with sessions() as session:
        seed(session)
        first = session.execute(select(Run)).scalars().first()
        assert first is not None
        judge_candidates(
            session,
            run_id=first.id,
            sender=stub_sender(verdict_payload(["circ saw", "saw blade", "larger mug"])),
            now=NOW,
        )
        later = Run(status="running", trigger="manual")
        session.add(later)
        session.flush()
        queue = pending_judgements(session, run_id=later.id)
        assert len(queue) == 3, "a new run must judge the candidates again"
        assert {str(candidate.status) for candidate in queue} == {"kept"}


def test_judgements_refuse_an_update(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        judge_candidates(session, run_id=run.id, sender=stub_sender(verdict_payload(["circ saw"])),
                         now=NOW)
        with pytest.raises(Exception, match=r"append-only"):
            session.execute(text("UPDATE judgements SET decision = 'drop'"))


def test_a_pruned_candidate_is_not_resurrected_by_a_verdict(
    sessions: sessionmaker[Session],
) -> None:
    """Mining's decisions and the Judge's verdicts have different authority."""
    with sessions() as session:
        seed(session, phrases=("circ saw",))
        candidate = session.execute(select(Candidate)).scalars().one()
        candidate.status = "briefed"
        session.flush()
        run = session.execute(select(Run)).scalars().first()
        assert run is not None
        report = judge_candidates(
            session,
            run_id=run.id,
            sender=stub_sender(verdict_payload(["circ saw"], decisions=["drop"])),
            candidates=[candidate],  # handed over explicitly: the queue skips 'briefed'
            now=NOW,
        )
        assert report.judged == 1  # the verdict is recorded...
        assert statuses(session)["circ saw"] == "briefed"  # ...but the status is not overwritten


def test_the_report_serializes_for_the_ledger(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed(session)
        run = session.execute(select(Run)).scalars().first()
        report = judge_candidates(
            session, run_id=run.id, sender=stub_sender(verdict_payload(["circ saw"])), now=NOW
        )
        payload = report.as_dict()
        assert json.dumps(payload)  # the run note can carry it verbatim
        assert payload["providers"], "the report must name where the verdicts came from"
        assert payload["providers"][0] in {"gemini", "cache"}
        assert report.summary().startswith("L3 judge:")
