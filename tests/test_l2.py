"""L2 enrichment tests: what the layer pays for, what it refuses, and what it never fakes.

The eBay fixture here is **hand-written**, and labelled as such: no live eBay call has been made,
because the credentials are blank. It exists to exercise the parsing, the price spread and the
budget/ledger path — not to claim a real quote from a real marketplace. When a key lands, the plugin
is recorded the same way the Tier-S sources were (`scripts/record_fixtures.py`) and this fixture can
be replaced by a real recording.

The properties that matter:

* only `kept` candidates are enriched, and only the top-K of them;
* a missing credential is a **skipped** enrichment with a reason, never "zero listings";
* an exhausted budget stops the source without touching the other one;
* a parse that finds no listings is `empty`, which is different from `0`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trend_analyst.pipeline.layers.l2 import L2Report, enrich_candidates
from trend_analyst.sources.base import (
    FetchContext,
    HttpResponse,
    RawBatch,
    Signal,
    SourceBudget,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.sources.tier_a.ebay import EbayBrowsePlugin, MissingCredentialError
from trend_analyst.sources.tier_a.producthunt import ProductHuntPlugin
from trend_analyst.sources.tier_a.searchapi import SearchApiPlugin
from trend_analyst.store.models import Candidate, RawItem, Run, Score, SignalRow

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 20, tzinfo=UTC)
EBAY_SEARCH = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_TOKEN = "https://api.ebay.com/identity/v1/oauth2/token"


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


class FixedClock:
    """A clock that does not move: budgets and rate limits behave reproducibly in tests."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self._monotonic += max(seconds, 0.0)


class StubClient:
    """An HTTP client that answers from a dictionary of canned responses."""

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def _answer(self, url: str) -> HttpResponse:
        self.calls.append(url)
        status, text = self._responses.get(url, (200, "{}"))
        return HttpResponse(url=url, status_code=status, text=text, headers={})

    def get(self, url: str, *, params=None, headers=None) -> HttpResponse:
        return self._answer(url)

    def post(self, url: str, *, params=None, headers=None, content=None) -> HttpResponse:
        return self._answer(url)


#: A hand-written Browse response: two items with prices, and a `total` that says 137 compete.
EBAY_JSON = """
{
  "total": 137,
  "itemSummaries": [
    {"title": "Circular saw blade guard replacement", "price": {"value": "18.50", "currency":
    "USD"}},
    {"title": "Circular saw base plate square", "price": {"value": "41.00", "currency": "USD"}},
    {"title": "Circular saw (no price listed)"}
  ]
}
"""
TOKEN_JSON = '{"access_token": "app-token-for-tests", "expires_in": 7200}'

#: Hand-written Product Hunt response shape, labelled as such: the GraphQL endpoint
#: answers empty 400s from here (September 2026), so no recording exists. The shape
#: follows the public v2 schema (posts -> edges -> node); when a live recording lands,
#: this constant goes away. What it proves is parsing, grounding and budget behaviour —
#: not that Product Hunt serves these launches.
PH_TOKEN_URL = "https://api.producthunt.com/v2/oauth/token"
PH_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
PH_LAUNCHES_JSON = """
{
  "data": {"posts": {"edges": [
    {"node": {"id": "1", "name": "DeskPad Pro", "tagline": "A mat that charges",
     "votesCount": 512, "createdAt": "2026-09-20T08:00:00Z", "slug": "deskpad-pro"}},
    {"node": {"id": "2", "name": "Nameless", "tagline": "",
     "votesCount": 3, "createdAt": "2026-09-19T08:00:00Z", "slug": ""}},
    {"node": {"name": "", "votesCount": 9}}
  ]}}
}
"""


def seed_candidates(session: Session, *, status: str = "kept", count: int = 3) -> None:
    run = Run(status="running", trigger="manual")
    session.add(run)
    session.flush()
    for index, phrase in enumerate(("circ saw", "rebar cutter", "larger mug")[:count]):
        candidate = Candidate(phrase=phrase, category="tools_diy", status=status, mentions=2)
        session.add(candidate)
        session.flush()
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
                mgs=60.0 - index,
                fad_probability=0.3,
                fad_label="trend",
                revenue_p10=20.0,
                revenue_p50=150.0,
                revenue_p90=900.0,
            )
        )
    session.flush()


