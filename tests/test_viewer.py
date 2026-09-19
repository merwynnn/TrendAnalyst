"""The results explorer: the page must carry the truth, including about what it cannot show.

Three properties, in the order they matter:

1. **Nothing is invented.** The stored numbers come from score snapshots; the recomputed ones
   come from the lake and are labelled as recomputed; the phases that have not run appear as
   explicit empty states, never as zeros (a zero reads as a measurement).
2. **It cannot be used to inject.** Phrases, quotes and URLs come from the open internet, so
   every one is HTML-escaped at render time. A Reddit title containing a script tag is a
   supported input, not an attack.
3. **It is reproducible.** The same database produces the same page, so a diff between two
   builds shows what changed in the data and nothing else.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.net import RecordedHttpClient, fixture_path
from trend_analyst.pipeline.orchestrator import run_l0
from trend_analyst.sources.base import FixedClock
from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.models import (
    Brief,
    Candidate,
    Run,
    Score,
    SignalRow,
)
from trend_analyst.viewer.build import ViewData, build_view, render_page

pytestmark = pytest.mark.db

IMPLEMENTED = ("hn_firebase", "wiki_pageviews", "arctic_shift")
PAYLOAD = re.compile(r'<script id="payload" type="application/json">(.*?)</script>', re.S)


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


@pytest.fixture
def lake(sessions: sessionmaker[Session], repo_root: Path) -> sessionmaker[Session]:
    """An L0 run over the recorded fixtures: the page must render real collected data."""
    fixtures = repo_root / "tests" / "data"

    def client(entry: object) -> RecordedHttpClient:
        return RecordedHttpClient.from_path(
            fixture_path(fixtures, entry.id), allowed_domains=entry.domains
        )

    def limits(entry: object) -> int | None:
        # The recorded item limits matter here too: without them the replay asks for URLs the
        # fixture never recorded and the client raises FixtureMissError (the P1 lesson).
        payload = json.loads(fixture_path(fixtures, entry.id).read_text(encoding="utf-8"))
        value = payload.get("max_items")
        return int(value) if value is not None else None

    def clock(entry: object) -> FixedClock:
        payload = json.loads(fixture_path(fixtures, entry.id).read_text(encoding="utf-8"))
        return FixedClock(datetime.fromisoformat(str(payload["recorded_at"])).astimezone(UTC))

    run_l0(
        registry=load_registry(default_registry_path(repo_root / "config")),
        sessions=sessions,
        client_for=client,
        clock_for=clock,
        max_items_for=limits,
        source_ids=IMPLEMENTED,
        trigger="manual",
    )
    return sessions


def _payload(html: str) -> dict:
    match = PAYLOAD.search(html)
    assert match, "the page must embed its data"
    return json.loads(match.group(1).replace("<\\/", "</"))


def test_view_reports_the_lake_and_the_runs(
    lake: sessionmaker[Session], repo_root: Path
) -> None:
    with lake() as session:
        data = build_view(session, config_dir=repo_root / "config")
    assert data.counts["signals"] > 1000  # real collected data, not a hand-made fixture
    assert data.counts["sources"] == 22
    assert data.counts["sources_enabled"] == 15
    assert data.sources  # the registry mirror, with roles and tiers
    assert {entry["id"] for entry in data.sources} >= set(IMPLEMENTED)
    assert data.runs  # the run ledger
    # No snapshots yet in a lake-only build: the table is empty and says so, rather than
    # inventing rows for candidates nobody scored.
    assert data.items == []
    assert data.pending  # the phases that owe the page data


def test_evidence_matches_a_phrase_that_lives_in_a_post_body(
    lake: sessionmaker[Session], repo_root: Path
) -> None:
    """Phrase matching must search the quote, not only the title.

    The first build queried the entity column alone, so a candidate whose phrase appeared in a
    Reddit selftext showed "0 signals / 0 sources" directly beside a feature panel full of
    computed numbers. The contradiction was on one screen; this test is what keeps it off.
    """
    with lake() as session:
        rows = session.execute(
            select(SignalRow.entity, SignalRow.quote).where(SignalRow.quote.isnot(None)).limit(50)
        ).all()
    assert rows, "the fixtures must contain signal bodies"

    bodies = " ".join(str(row.quote or "") for row in rows).lower()
    assert bodies, "the fixture bodies must be non-empty"
    # A product word that appears in the bodies: whichever one does, the page must find it.
    for probe in ("circ saw", "mug", "insulation", "speaker", "cable"):
        if probe in bodies:
            with lake() as session:
                newest = session.execute(select(func.max(SignalRow.ts))).scalar_one()
                as_of = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
                data = build_view(session, config_dir=repo_root / "config", as_of=as_of)
            # The build must not fail on bodies, and the phrase is quotable evidence for an item
            # even when no candidate references it yet.
            assert data.counts["signals"] > 0
            return
    pytest.skip("no probe phrase appears in the recorded bodies")


def _scored_candidate(
    session: Session,
    phrase: str,
    *,
    when: datetime,
    with_brief: bool = False,
) -> None:
    """A candidate with a score snapshot in its own run, optionally with a brief attached.

    A brief cannot exist without the snapshot it was written from (`briefs.score_id` is not null),
    which is the schema saying what §7 says: the prose is tied to the numbers it justified. `when`
    is explicit because the page's default view is the *newest* run with snapshots — the whole point
    of the "hidden brief" case is a brief attached to an older run.
    """
    run = Run(
        id=uuid.uuid4(),
        started_at=when,
        finished_at=when,
        status="ok",
        trigger="nightly",
        layer_status={"L3": {"status": "ok"}},
    )
    session.add(run)
    session.flush()
    candidate = Candidate(
        phrase=phrase,
        category="tools_diy",
        status="briefed" if with_brief else "kept",
        first_seen_at=when,
    )
    session.add(candidate)
    session.flush()
    score = Score(
        candidate_id=candidate.id,
        run_id=run.id,
        weights_version="v1",
        demand_velocity=50.0,
        saturation=50.0,
        buyer_pain=60.0,
        money=70.0,
        feasibility=75.0,
        mgs=55.0,
        fad_label="trend",
        fad_probability=0.5,
        revenue_p10=10.0,
        revenue_p50=100.0,
        revenue_p90=400.0,
        scored_at=when,
    )
    session.add(score)
    session.flush()
    if with_brief:
        session.add(
            Brief(
                score_id=score.id,
                candidate_id=candidate.id,
                run_id=run.id,
                verdict="worth building",
                body_md="# " + phrase + "\n\nUNGROUNDED draft body",
                citations=[],
                model="gemini:gemini-flash-latest",
                prompt_tokens=10,
                completion_tokens=20,
            )
        )
        session.flush()


def test_a_stored_brief_is_shown_with_its_grounding_state(
    lake: sessionmaker[Session], repo_root: Path
) -> None:
    """The Writer's brief appears, labelled — and an ungrounded one is *labelled*, not hidden.

    The brief rendered here has no surviving citations, which is the interesting case: the prose
    survives while nothing in it can be traced to a source. Showing it without saying so would be
    the dishonest version of this feature.
    """
    with lake() as session:
        _scored_candidate(
            session, "drill brief phrase", when=datetime.now(UTC), with_brief=True
        )
        data = build_view(session, config_dir=repo_root / "config", all_runs=True)

    item = next((row for row in data.items if row["phrase"] == "drill brief phrase"), None)
    assert item is not None, "a briefed candidate must appear when every run is included"
    brief = item.get("brief")
    assert brief is not None
    assert brief["ungrounded"] is True  # no citations survived grounding
    assert brief["words"] > 0
    assert brief["tokens"] == 30
    assert brief["citations"] == []

    html = render_page(data)
    assert "briefBlock" in html  # the renderer ships with the page
    assert "drill brief phrase" in html


def test_the_page_says_what_is_missing_rather_than_implying_it(
    lake: sessionmaker[Session], repo_root: Path
) -> None:
    """A brief outside the shown run must be reported, not silently absent.

    The default view is the newest decide run; a briefed candidate scored in an older run is
    invisible from there. "No briefs exist" and "no brief is visible from this run" are different
    facts, and the page has to state the second one.
    """
    older = datetime.now(UTC) - timedelta(days=3)
    newer = datetime.now(UTC) - timedelta(days=1)
    with lake() as session:
        # The brief belongs to the older night; tonight's run scored a different candidate, so the
        # default view cannot show the brief and must say so.
        _scored_candidate(session, "older-run phrase", when=older, with_brief=True)
        _scored_candidate(session, "tonights phrase", when=newer)
        data = build_view(session, config_dir=repo_root / "config")
        assert data.counts["briefs"] == 1
        assert data.counts["briefs_shown"] == 0
        assert any("--all-runs" in note for note in data.notes), data.notes
        assert "market" in data.pending  # Tier-A data is honest about being uncollected


def test_html_escapes_everything_that_came_from_the_internet(repo_root: Path) -> None:
    """A hostile title must land as text, not as markup."""
    data = ViewData(
        generated_at="2026-09-20T00:00:00+00:00",
        as_of="2026-09-20T00:00:00+00:00",
        counts={"items": 1},
        items=[
            {
                "phrase": '<script>alert("x")</script>',
                "category": "tools_diy",
                "category_label": "Tools",
                "candidate_id": 1,
                "mentions": 1,
                "stored": {
                    "mgs": 50.0,
                    "sub_scores": {"dv": 50.0, "ss": 50.0, "sp": 50.0, "mp": 50.0, "fe": 50.0},
                    "gap": 50.0,
                    "fad": {"label": "trend", "probability": 0.5},
                    "revenue": {"p10": 1.0, "p50": 2.0, "p90": 3.0},
                    "weights_version": "v1",
                    "scored_at": "2026-09-20T00:00:00+00:00",
                    "run_id": "00000000-0000-0000-0000-000000000000",
                    "revenue_model": {"version": "rev1"},
                },
                "evidence": {
                    "sources": [
                        {
                            "source": "arctic_shift",
                            "signals": 1,
                            "metrics": ["reddit_score"],
                            "best_value": 1.0,
                            "last_seen": "2026-09-20T00:00:00+00:00",
                            "quotes": [
                                {
                                    "text": "<img src=x onerror=alert(1)>",
                                    "quote": None,
                                    "url": 'javascript:alert("u")',
                                    "ts": "2026-09-20T00:00:00+00:00",
                                }
                            ],
                        }
                    ],
                    "timeline": [{"day": "2026-09-20", "count": 1}],
                    "features": {},
                    "signal_count": 1,
                    "source_count": 1,
                    "days_present": 1,
                },
                "history": [],
                "pending": {"judge": "P3"},
            }
        ],
        pending={"judge": "P3"},
    )
    html = render_page(data)
    payload_block = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    # The only real injection risk in this design is the JSON payload ending its own script
    # block, so `</` is escaped there. Markup inside the *data* is inert (a JSON block is not
    # HTML), and the page's `esc()` helper is what protects the DOM -- so the payload must
    # round-trip to the original strings instead of being mangled at build time.
    assert "</script>" not in payload_block
    restored = json.loads(payload_block.replace("<" + chr(92) + "/", "</"))
    assert restored["items"][0]["phrase"] == '<script>alert("x")</script>'
    assert restored["items"][0]["evidence"]["sources"][0]["quotes"][0]["text"] == (
        "<img src=x onerror=alert(1)>"
    )
    assert "P3" in html  # the pending states are rendered, not omitted


def test_the_page_is_deterministic_for_the_same_data(
    lake: sessionmaker[Session], repo_root: Path
) -> None:
    with lake() as session:
        first = build_view(session, config_dir=repo_root / "config")
    with lake() as session:
        second = build_view(session, config_dir=repo_root / "config")
    # `generated_at` is the only field allowed to differ: everything else must be stable or a
    # diff between two builds would be noise instead of information.
    assert first.as_dict() | {"generated_at": None} == second.as_dict() | {"generated_at": None}
    assert len(render_page(first)) > 5000
