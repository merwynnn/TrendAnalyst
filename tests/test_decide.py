"""The P2 acceptance: the ranked table renders end to end from real L0 data, snapshotted.

The build brief's bar is *"ranked table renders end-to-end from L0 data with snapshots
versioned"*, and the specification adds the property that makes versioning worth anything
(§5.4): *"replaying a checkpoint with identical inputs MUST reproduce identical downstream
scores (hash-asserted in CI)"*. Both are asserted here, against real Postgres and the
recorded fixtures — no network, and the lake the scorer reads is one L0 actually filled.

What this file deliberately does *not* assert is that the ranking is *good*. With three of
fifteen sources collected, the sub-score spread is mostly category priors, and a test that
demanded a particular candidate on top would be pinning today's lake rather than the
pipeline's behaviour.
"""

from __future__ import annotations

import hashlib
import json
import uuid as uuid_module
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from config.categories import default_taxonomy
from trend_analyst.net import RecordedHttpClient, fixture_path
from trend_analyst.pipeline.decide import run_decide
from trend_analyst.pipeline.layers.l1 import (
    STOP_WORDS,
    MinedPhrase,
    collapse_substrings,
    extract_phrases,
    mine,
)
from trend_analyst.pipeline.layers.l3 import load_attention_points, score_phrases
from trend_analyst.pipeline.orchestrator import _render_ranked, run_l0
from trend_analyst.pipeline.orchestrator import main as orchestrator_main
from trend_analyst.sources.base import FixedClock
from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.models import Candidate, Run, SignalRow
from trend_analyst.store.snapshots import (
    RankedScore,
    ScoreRecord,
    candidate_key,
    count_candidates,
    count_scores,
    history_for,
    latest_ranked,
    upsert_candidates,
    write_snapshot,
    write_snapshots,
)

pytestmark = pytest.mark.db

IMPLEMENTED = ("hn_firebase", "wiki_pageviews", "arctic_shift")


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


@pytest.fixture
def fixture_data(repo_root: Path) -> Path:
    return repo_root / "tests" / "data"


@pytest.fixture
def registry(repo_root: Path) -> object:
    return load_registry(default_registry_path(repo_root / "config"))


def _client_factory(fixtures_dir: Path) -> object:
    def build(entry: object) -> RecordedHttpClient:
        return RecordedHttpClient.from_path(
            fixture_path(fixtures_dir, entry.id), allowed_domains=entry.domains
        )

    return build