def budget_for(source_id: str = "ebay_browse", cap: int = 5000) -> SourceBudget:
    """A leash with a frozen clock.

    The sleep callable must be the CLOCK's: with a frozen `monotonic()` and a real `time.sleep`, the
    token bucket never sees time pass, so `acquire()` spins to its deadline and reports a rate limit
    that is an artefact of the test's clock rather than of the code under test.
    """
    clock = FixedClock()
    return SourceBudget(
        source_id=source_id,
        budget_per_day=cap,
        rps=1000.0,
        clock=clock,
        sleep=clock.sleep,
    )


def context_factory(client: StubClient, clock: FixedClock):
    def build(**kwargs) -> FetchContext:  # noqa: ANN003
        return FetchContext(**kwargs)

    return build


# ---------------------------------------------------------------------------
# the plugin
# ---------------------------------------------------------------------------
def test_the_plugin_reports_supply_and_the_price_spread() -> None:
    plugin = EbayBrowsePlugin()
    batch = RawBatch.from_parts(
        source_id="ebay_browse",
        cursor="circ saw",
        parts=[EBAY_JSON],
        status_codes=(200,),
        fetched_at=NOW,
        request_count=2,
    )
    signals = plugin.parse(batch)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.entity == "circ saw"
    assert signal.value == 137.0  # active listings: the saturation measurement
    assert signal.metadata["price_min"] == "18.50"
    assert signal.metadata["price_median"] == "29.75"
    assert signal.metadata["price_max"] == "41.00"
    assert signal.metadata["priced_listings"] == "2"
    assert signal.url, "a reader must be able to check the number"
    assert "circ+saw" in signal.url


def test_no_listings_is_an_empty_batch_not_a_zero() -> None:
    plugin = EbayBrowsePlugin()
    empty = RawBatch.from_parts(
        source_id="ebay_browse", cursor="nobody sells this", parts=['{"total": 0}'],
        fetched_at=NOW,
    )
    assert plugin.parse(empty) == []


def test_the_plugin_refuses_to_fetch_without_credentials() -> None:
    plugin = EbayBrowsePlugin()
    batch = RawBatch.from_parts(
        source_id="ebay_browse", cursor="circ saw", parts=[], fetched_at=NOW
    )
    del batch
    context = FetchContext(
        run_id="r",
        source_id="ebay_browse",
        client=StubClient({}),
        budget=budget_for(),
        clock=FixedClock(),
        cursor="circ saw",
    )
    with pytest.raises(MissingCredentialError):
        plugin.fetch(context)


def test_the_plugin_fetches_with_credentials() -> None:
    class Secrets:
        ebay_client_id = "id-for-tests"
        ebay_client_secret = "secret-for-tests"

    class Settings:
        tier_a = Secrets()

    client = StubClient({EBAY_TOKEN: (200, TOKEN_JSON), EBAY_SEARCH: (200, EBAY_JSON)})
    plugin = EbayBrowsePlugin(settings=Settings())
    context = FetchContext(
        run_id="r",
        source_id="ebay_browse",
        client=client,
        budget=budget_for(),
        clock=FixedClock(),
        cursor="circ saw",
    )
    batch = plugin.fetch(context)
    assert batch.cursor == "circ saw"
    assert batch.request_count >= 2, "the token call and at least one search"
    assert client.calls[0] == EBAY_TOKEN
    assert plugin.parse(batch)[0].value == 137.0


# ---------------------------------------------------------------------------
# the Product Hunt plugin (unvalidated live: GraphQL answers empty 400s from here)
# ---------------------------------------------------------------------------
def test_ph_plugin_reports_votes_per_launch() -> None:
    plugin = ProductHuntPlugin()
    batch = RawBatch.from_parts(
        source_id="producthunt_graphql",
        cursor="2026-09-21",
        parts=[PH_LAUNCHES_JSON],
        status_codes=(200,),
        fetched_at=NOW,
        request_count=2,
    )
    signals = plugin.parse(batch)
    assert len(signals) == 2
    assert signals[0].entity == "DeskPad Pro"
    assert signals[0].value == 512.0
    assert signals[0].metric == "ph_votes"
    assert signals[0].url == "https://www.producthunt.com/posts/deskpad-pro"
    assert signals[0].quote == "A mat that charges"
    assert signals[0].metadata["slug"] == "deskpad-pro"
    assert signals[1].entity == "Nameless"
    assert signals[1].url is None, "no slug, no URL — and the signal still says so"


