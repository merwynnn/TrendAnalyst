"""Niches first: keep every idea with its reason, judge pain, measure passion, show it.

The pipeline's goal is a profitable niche — a product solving a painful problem in a
passionate market — and the dashboard ranks niches, not fragments. These tests pin the
mechanics: L1 returns everything mined with per-idea drop reasons, the pain gate
assesses niches in one cached call (or skips loudly without a transport), passion is
engagement depth normalized 0-100, and the dashboard renders every stored idea with
its evidence and its why.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.dashboard import build_dashboard, niche_score
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.categories import default_taxonomy
from trend_analyst.llm.extract import heuristic_sender
from trend_analyst.llm.pain import NicheEvidence, assess_niches, pain_prompt
from trend_analyst.pipeline.decide import run_decide
from trend_analyst.pipeline.layers.l1 import rank_products
from trend_analyst.scoring.features import SignalPoint
from trend_analyst.scoring.passion import category_passion
from trend_analyst.store.models import Judgement, Run, SignalRow
from trend_analyst.store.snapshots import latest_ranked

NOW = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.fixture
def sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A session factory inside one transaction, rolled back after the test."""
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def _points(
    phrases: list[str], days: int = 10
) -> tuple[list[tuple[str, str, list[int]]], dict[int, SignalPoint]]:
    """One point id per (phrase, day): velocity spreads the shortlist, volume does not."""
    resolved: list[tuple[str, str, list[int]]] = []
    by_id: dict[int, SignalPoint] = {}
    point_id = 0
    for index, phrase in enumerate(phrases):
        ids: list[int] = []
        # Later phrases are hotter: more recent days, so the prune has something to rank.
        for day in range(days - index % days):
            point_id += 1
            ids.append(point_id)
            by_id[point_id] = SignalPoint(
                ts=NOW - timedelta(days=day),
                value=1.0,
                text=f"{phrase} is great",
                source_id="arctic_shift",
                metric="comments",
            )
        resolved.append((phrase, "tools_diy", ids))
    return resolved, by_id


def test_rank_returns_everything_with_a_reason() -> None:
    """The shortlist prioritizes; nothing mined is thrown away silently."""
    phrases = [f"widget {index}" for index in range(20)]
    resolved, by_id = _points(phrases)
    ranked = rank_products(resolved, by_id, as_of=NOW, prune_fraction=0.05, min_keep=4)
    assert len(ranked.kept) == 4  # the floor still prioritizes the judge's reading order
    assert len(ranked.all) == 20  # but everything mined survives, with flags set
    kept_phrases = {item.phrase for item in ranked.kept}
    flagged_phrases = {item.phrase for item in ranked.all if item.kept}
    assert kept_phrases == flagged_phrases
    dropped = [item for item in ranked.all if not item.kept]
    assert len(dropped) == 16
    for item in dropped:
        assert item.drop_reason, f"{item.phrase} missed the shortlist without a reason"
        assert "rank" in item.drop_reason
        assert "percentile" in item.drop_reason
    for item in ranked.kept:
        assert item.drop_reason == ""
    # The evidence map covers every phrase, so scoring never re-matches text.
    assert set(ranked.match_points) == set(phrases)


def test_stale_phrases_are_named_not_counted() -> None:
    """A product gone quiet is dropped with its name attached, not just a counter."""
    resolved, by_id = _points(["fresh widget"])
    stale_id = 999
    by_id[stale_id] = SignalPoint(
        ts=NOW - timedelta(days=400), value=1.0, text="old widget broke",
        source_id="arctic_shift", metric="comments",
    )
    resolved.append(("old widget", "tools_diy", [stale_id]))
    ranked = rank_products(resolved, by_id, as_of=NOW, min_keep=10)
    assert ranked.stale == 1
    assert ("old widget", "tools_diy") in ranked.stale_phrases
    assert all(item.phrase != "old widget" for item in ranked.all)


def test_pain_prompt_bounds_evidence() -> None:
    """Free-tier sized: bounded phrases and texts per niche, cacheable payload."""
    evidence = [
        NicheEvidence(
            category="tools_diy",
            products=tuple((f"widget {index}", index, 50.0) for index in range(30)),
            sample_texts=tuple(f"people say thing {index} " * 20 for index in range(30)),
        )
    ]
    prompt, payload = pain_prompt(evidence)
    assert payload["niches"][0]["category"] == "tools_diy"
    assert len(payload["niches"][0]["products"]) <= 8
    assert len(payload["niches"][0]["texts"]) <= 8
    assert all(len(text) <= 300 for text in payload["niches"][0]["texts"])
    assert "painful" in prompt.lower()


def test_pain_skipped_without_transport_names_it(sessions: sessionmaker[Session]) -> None:
    """No sender, no assessment — and no zero that would read as 'no pain'."""
    with sessions() as session:
        report = assess_niches(
            session,
            [NicheEvidence(category="tools_diy", products=(("widget", 3, 55.0),))],
            sender=None,
            now=NOW,
        )
    assert report.status == "skipped"
    assert report.assessments == {}


@pytest.mark.db
def test_pain_assesses_niches_in_one_call(sessions: sessionmaker[Session]) -> None:
    """One cached provider call scores every niche; unknown categories never apply."""
    payload = json.dumps(
        {
            "niches": [
                {
                    "category": "tools_diy",
                    "pain_score": 72.0,
                    "painful_problem": "drill holes cost renters their deposits",
                    "rationale": "three products work around drilling bans",
                    "representative_phrases": ["no-drill shelf"],
                },
                {"category": "invented", "pain_score": 99.0},
            ]
        }
    )

    def send(_provider: object, _prompt: str) -> tuple[str, int, int]:
        return payload, 120, 60

    with sessions() as session:
        report = assess_niches(
            session,
            [NicheEvidence(category="tools_diy", products=(("no-drill shelf", 5, 61.0),))],
            sender=send,  # type: ignore[arg-type]
            now=NOW,
        )
    assert report.status == "ok"
    assert set(report.assessments) == {"tools_diy"}  # invented dropped, counted
    assert report.unknown_categories == 1
    item = report.assessments["tools_diy"]
    assert item.pain_score == 72.0
    assert "deposits" in item.painful_problem