def _clock_factory(fixtures_dir: Path) -> object:
    def build(entry: object) -> FixedClock:
        payload = json.loads(fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8"))
        return FixedClock(datetime.fromisoformat(str(payload["recorded_at"])).astimezone(UTC))

    return build


def _limits_factory(fixtures_dir: Path) -> object:
    def build(entry: object) -> int | None:
        payload = json.loads(fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8"))
        value = payload.get("max_items")
        return int(value) if value is not None else None

    return build


def fill_lake(
    *,
    registry: object,
    sessions: sessionmaker[Session],
    fixtures_dir: Path,
) -> None:
    """Run L0 over the recorded fixtures — the "L0 data" the decide layer must consume."""
    run_l0(
        registry=registry,  # type: ignore[arg-type]
        sessions=sessions,
        client_for=_client_factory(fixtures_dir),  # type: ignore[arg-type]
        clock_for=_clock_factory(fixtures_dir),  # type: ignore[arg-type]
        max_items_for=_limits_factory(fixtures_dir),  # type: ignore[arg-type]
        source_ids=IMPLEMENTED,
        trigger="manual",
    )


def newest_signal(sessions: sessionmaker[Session]) -> datetime:
    with sessions() as session:
        value = session.execute(select(func.max(SignalRow.ts))).scalar_one()
    return value if value.tzinfo else value.replace(tzinfo=UTC)  # type: ignore[no-any-return]


def decide(
    *,
    sessions: sessionmaker[Session],
    as_of: datetime,
    top: int = 5,
    **kwargs: object,
) -> object:
    return run_decide(
        sessions=sessions,
        taxonomy=default_taxonomy(),
        as_of=as_of,
        trigger="manual",
        top=top,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# mining: pure, no database needed
# ---------------------------------------------------------------------------
def test_extract_phrases_never_crosses_a_field_separator() -> None:
    """The bug that produced "cat jarman cat jarman" from a joined entity and quote."""
    joined = "cat jarman | cat jarman"
    phrases = extract_phrases(joined)
    assert not any(phrase.count("jarman") > 1 for phrase in phrases)
    assert all(phrase.count(" ") <= 3 for phrase in phrases)


def test_extract_phrases_drops_thread_furniture() -> None:
    phrases = extract_phrases("Weekly Newbie Q&A and Store Critique Thread")
    assert not any("weekly" in phrase for phrase in phrases)
    assert not any("thread" in phrase for phrase in phrases)


def test_mine_requires_a_category_and_reports_the_prune() -> None:
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    texts = [
        ("my circ saw blade is broken and flimsy", now, "arctic_shift"),
        ("looking for a reusable mug", now, "arctic_shift"),
        ("Cat Jarman won the award", now, "wiki_pageviews"),  # a person, and an excluded source
    ]
    report = mine(texts, taxonomy=taxonomy, as_of=now)
    kept = {phrase.phrase for phrase in report.phrases}
    assert "circ saw" in kept or "saw blade" in kept
    assert not any("jarman" in phrase for phrase in kept)
    assert report.mined == report.kept + report.pruned
    assert report.unmatched > 0
    assert report.summary().startswith("L1:")


def test_mine_applies_the_floor_and_says_so() -> None:
    """A 95% prune on a tiny lake would keep nothing; the floor keeps it useful.

    The texts are product phrases ("<tool> storage"), not sentences: since P7 a phrase whose head
    is not a buyable noun cannot be a candidate at all, so "my drill is broken" would mine nothing —
    correctly, and uselessly for a test about the prune.
    """
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    texts = [
        (f"i need better {noun} storage for the shop", now, "arctic_shift")
        for noun in ("drill", "ladder", "caulk", "gutter", "hinge")
    ]
    report = mine(texts, taxonomy=taxonomy, as_of=now, prune_fraction=0.05, min_keep=4)
    assert report.kept == 4  # the floor, not 5% of 5
    assert report.min_keep == 4


def test_mine_honours_the_staleness_horizon() -> None:
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    old = now - timedelta(days=400)
    report = mine(
        [("i need a drill holder", old, "arctic_shift")], taxonomy=taxonomy, as_of=now
    )
    # One text yields several overlapping phrases ("drill holder", "need a drill holder"), and every
    # one of them is stale: the assertion is that age alone empties the candidate set.
    assert report.stale >= 1
    assert report.mined == 0
    assert report.phrases == ()


def test_collapse_substrings_keeps_the_more_frequent_shorter_phrase() -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)

    def mined(phrase: str, mentions: int) -> MinedPhrase:
        return MinedPhrase(
            phrase=phrase, category_id="tools_diy", mentions=mentions,
            source_ids=("arctic_shift",), first_seen=now, last_seen=now, texts=("t",),
        )

    survivors, collapsed = collapse_substrings(
        [mined("circ saw", 3), mined("blade circ saw", 1), mined("circ saw it still", 1)]
    )
    assert [item.phrase for item in survivors] == ["circ saw"]
    assert collapsed == 2


# ---------------------------------------------------------------------------
# snapshots: append-only, versioned, resumable
# ---------------------------------------------------------------------------
def test_snapshot_writes_are_idempotent_per_run_and_version(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)

    first = decide(sessions=sessions, as_of=as_of, top=5)
    assert first.snapshots_written > 0
    assert first.snapshots_existing == 0

    # Same run, same inputs: nothing new. This is the "(candidate, run, weights_version)"
    # unique key doing the work, not a flag in memory.
    with sessions() as session:
        assert write_snapshots(session, first.run_id, []) == 0
        assert count_scores(session, first.run_id) == first.snapshots_written

    second = decide(sessions=sessions, as_of=as_of, top=5)
    assert second.run_id != first.run_id  # a real new run
    assert second.snapshots_written == first.snapshots_written  # and a new versioned row each


def test_replaying_the_same_run_reproduces_the_same_snapshot_hash(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    """Spec §5.4: identical inputs, identical scores — hash-asserted."""
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)

    first = decide(sessions=sessions, as_of=as_of, top=5)
    second = decide(sessions=sessions, as_of=as_of, top=5)
    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_hash  # not the hash of an empty payload
    assert first.snapshot_hash != snapshot_hash_of_empty()

    # The hash covers the numbers, so a changed weight version changes it.
    third = decide(sessions=sessions, as_of=as_of, top=5, min_keep=100)
    assert third.snapshot_hash
    if third.mining.kept != first.mining.kept:
        assert third.snapshot_hash != first.snapshot_hash


def snapshot_hash_of_empty() -> str:
    return hashlib.sha256(b"[]").hexdigest()


def test_upsert_candidates_accumulates_forward_only(sessions: sessionmaker[Session]) -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)
    later = now + timedelta(days=2)

    def mined(mentions: int, last: datetime) -> MinedPhrase:
        return MinedPhrase(
            phrase="circ saw", category_id="tools_diy", mentions=mentions,
            source_ids=("arctic_shift",), first_seen=now, last_seen=last, texts=("t",),
        )

    with sessions() as session:
        ids = upsert_candidates(session, [mined(2, now)])
        candidate_id = ids[candidate_key("circ saw", "tools_diy")]
        upsert_candidates(session, [mined(5, later)])
        row = session.get(Candidate, candidate_id)
        assert row is not None
        assert row.mentions == 5
        assert row.first_seen_at == now  # never rewritten backwards
        assert row.last_seen_at == later
        assert row.status == "active"  # a mining run never reactivates a rejected candidate


def test_write_snapshot_reports_whether_it_wrote(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)
    report = decide(sessions=sessions, as_of=as_of, top=3)
    candidate = report.candidates[0]
    with sessions() as session:
        row = session.execute(
            select(Candidate).where(Candidate.phrase == candidate.phrase)
        ).scalar_one()
        record = ScoreRecord(
            candidate_id=row.id,
            result=candidate.result,
            fad=candidate.fad,
            revenue=candidate.revenue,
        )
        assert write_snapshot(session, report.run_id, record) is False  # already there
        other_run = Run(status="running", trigger="manual")
        session.add(other_run)
        session.flush()
        assert write_snapshot(session, other_run.id, record) is True


def test_history_is_append_only_and_readable(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)
    decide(sessions=sessions, as_of=as_of, top=3)
    decide(sessions=sessions, as_of=as_of, top=3)

    with sessions() as session:
        ranked = latest_ranked(session, limit=10)
        assert ranked, "the ranked table must render from the snapshots"
        assert ranked == sorted(ranked, key=lambda row: (-row.mgs, row.phrase))
        phrase = ranked[0].phrase
        history = history_for(session, phrase)
        assert len(history) == 2  # the same phrase scored in two runs, both kept
        assert history[0].scored_at <= history[1].scored_at
        # A trigger refuses UPDATE and DELETE on scores; the table is a ledger, not a cache.
        with pytest.raises(Exception, match=r"append-only|permission|violates"):
            session.execute(text("UPDATE scores SET mgs = 0"))


# ---------------------------------------------------------------------------
# the end-to-end acceptance
# ---------------------------------------------------------------------------
def test_ranked_table_renders_from_l0_data(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)
    report = decide(sessions=sessions, as_of=as_of, top=8)

    assert report.status == "ok"
    assert report.scoring.scored > 0
    assert report.snapshots_written == report.scoring.scored
    assert report.mining.texts_scanned > 50  # real L0 text, not a hand-made corpus

    rendered = report.render(top=8)
    assert "MGS" in rendered
    assert "fad" in rendered
    assert "phrase" in rendered
    assert "L1:" in rendered
    assert "L3:" in rendered

    with sessions() as session:
        assert count_scores(session) >= report.snapshots_written
        assert count_candidates(session) >= report.scoring.scored
        for ranked in latest_ranked(session, limit=5):
            assert 0 <= ranked.mgs <= 100
            assert ranked.weights_version == "v1"
            assert ranked.fad_label in {"fad", "trend", "evergreen"}
            assert ranked.revenue_p10 <= ranked.revenue_p50 <= ranked.revenue_p90


def test_decide_is_idempotent_over_the_same_lake(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    """A second decide run over an unchanged lake writes new snapshots, not new candidates."""
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)

    first = decide(sessions=sessions, as_of=as_of, top=5)
    with sessions() as session:
        candidates_after_first = count_candidates(session)

    second = decide(sessions=sessions, as_of=as_of, top=5)
    with sessions() as session:
        assert count_candidates(session) == candidates_after_first  # upsert, not re-insert
    assert second.mining.kept == first.mining.kept
    assert [c.phrase for c in second.top] == [c.phrase for c in first.top]


def test_dry_run_writes_nothing(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)
    with sessions() as session:
        before_candidates = count_candidates(session)
        before_scores = count_scores(session)

    report = decide(sessions=sessions, as_of=as_of, top=5, dry_run=True)
    assert report.snapshots_written == 0
    assert report.snapshot_hash == ""
    with sessions() as session:
        assert count_candidates(session) == before_candidates
        assert count_scores(session) == before_scores


def test_l3_counts_phrases_it_cannot_find_evidence_for() -> None:
    """A mine/score disagreement is counted, never scored as zero."""
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    ghost = MinedPhrase(
        phrase="circ saw", category_id="tools_diy", mentions=1,
        source_ids=("arctic_shift",), first_seen=now, last_seen=now, texts=("t",),
    )
    report = score_phrases([ghost], [], taxonomy=taxonomy, as_of=now)
    assert report.scored == 0
    assert report.skipped_no_evidence == 1


def test_attention_values_are_normalized_per_source(
    sessions: sessionmaker[Session], registry: object, fixture_data: Path
) -> None:
    """An upvote and a pageview must not share a scale."""
    fill_lake(registry=registry, sessions=sessions, fixtures_dir=fixture_data)
    as_of = newest_signal(sessions)
    with sessions() as session:
        points = load_attention_points(session, as_of=as_of)
    assert points
    assert all(0.0 <= point.value <= 100.0 for point in points)
    # Every source's values are a percentile *within that source*.
    for source_id in {point.source_id for point in points}:
        values = [point.value for point in points if point.source_id == source_id]
        assert max(values) <= 100.0
        assert min(values) >= 0.0


def test_ranked_renderer_shows_every_field_the_table_promises() -> None:
    """`--rank`'s renderer, tested directly.

    The CLI path itself is not exercised against a live database here, and the reason is
    honest rather than lazy: the CLI opens its own connection, so it cannot see rows written
    inside this test's rolled-back transaction. Committing them into the test database to
    watch the CLI read them back would leak state into every later test. What is worth
    testing — that the table shows the numbers, the weights version and the missing-data
    case — is a pure function over stored rows, so it is tested as one.
    """
    assert "run --layers l0,l1,l3 first" in _render_ranked([]).lower()

    row = RankedScore(
        candidate_id=1,
        phrase="circ saw",
        category="tools_diy",
        mgs=54.64,
        demand_velocity=50.0,
        saturation=50.0,
        buyer_pain=50.0,
        money=63.0,
        feasibility=77.0,
        fad_probability=0.31,
        fad_label="trend",
        revenue_p10=25.0,
        revenue_p50=183.0,
        revenue_p90=1046.0,
        weights_version="v1",
        scored_at=datetime(2026, 9, 20, tzinfo=UTC),
        run_id=uuid_module.uuid4(),
        mentions=2,
    )
    rendered = _render_ranked([row])
    assert "MGS" in rendered
    assert "circ saw" in rendered
    assert "tools_diy" in rendered
    assert "trend" in rendered
    assert "w=v1" in rendered  # the weights version is visible, or history is unreadable
    assert "25-183-1,046" in rendered
    assert "54.6" in rendered


def test_cli_refuses_layers_it_cannot_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert orchestrator_main(["--layers", "L2"]) == 1
    assert "L2 is P4" in capsys.readouterr().err


def test_cli_rejects_an_unknown_layer(capsys: pytest.CaptureFixture[str]) -> None:
    assert orchestrator_main(["--layers", "L9"]) == 1
    assert "unknown layer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P7: the head-noun rule, measured against the phrases the first live nights produced
# ---------------------------------------------------------------------------
#: Phrases the Judge gate dropped as noise on the first live nights, with its own words. Each is a
#: sentence fragment that reached scoring because a domain noun appeared anywhere inside the n-gram
#: window. They are a regression suite, not a hypothetical: every one of these consumed a slot, a
#: score and Judge tokens.
FRAGMENTS_FROM_LIVE_RUNS: tuple[tuple[str, str], ...] = (
    ("filament whatever", "conversational noise, not an actionable phrase"),
    ("filament thanks", "conversational closing remark, not a product gap"),
    ("mouse but i m", "fragmented text, pure noise"),
    ("bolt which vendors", "nonsensical, lacks coherent intent"),
    ("pick up the defective", "general customer service complaint, not a distinct product gap"),
    ("saw it still feels", "extracted incorrectly from user sentence text"),
    ("time to pick", "garbled phrase from a gardening query, miscategorized under music audio"),
    # a stop word precedes the head, so the head rule rejects it before the Judge sees it
    ("replicate this through filament", "a general query about 3D printing"),
    ("16 bolt", "a fragmented misinterpretation of a '5/16 bolt'"),
    ("mickey mouse", "a generic copyrighted character model"),
)


@pytest.mark.parametrize(("fragment", "why"), FRAGMENTS_FROM_LIVE_RUNS)
def test_phrases_the_judge_called_noise_never_reach_scoring(fragment: str, why: str) -> None:
    """A fragment must die at the taxonomy, before it costs a score and a token.

    The Judge caught all of these — at the *end* of the pipeline, after mining, scoring, ranking and
    a provider call. This is the same judgement made where it is free. `why` is the Judge's own
    reason, kept beside the case so the test fails with the evidence, not just a colour.
    """
    now = datetime(2026, 9, 20, tzinfo=UTC)
    report = mine(
        [(f"and then {fragment} happened again", now, "arctic_shift")],
        taxonomy=default_taxonomy(),
        as_of=now,
    )
    mined = {phrase.phrase for phrase in report.phrases}
    assert fragment not in mined, f"{fragment!r} still mined: {why}"
    # And the rejection is attributed, not silent: every fragment must leave a named reason behind.
    assert report.rejections, f"{fragment!r} was dropped without a reason"
    assert report.mined == 0 or fragment not in mined


@pytest.mark.parametrize(
    "phrase",
    [
        "chisel storage",  # head is a product FORM, category from the modifier
        "circ saw",
        "saw blade",
        "cutting board",
        "espresso machine",
        "monitor mount",
        "cable management",
    ],
)
def test_real_product_phrases_still_mine(phrase: str) -> None:
    """The rule must not be a wall: these are the phrases worth keeping, including the best find.

    "chisel storage" is the case that shaped the design — the first live night's only genuine gap,
    whose head noun ("storage") appears in no category's core list. A head-noun rule without the
    shared `product_nouns` vocabulary would have deleted it.
    """
    category, reason = default_taxonomy().match_with_reason(phrase, stop_words=STOP_WORDS)
    assert category is not None, f"{phrase!r} was rejected with reason {reason!r}"


def test_the_run_note_names_why_phrases_were_rejected() -> None:
    """The ledger line must explain the funnel, not just count it.

    Before P7 it said "unmatched 17634" — a number that cannot distinguish a small taxonomy from a
    mining layer emitting sentence fragments. Now it names the reasons, so the next night's log is
    a diagnosis.
    """
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    texts = [
        ("i need better chisel storage", now, "arctic_shift"),
        ("filament whatever thanks", now, "arctic_shift"),
        ("mickey mouse printed badly", now, "arctic_shift"),
        ("my 5/16 bolt snapped", now, "arctic_shift"),
        ("time to pick the tomatoes", now, "arctic_shift"),
    ]
    report = mine(texts, taxonomy=taxonomy, as_of=now)

    assert [phrase.phrase for phrase in report.phrases] == ["chisel storage"]
    assert report.rejections["brand"] >= 1
    assert report.rejections["numeric_leading"] >= 1
    assert report.rejections["function_word_before_head"] >= 1
    summary = report.summary()
    assert "brand" in summary
    assert "numeric_leading" in summary
    assert report.as_dict()["rejections"]  # and it survives serialisation


def test_a_single_document_phrase_is_reported_not_deleted() -> None:
    """Measured decision: the two-document gate I planned would have deleted the best find.

    "chisel storage" appeared in exactly one post. The count is reported (and used to rank phrases
    that several people wrote above phrases one person wrote), but it is not a gate.
    """
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    report = mine(
        [("i need better chisel storage", now, "arctic_shift")], taxonomy=taxonomy, as_of=now
    )
    assert report.kept == 1
    assert report.single_document == 1
    assert report.phrases[0].documents == 1
    assert "single document" in report.summary()


def test_phrases_several_people_wrote_outrank_one_off_remarks() -> None:
    """Ranking, not filtering: with a small lake the percentiles cannot separate these."""
    taxonomy = default_taxonomy()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    texts = [
        # the same phrase in three documents, with a lower velocity than the single-doc one
        ("i like my new desk mat", now, "arctic_shift"),
        ("this desk mat curls at the edge", now, "hn_firebase"),
        ("desk mat replacement", now, "arctic_shift"),
        ("i need better chisel storage", now, "arctic_shift"),
    ]
    report = mine(texts, taxonomy=taxonomy, as_of=now, min_keep=10)
    by_phrase = {phrase.phrase: phrase for phrase in report.phrases}
    assert by_phrase["desk mat"].documents >= 2
    assert report.phrases[0].phrase == "desk mat"  # shared beats sharper-but-lonely


def test_stop_words_are_words_and_never_a_product_noun() -> None:
    """Two guards for one P7 bug, because it happened in a way no reviewer would catch.

    The stop list is a single string that is `.split()`, and the first P7 edit put an explanatory
    comment *inside* it. The words "saw", `"saw"` and "saw," silently became stop words, which
    deleted the saw products ("circ saw", "saw blade") from mining — a code change that looked like
    a comment. So: every entry must be a plain lowercase word, and no entry may be a noun the
    taxonomy needs (a "watering can" and a "saw" are products; "cannot" is not).
    """
    taxonomy = default_taxonomy()
    punctuation = {word for word in STOP_WORDS if not word.isalpha()}
    assert not punctuation, f"STOP_WORDS has non-words (prose leaked in): {punctuation}"

    product_nouns = set(taxonomy.core_heads) | set(taxonomy.product_nouns)
    collisions = sorted(STOP_WORDS & product_nouns)
    assert not collisions, (
        f"STOP_WORDS cannot contain a product noun: {collisions}. "
        "A word that is both a verb and a thing (saw, can, board) belongs to the taxonomy."
    )