def test_ph_plugin_refuses_to_fetch_without_credentials() -> None:
    plugin = ProductHuntPlugin()
    context = FetchContext(
        run_id="r",
        source_id="producthunt_graphql",
        client=StubClient({}),
        budget=budget_for("producthunt_graphql"),
        clock=FixedClock(),
        cursor="circ saw",
    )
    with pytest.raises(MissingCredentialError):
        plugin.fetch(context)


def test_ph_plugin_mints_a_token_then_queries() -> None:
    class Secrets:
        producthunt_client_id = "id-for-tests"
        producthunt_client_secret = "secret-for-tests"

    class Settings:
        tier_a = Secrets()

    client = StubClient({
        PH_TOKEN_URL: (200, TOKEN_JSON),
        PH_GRAPHQL_URL: (200, PH_LAUNCHES_JSON),
    })
    plugin = ProductHuntPlugin(settings=Settings())
    context = FetchContext(
        run_id="r",
        source_id="producthunt_graphql",
        client=client,
        budget=budget_for("producthunt_graphql"),
        clock=FixedClock(),
        cursor="circ saw",
    )
    batch = plugin.fetch(context)
    assert batch.request_count == 2, "the token call plus the launches query"
    assert client.calls[0] == PH_TOKEN_URL, "token first, cached for the run"
    again = plugin.fetch(
        FetchContext(
            run_id="r", source_id="producthunt_graphql", client=client,
            budget=budget_for("producthunt_graphql"), clock=FixedClock(), cursor="circ saw",
        )
    )
    assert again.request_count == 1, "a cached token costs no second dance"
    assert len(plugin.parse(batch)) == 2


# ---------------------------------------------------------------------------
# the SearchAPI plugin (hand-written shapes from a real probe; recording would
# commit the key, which travels in the URL — see the plugin docstring)
# ---------------------------------------------------------------------------
SEARCHAPI_JSON = """
{
  "inline_shopping": [
    {"title": "Mind Reader Anti-Fatigue Mat", "price": "$39.86", "extracted_price": 39.86,
     "seller": "Home Depot", "rating": 4.6, "reviews": 49},
    {"title": "Genuine Mats Standing Desk Mat", "price": "$41.00", "extracted_price": 41.0,
     "seller": "Amazon.com", "rating": 0, "reviews": 0},
    {"title": "Priceless Mat", "price": "free", "extracted_price": null, "seller": "X"}
  ],
  "related_searches": [{"query": "Best standing desk mat"}]
}
"""


def test_searchapi_reports_the_shelf_and_the_price_spread() -> None:
    plugin = SearchApiPlugin()
    batch = RawBatch.from_parts(
        source_id="searchapi",
        cursor="standing desk mat",
        parts=[SEARCHAPI_JSON],
        status_codes=(200,),
        fetched_at=NOW,
        request_count=1,
    )
    signals = plugin.parse(batch)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.entity == "standing desk mat"
    assert signal.metric == "search_shopping"
    assert signal.value == 3.0, "three listings competed"
    assert signal.metadata["price_min"] == "39.86"
    assert signal.metadata["price_median"] == "40.43"
    assert signal.metadata["price_max"] == "41.00"
    assert signal.metadata["priced_listings"] == "2"
    assert signal.metadata["avg_rating"] == "4.60"
    assert signal.metadata["sellers"] == "3"
    assert signal.url, "a reader must be able to check the shelf"
    assert "udm=28" in signal.url


def test_searchapi_empty_shelf_is_no_signal_not_a_zero() -> None:
    plugin = SearchApiPlugin()
    assert plugin.parse(RawBatch.from_parts(
        source_id="searchapi", cursor="nobody sells this",
        parts=['{"inline_shopping": []}'], fetched_at=NOW)) == []
    assert plugin.parse(RawBatch.from_parts(
        source_id="searchapi", cursor="no cursor",
        parts=['{"inline_shopping": [{"title": "T"}]}'], fetched_at=NOW)) == []


def test_searchapi_refuses_to_fetch_without_a_key() -> None:
    plugin = SearchApiPlugin()
    context = FetchContext(
        run_id="r",
        source_id="searchapi",
        client=StubClient({}),
        budget=budget_for("searchapi"),
        clock=FixedClock(),
        cursor="standing desk mat",
    )
    with pytest.raises(MissingCredentialError):
        plugin.fetch(context)