@pytest.mark.db
def test_passion_is_engagement_not_audience(sessions: sessionmaker[Session]) -> None:
    """Pageviews do not move passion; comments and replies do."""
    with sessions() as session:
        session.add_all(
            [
                SignalRow(
                    source_id="wiki_pageviews", entity="Cat Jarman", metric="pageviews",
                    value=100_000.0, ts=NOW, category="books",
                ),
                SignalRow(
                    source_id="hn_firebase", entity="no-drill shelf", metric="comments",
                    value=40.0, ts=NOW, category="tools_diy",
                    metadata_json={"comments": 40},
                ),
                SignalRow(
                    source_id="arctic_shift", entity="no-drill shelf", metric="upvotes",
                    value=500.0, ts=NOW, category="tools_diy",
                    metadata_json={"num_comments": 120},
                ),
            ]
        )
        session.flush()
        passion = category_passion(session, as_of=NOW, window_days=90)
    assert passion["tools_diy"] == 100.0  # the talking niche normalizes to the top
    assert passion["books"] == 0.0  # a hundred thousand silent views is not passion


def test_niche_score_weights_are_stated() -> None:
    """0.4 pain + 0.3 passion + 0.3 momentum — and a missing assessment never reads as zero pain."""
    assert niche_score(pain=100.0, passion=100.0, top_mgs=[100.0]) == 100.0
    assert niche_score(pain=0.0, passion=0.0, top_mgs=[0.0]) == 0.0
    full = niche_score(pain=80.0, passion=60.0, top_mgs=[70.0, 60.0])
    assert full == pytest.approx(0.4 * 80 + 0.3 * 60 + 0.3 * 65)
    # Unassessed: passion + momentum rescaled, pain contributes nothing, not zero.
    partial = niche_score(pain=None, passion=60.0, top_mgs=[70.0, 60.0])
    assert partial == pytest.approx((0.3 * 60 + 0.3 * 65) / 0.6)
    assert niche_score(pain=None, passion=0.0, top_mgs=[]) == 0.0


@pytest.mark.db
def test_dashboard_renders_every_idea_with_its_why(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The page lists ALL scored ideas under their niches, with pain, reasons and cuts."""
    with sessions() as session:
        session.add(
            SignalRow(
                source_id="hn_firebase", entity="no-drill shelf", metric="comments",
                value=12.0, ts=NOW, category="tools_diy", quote="landlord keeps my deposit",
                url="https://example.com/t/1", metadata_json={"comments": 12},
            )
        )
        session.flush()
    report = run_decide(
        sessions=sessions,
        taxonomy=default_taxonomy(),
        as_of=NOW,
        trigger="manual",
        sender=heuristic_sender(),
        top=5,
    )
    assert report.scoring.scored > 0
    with sessions() as session:
        ideas = latest_ranked(session, limit=100)
        phrase = ideas[0].phrase
        run_row = Run(
            status="ok",
            trigger="nightly",
            started_at=NOW,
            finished_at=NOW,
            layer_status={
                "L1": {
                    "dropped": [
                        {
                            "phrase": "also-ran widget",
                            "category": "tools_diy",
                            "reason": "below the velocity shortlist (rank 9 of 9)",
                        }
                    ]
                },
                "pain": {
                    "status": "ok",
                    "niches": {
                        "tools_diy": {
                            "pain_score": 72.0,
                            "painful_problem": "drill holes cost renters deposits",
                            "rationale": "workarounds everywhere",
                            "representative_phrases": [phrase],
                        }
                    },
                },
            },
            notes="test run",
        )
        session.add(run_row)
        session.flush()
        session.add(
            Judgement(
                candidate_id=ideas[0].candidate_id,
                run_id=run_row.id,
                gate="judge",
                decision="keep",
                confidence=0.9,
                reason="renters keep asking for it",
                quotes=[{"text": "landlord keeps my deposit", "url": "https://example.com/t/1"}],
            )
        )
        # A newer stage row with no results (judge/writer rows carry none): the
        # dashboard must still explain the latest run that decided something.
        session.add(
            Run(
                status="ok",
                trigger="nightly",
                started_at=NOW + timedelta(hours=1),
                finished_at=NOW + timedelta(hours=1),
                layer_status={},
                notes="later stage, no results",
            )
        )
        session.commit()
    target = build_dashboard(sessions, out_dir=tmp_path, as_of=NOW)
    page = target.read_text(encoding="utf-8")
    assert target.name == "index.html"
    assert phrase in page  # every scored idea is listed…
    assert "72" in page  # …with the pain score…
    assert "deposits" in page  # …and the problem line…
    assert "also-ran widget" in page  # …and the cut list…
    assert "shortlist" in page
    assert "niche score" in page
    assert "<details>" in page  # expandable rows, no tabs
    assert 'href="winners.html"' in page  # nav to the winners page
    assert 'href="movers.html"' in page  # nav to the movers page
    winners = (tmp_path / "winners.html").read_text(encoding="utf-8")
    assert phrase in winners  # the kept idea is a winner…
    assert "renters keep asking" in winners  # …with the judge's reason…
    assert "judge: keep" in winners
    movers = (tmp_path / "movers.html").read_text(encoding="utf-8")
    assert phrase in movers  # one scored run so far: everything is new…
    assert "First scored run" in movers