def test_searchapi_fetches_one_request_per_phrase() -> None:
    class Secrets:
        searchapi_key = "key-for-tests"

    class Settings:
        tier_a = Secrets()

    search_url = "https://www.searchapi.io/api/v1/search"
    client = StubClient({search_url: (200, SEARCHAPI_JSON)})
    plugin = SearchApiPlugin(settings=Settings())
    context = FetchContext(
        run_id="r",
        source_id="searchapi",
        client=client,
        budget=budget_for("searchapi"),
        clock=FixedClock(),
        cursor="standing desk mat",
    )
    batch = plugin.fetch(context)
    assert batch.cursor == "standing desk mat"
    assert batch.request_count == 1, "the five-a-day budget means one call per phrase"
    assert client.calls == [search_url], "one search request, nothing else"
    assert len(plugin.parse(batch)) == 1


def test_searchapi_needs_a_phrase_in_the_cursor() -> None:
    class Secrets:
        searchapi_key = "key-for-tests"

    class Settings:
        tier_a = Secrets()

    plugin = SearchApiPlugin(settings=Settings())
    context = FetchContext(
        run_id="r",
        source_id="searchapi",
        client=StubClient({}),
        budget=budget_for("searchapi"),
        clock=FixedClock(),
        cursor="",
    )
    with pytest.raises(SourceSkippedError, match="no enrichment phrase"):
        plugin.fetch(context)


# ---------------------------------------------------------------------------
# the layer
# ---------------------------------------------------------------------------
def test_only_kept_candidates_are_enriched(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_candidates(session, status="dropped")
        report = enrich_candidates(
            session, run_id=None, plugins={"ebay_browse": _StubPlugin()}, top_k=5
        )
        assert report.status == "empty"
        assert report.candidates == 0


def test_the_layer_enriches_the_top_k_only(sessions: sessionmaker[Session]) -> None:
    client = StubClient({EBAY_SEARCH: (200, EBAY_JSON)})
    with sessions() as session:
        seed_candidates(session)
        run = session.execute(select(Run)).scalars().first()
        plugin = _StubPlugin()
        report = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": plugin},
            top_k=2,
            budgets={"ebay_browse": budget_for()},
            client_for=lambda _source: client,
            clock=FixedClock(),
            context_factory=context_factory(client, FixedClock()),
        )
        assert report.candidates == 2
        assert [outcome.phrase for outcome in report.outcomes] == ["circ saw", "rebar cutter"]
        assert report.enriched == 2
        assert report.signals_written == 2
        stored = session.execute(select(SignalRow)).scalars().all()
        assert {str(row.entity) for row in stored} == {"circ saw", "rebar cutter"}
        assert session.execute(select(RawItem)).scalars().all()


def test_a_second_pass_stores_no_duplicates(sessions: sessionmaker[Session]) -> None:
    """Single-shot: calling twice re-fetches, but the signal unique key keeps the
    lake at one row — a re-run is cheap, not free."""
    client = StubClient({EBAY_SEARCH: (200, EBAY_JSON)})
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        plugin = _StubPlugin()
        kwargs = {
            "plugins": {"ebay_browse": plugin},
            "top_k": 1,
            "budgets": {"ebay_browse": budget_for()},
            "client_for": lambda _source: client,
            "clock": FixedClock(),
            "context_factory": context_factory(client, FixedClock()),
        }
        first = enrich_candidates(session, run_id=run.id, **kwargs)  # type: ignore[arg-type]
        assert first.enriched == 1
        second = enrich_candidates(session, run_id=run.id, **kwargs)  # type: ignore[arg-type]
        assert second.enriched == 1, "the pass runs again — nothing is skipped as done"
        assert len(session.execute(select(SignalRow)).scalars().all()) == 1, (
            "but the lake holds no duplicate"
        )


def test_a_missing_credential_is_a_skip_with_a_reason(sessions: sessionmaker[Session]) -> None:
    class Secrets:
        ebay_client_id = ""
        ebay_client_secret = ""

    class Settings:
        tier_a = Secrets()

    client = StubClient({})
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        report = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": EbayBrowsePlugin(settings=Settings())},
            top_k=1,
            budgets={"ebay_browse": budget_for()},
            client_for=lambda _source: client,
            clock=FixedClock(),
            context_factory=context_factory(client, FixedClock()),
        )
        outcome = report.outcomes[0]
        assert outcome.status == "skipped"
        assert "not set" in outcome.reason.lower(), "the reason must say what is missing"
        assert outcome.signals == 0
        assert session.execute(select(SignalRow)).scalars().all() == []


def test_an_exhausted_budget_skips_the_source_and_names_it(
    sessions: sessionmaker[Session],
) -> None:
    client = StubClient({EBAY_SEARCH: (200, EBAY_JSON)})
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        spent = budget_for(cap=1)
        spent.charge(1)
        assert spent.spent_today == 1
        second = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": _StubPlugin()},
            top_k=1,
            budgets={"ebay_browse": spent},
            client_for=lambda _source: client,
            clock=FixedClock(),
            context_factory=context_factory(client, FixedClock()),
        )
        assert second.skipped_sources["ebay_browse"].startswith("daily budget exhausted")


def test_a_failing_source_does_not_stop_the_layer(sessions: sessionmaker[Session]) -> None:
    client = StubClient({EBAY_SEARCH: (200, EBAY_JSON)})
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        report = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": _FailingPlugin()},
            top_k=1,
            budgets={"ebay_browse": budget_for()},
            client_for=lambda _source: client,
            clock=FixedClock(),
            context_factory=context_factory(client, FixedClock()),
        )
        assert report.outcomes[0].status == "failed"
        assert "upstream exploded" in report.outcomes[0].reason
        assert report.status == "ok", "a Tier-A failure is a gap, not a crash"


def test_a_source_with_no_budget_is_refused(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        report = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": _StubPlugin()},
            top_k=1,
            budgets={},
        )
        assert (
            report.skipped_sources["ebay_browse"] == "no budget configured: the leash is required"
        )
        assert report.outcomes == []


def test_a_source_with_no_plugin_is_named(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        report = enrich_candidates(
            session, run_id=run.id, plugins={}, source_ids=("ebay_browse", "bestbuy_trending"),
            top_k=1, budgets={"bestbuy_trending": budget_for("bestbuy_trending")},
        )
        assert set(report.skipped_sources) == {"ebay_browse", "bestbuy_trending"}
        assert "no plugin in this build" in report.skipped_sources["ebay_browse"]


def test_dry_run_writes_nothing(sessions: sessionmaker[Session]) -> None:
    client = StubClient({EBAY_SEARCH: (200, EBAY_JSON)})
    with sessions() as session:
        seed_candidates(session, count=1)
        run = session.execute(select(Run)).scalars().first()
        report = enrich_candidates(
            session,
            run_id=run.id,
            plugins={"ebay_browse": _StubPlugin()},
            top_k=1,
            budgets={"ebay_browse": budget_for()},
            client_for=lambda _source: client,
            clock=FixedClock(),
            context_factory=context_factory(client, FixedClock()),
            dry_run=True,
        )
        assert report.signals_written == 0
        assert session.execute(select(SignalRow)).scalars().all() == []


def test_the_report_serializes(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_candidates(session, count=1)
        report = L2Report(candidates=1)
        assert report.summary().startswith("L2 enrich:")
        assert report.as_dict()["candidates"] == 1


class _StubPlugin(SourcePlugin):
    """A Tier-A plugin with no network: it answers with the hand-written Browse shape."""

    id = "ebay_browse"
    tier = "A"
    layers = ("L2",)
    schedule = "on_demand"
    budget_per_day = 5000
    rps = 1000.0
    domains = ("api.ebay.com",)

    def fetch(self, ctx: FetchContext) -> RawBatch:
        return RawBatch.from_parts(
            source_id=self.id,
            cursor=ctx.cursor,
            parts=[EBAY_JSON],
            status_codes=(200,),
            fetched_at=NOW,
            request_count=1,
        )

    def parse(self, raw: RawBatch) -> list[Signal]:
        return EbayBrowsePlugin().parse(raw)


class _FailingPlugin(_StubPlugin):
    """A plugin whose fetch raises: the failure path, not the HTTP-status path."""

    def fetch(self, ctx: FetchContext) -> RawBatch:
        raise RuntimeError("upstream exploded")
