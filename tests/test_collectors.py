"""Collector tests — three Tier-S plugins, replayed from recorded fixtures.

No test here touches the network: every request goes through a `RecordedHttpClient` built
from `tests/data/http/<source>.json`, which was captured once from the live APIs by
`scripts/record_fixtures.py`.

The fixtures cover two passes per source: a cold start (no watermark) and the incremental
run that follows it. That is deliberate — the watermark path is most of P1's value, and a
fixture that only covers the cold start would leave it untested.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trend_analyst.net import (
    HttpPolicy,
    RecordedHttpClient,
    check_egress,
    fixture_path,
    request_url,
)
from trend_analyst.sources.base import (
    FetchContext,
    HttpResponse,
    PluginContractError,
    RawBatch,
    Signal,
    SourceBudget,
    SourcePlugin,
    SourceSkippedError,
    load_plugin,
)
from trend_analyst.sources.registry import SourceEntry, default_registry_path, load_registry
from trend_analyst.sources.tier_s.arctic_shift import PAIN_SUBS, ArcticShiftPlugin
from trend_analyst.sources.tier_s.hn import API as HN_API
from trend_analyst.sources.tier_s.hn import HackerNewsPlugin
from trend_analyst.sources.tier_s.wiki import WikipediaPageviewsPlugin, days_to_fetch

NOW = datetime(2026, 5, 20, 6, 0, tzinfo=UTC)
IMPLEMENTED = ("hn_firebase", "wiki_pageviews", "arctic_shift")


class FrozenClock:
    """A clock that only moves when a test's sleeper moves it."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._monotonic = 1_000.0

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def registry(repo_root: Path) -> Any:
    return load_registry(default_registry_path(repo_root / "config"))


def recorded_at(fixtures_dir: Path, source_id: str) -> datetime:
    """When a fixture was recorded. It becomes the test clock: the plugins derive dates
    (Wikipedia days, Arctic Shift's `after=`) from `clock.now()`, so a test that pretends
    it is a different day would request URLs the fixture does not contain — and rightly
    fail. Reading the stamp keeps the fixture self-describing instead of hard-coding a
    date a future re-recording would invalidate."""
    payload = json.loads(fixture_path(fixtures_dir, source_id).read_text(encoding="utf-8"))
    return datetime.fromisoformat(str(payload["recorded_at"])).astimezone(UTC)


def fixture_max_items(fixtures_dir: Path, source_id: str) -> int | None:
    """The item limit the fixture was recorded with.

    Replaying with a different limit would request URLs the fixture does not contain —
    the plugin derived its request from `ctx.max_items`, so the replay must use the same
    one. Stamping it keeps that honest without re-recording.
    """
    payload = json.loads(fixture_path(fixtures_dir, source_id).read_text(encoding="utf-8"))
    value = payload.get("max_items")
    return int(value) if value is not None else None


def make_context(
    entry: SourceEntry,
    fixtures_dir: Path,
    *,
    cursor: str | None = None,
    budget_per_day: int | None = None,
    max_items: int | None = None,
    client: Any | None = None,
    now: datetime | None = None,
) -> tuple[FetchContext, RecordedHttpClient, SourceBudget]:
    """A context wired to a fixture, with a clock that moves only when the budget sleeps.

    The sleeper advances the clock, which is what a real sleep does. A sleeper that did
    nothing would make the pace limiter spin, which is why SourceBudget.acquire() is
    bounded as well.
    """
    clock = FrozenClock(now or recorded_at(fixtures_dir, entry.id))
    if max_items is None:
        max_items = fixture_max_items(fixtures_dir, entry.id)
    recorded = client or RecordedHttpClient.from_path(
        fixture_path(fixtures_dir, entry.id), allowed_domains=entry.domains
    )
    budget = SourceBudget(
        source_id=entry.id,
        budget_per_day=budget_per_day or entry.budget_per_day,
        rps=entry.rps,
        clock=clock,
        sleep=clock.advance,
    )
    return (
        FetchContext(
            run_id="run-test",
            source_id=entry.id,
            client=recorded,
            budget=budget,
            clock=clock,
            cursor=cursor,
            max_items=max_items,
        ),
        recorded,
        budget,
    )


def metric_names(signals: list[Signal]) -> set[str]:
    return {signal.metric for signal in signals}


# ---------------------------------------------------------------------------
# The contract, against the real registry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source_id", IMPLEMENTED)
def test_implemented_plugins_satisfy_their_registry_entry(
    registry: Any, source_id: str
) -> None:
    """load_plugin() imports, validates against the registry and instantiates."""
    plugin = load_plugin(registry.by_id(source_id))

    assert isinstance(plugin, SourcePlugin)
    assert plugin.id == source_id
    assert plugin.tier == "S"
    assert plugin.layers == ("L0",)


def test_stubbed_sources_still_fail_loudly(registry: Any) -> None:
    """A source without a plugin is an error the developer sees, not an empty fetch."""
    with pytest.raises(PluginContractError, match="defines no SourcePlugin"):
        load_plugin(registry.by_id("gdelt_doc"))


# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------
def test_hn_first_pass_fetches_the_front_page_and_stories(
    registry: Any, fixtures_dir: Path
) -> None:
    entry = registry.by_id("hn_firebase")
    ctx, client, _budget = make_context(entry, fixtures_dir)

    batch = HackerNewsPlugin().fetch(ctx)
    signals = HackerNewsPlugin().parse(batch)

    assert batch.not_modified is False
    assert batch.item_count == 12, "the recorded cold start fetched 12 stories"
    assert batch.request_count == 13, "1 front page + 12 stories"
    assert set(batch.status_codes) == {200}
    assert batch.cursor is not None
    assert batch.cursor.split(",")[0].isdigit(), "the cursor is the front-page id list"
    assert len(client.requests) == 13

    assert len(signals) == 12
    assert metric_names(signals) == {"hn_score"}
    for signal in signals:
        assert signal.source_id == "hn_firebase"
        assert signal.entity.strip()
        assert signal.url is not None
        assert signal.url.startswith("https://news.ycombinator.com/item?id=")
        assert signal.quote == signal.entity
        assert signal.ts.tzinfo is not None
        assert signal.value >= 0
        assert signal.metadata["hn_id"].isdigit()


def test_hn_second_pass_is_not_modified(registry: Any, fixtures_dir: Path) -> None:
    """Same front page, same watermark: one request, nothing parsed (spec §5.1)."""
    entry = registry.by_id("hn_firebase")
    first_ctx, _first_client, _budget = make_context(entry, fixtures_dir)
    first = HackerNewsPlugin().fetch(first_ctx)

    second_ctx, second_client, _budget = make_context(entry, fixtures_dir, cursor=first.cursor)
    second = HackerNewsPlugin().fetch(second_ctx)

    assert second.not_modified is True
    assert second.parts == ()
    assert second.request_count == 1
    assert len(second_client.requests) == 1, "only the front page is fetched, no stories"
    assert HackerNewsPlugin().parse(second) == []


def test_hn_cursor_is_the_front_page_id_list(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("hn_firebase")
    ctx, client, _budget = make_context(entry, fixtures_dir)
    batch = HackerNewsPlugin().fetch(ctx)

    front_page = json.loads(
        RecordedHttpClient.from_path(
            fixture_path(fixtures_dir, entry.id), allowed_domains=entry.domains
        ).get(f"{HN_API}/topstories.json").text
    )
    assert batch.cursor == ",".join(str(item) for item in front_page)
    assert client.requests[0] == f"{HN_API}/topstories.json"


def test_hn_partial_batch_when_the_budget_runs_out(registry: Any, fixtures_dir: Path) -> None:
    """A budget refusal must degrade, not crash, and must not fake an empty answer."""
    entry = registry.by_id("hn_firebase")
    ctx, _client, budget = make_context(entry, fixtures_dir, budget_per_day=1)

    batch = HackerNewsPlugin().fetch(ctx)

    assert batch.parts == ()
    assert batch.not_modified is False, "a budget stop is not 'nothing changed'"
    assert batch.request_count == 1
    assert budget.spent_today == 1


def test_hn_skips_when_the_budget_is_already_spent(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("hn_firebase")
    ctx, _client, budget = make_context(entry, fixtures_dir, budget_per_day=1)
    budget.try_acquire()  # spend the only request

    with pytest.raises(SourceSkippedError, match="budget refused"):
        HackerNewsPlugin().fetch(ctx)


def test_hn_429_degrades_and_starts_backoff(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("hn_firebase")

    class Always429:
        source_id = "hn_firebase"

        def get(self, url: str, *, params: Any = None, headers: Any = None) -> HttpResponse:
            return HttpResponse(
                url=url, status_code=429, text="slow down", headers={"Retry-After": "60"}
            )

    ctx, _client, budget = make_context(entry, fixtures_dir, client=Always429())

    with pytest.raises(SourceSkippedError, match="HTTP 429"):
        HackerNewsPlugin().fetch(ctx)

    assert budget.rate_limit_hits == 1
    assert budget.backoff_remaining_s() > 0, "the 429 must start backoff (spec §4.3)"


def test_hn_ignores_non_stories_and_junk() -> None:
    plugin = HackerNewsPlugin()
    parts = [
        '{"type":"job","id":1,"title":"We are hiring","time":1,"score":1}',
        '{"type":"story","id":2,"title":"","time":1,"score":1}',
        '{"type":"story","id":3,"title":"Real story","time":1,"score":7,"descendants":2}',
        "not json at all",
        "null",
    ]
    batch = RawBatch.from_parts(source_id="hn_firebase", parts=parts, fetched_at=NOW)
    signals = plugin.parse(batch)

    assert [signal.entity for signal in signals] == ["Real story"]
    assert signals[0].value == 7.0
    assert signals[0].metadata["comments"] == "2"


# ---------------------------------------------------------------------------
# Wikipedia pageviews
# ---------------------------------------------------------------------------
def test_wiki_first_pass_collects_the_missing_days(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("wiki_pageviews")
    ctx, client, _budget = make_context(entry, fixtures_dir)

    batch = WikipediaPageviewsPlugin().fetch(ctx)
    signals = WikipediaPageviewsPlugin().parse(batch)

    assert batch.item_count == 3, "the cold start reaches back three complete days"
    assert batch.request_count == 3
    assert set(batch.status_codes) == {200}

    assert len(signals) == 2990
    assert metric_names(signals) == {"wiki_pageviews_top"}
    assert all(signal.ts.tzinfo is not None for signal in signals)
    assert all(signal.ts.hour == 0 and signal.ts.minute == 0 for signal in signals)
    assert all(signal.url is not None for signal in signals)
    assert all(str(signal.url).startswith("https://en.wikipedia.org/wiki/") for signal in signals)
    assert len(client.requests) == 3


def test_wiki_second_pass_makes_no_requests(registry: Any, fixtures_dir: Path) -> None:
    """Every complete day is already collected: the watermark says so, so nothing is asked."""
    entry = registry.by_id("wiki_pageviews")
    first_ctx, _client, _budget = make_context(entry, fixtures_dir)
    first = WikipediaPageviewsPlugin().fetch(first_ctx)

    second_ctx, second_client, _budget = make_context(entry, fixtures_dir, cursor=first.cursor)
    second = WikipediaPageviewsPlugin().fetch(second_ctx)

    assert second.not_modified is True
    assert second.parts == ()
    assert second_client.requests == [], "no request at all when there is no new day"


def test_wiki_days_to_fetch_walks_forward_from_the_watermark() -> None:
    now = datetime(2026, 5, 20, 6, 0, tzinfo=UTC)

    cold = days_to_fetch(cursor=None, now=now)
    assert [day.date().isoformat() for day in cold] == ["2026-05-17", "2026-05-18", "2026-05-19"]

    assert days_to_fetch(cursor="2026-05-19", now=now) == []
    assert days_to_fetch(cursor="2026-05-17", now=now) == [
        datetime(2026, 5, 18, tzinfo=UTC),
        datetime(2026, 5, 19, tzinfo=UTC),
    ]
    # A long gap is caught up 3 days at a time, oldest first — never by jumping to the
    # newest days, which would silently skip the middle of the range.
    assert [day.date().isoformat() for day in days_to_fetch(cursor="2026-04-01", now=now)] == [
        "2026-04-02",
        "2026-04-03",
        "2026-04-04",
    ]


def test_wiki_unreadable_cursor_is_treated_as_a_cold_start() -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    assert len(days_to_fetch(cursor="not-a-date", now=now)) == 3


def test_wiki_skips_pages_that_say_nothing_about_demand() -> None:
    payload = json.dumps(
        {
            "items": [
                {
                    "project": "en.wikipedia",
                    "access": "all-access",
                    "year": 2026,
                    "month": 5,
                    "day": 19,
                    "articles": [
                        {"article": "Main_Page", "views": 10, "rank": 1},
                        {"article": "Real_Topic", "views": 5, "rank": 2},
                        {"article": "", "views": 3, "rank": 3},
                    ],
                }
            ]
        }
    )
    batch = RawBatch.from_parts(source_id="wiki_pageviews", parts=[payload], fetched_at=NOW)
    signals = WikipediaPageviewsPlugin().parse(batch)

    assert [(signal.entity, signal.value) for signal in signals] == [("Real Topic", 5.0)]
    assert signals[0].ts == datetime(2026, 5, 19, tzinfo=UTC)


def test_wiki_skips_a_day_the_api_has_not_published(registry: Any, fixtures_dir: Path) -> None:
    """A 404 for an unfinished day is normal: the source continues with the others."""
    entry = registry.by_id("wiki_pageviews")

    class MixedStatuses:
        source_id = "wiki_pageviews"

        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, *, params: Any = None, headers: Any = None) -> HttpResponse:
            self.calls += 1
            if self.calls == 1:
                return HttpResponse(url=url, status_code=404, text="not found")
            return HttpResponse(url=url, status_code=200, text='{"items": []}')

    client = MixedStatuses()
    ctx, _client, _budget = make_context(entry, fixtures_dir, client=client)

    batch = WikipediaPageviewsPlugin().fetch(ctx)

    assert batch.item_count == 2, "the 404'd day is skipped, the rest are kept"
    assert 404 in batch.status_codes
    assert batch.cursor is not None


# ---------------------------------------------------------------------------
# Arctic Shift
# ---------------------------------------------------------------------------
def test_arctic_first_pass_sweeps_every_sub(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("arctic_shift")
    ctx, client, _budget = make_context(entry, fixtures_dir)

    batch = ArcticShiftPlugin().fetch(ctx)
    signals = ArcticShiftPlugin().parse(batch)

    assert batch.item_count == len(PAIN_SUBS)
    assert batch.request_count == len(PAIN_SUBS)
    assert len(client.requests) == len(PAIN_SUBS)

    assert len(signals) == 71
    assert metric_names(signals) == {"reddit_score"}
    for signal in signals:
        assert signal.url is not None
        assert signal.url.startswith("https://www.reddit.com/r/")
        assert signal.metadata["subreddit"]
        assert signal.quote
        assert signal.ts.tzinfo is not None


def test_arctic_second_pass_asks_only_for_what_is_new(registry: Any, fixtures_dir: Path) -> None:
    """The watermark becomes the API's `after=`: the incremental run is a different query."""
    entry = registry.by_id("arctic_shift")
    first_ctx, _first_client, _budget = make_context(entry, fixtures_dir)
    first = ArcticShiftPlugin().fetch(first_ctx)

    second_ctx, second_client, _budget = make_context(entry, fixtures_dir, cursor=first.cursor)
    second = ArcticShiftPlugin().fetch(second_ctx)

    assert first.cursor is not None
    assert len(first.cursor) == 10
    assert len(second_client.requests) == len(PAIN_SUBS)
    assert all(f"after={first.cursor}" in url for url in second_client.requests)
    assert second.item_count == len(PAIN_SUBS)
    assert ArcticShiftPlugin().parse(second), "the incremental pass still returns posts"


def test_arctic_request_shape(registry: Any, fixtures_dir: Path) -> None:
    """The API's parameters are what the plugin says they are — checked without a fixture,
    because this is about the request the plugin builds, not about a recorded answer."""
    entry = registry.by_id("arctic_shift")
    seen: list[str] = []

    class Recorder:
        source_id = "arctic_shift"

        def get(self, url: str, *, params: Any = None, headers: Any = None) -> HttpResponse:
            target = request_url(url, params)
            seen.append(target)
            return HttpResponse(url=target, status_code=200, text='{"data": []}')

    ctx, _client, _budget = make_context(entry, fixtures_dir, max_items=7, client=Recorder())
    ArcticShiftPlugin().fetch(ctx)

    first = seen[0]
    assert first.startswith("https://arctic-shift.photon-reddit.com/api/posts/search?")
    assert "limit=7" in first, "the caller's cap is honoured"
    assert "sort=desc" in first
    assert f"subreddit={PAIN_SUBS[0]}" in first
    assert "after=" in first, "the watermark becomes the API's after="
    assert len(seen) == len(PAIN_SUBS), "one request per subreddit, and no more"


def test_arctic_tolerates_a_bare_list_response() -> None:
    """The API has returned both {'data': [...]} and a bare list; parsing accepts both."""
    payload = json.dumps([{"title": "I wish this existed", "score": 3, "created_utc": 1789827491}])
    batch = RawBatch.from_parts(source_id="arctic_shift", parts=[payload], fetched_at=NOW)
    signals = ArcticShiftPlugin().parse(batch)

    assert len(signals) == 1
    assert signals[0].entity == "I wish this existed"
    assert signals[0].url is None, "no permalink, no URL — and the signal still says so"


def test_arctic_partial_sweep_when_the_budget_runs_out(registry: Any, fixtures_dir: Path) -> None:
    entry = registry.by_id("arctic_shift")
    ctx, client, budget = make_context(entry, fixtures_dir, budget_per_day=2)

    batch = ArcticShiftPlugin().fetch(ctx)

    assert batch.request_count == 2
    assert len(client.requests) == 2
    assert budget.spent_today == 2


# ---------------------------------------------------------------------------
# Fixture hygiene
# ---------------------------------------------------------------------------
def test_every_fixture_is_valid_and_stamped(fixtures_dir: Path) -> None:
    files = sorted((fixtures_dir / "http").glob("*.json"))
    assert files, "no fixtures recorded"

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["recorded_at"].startswith("20"), f"{path.name} has no recording stamp"
        assert payload["source_id"] == path.stem
        assert payload["responses"], f"{path.name} recorded nothing"
        assert payload.get("passes"), f"{path.name} has no pass journal"


def test_fixtures_carry_no_credentials(fixtures_dir: Path) -> None:
    """Only content-type and retry-after are kept, so a session cookie cannot be committed."""
    for path in sorted((fixtures_dir / "http").glob("*.json")):
        raw = path.read_text(encoding="utf-8").lower()
        for forbidden in ("set-cookie", "authorization", "api_key", "bearer ", "session="):
            assert forbidden not in raw, f"{path.name} mentions {forbidden!r}"


def test_fixtures_match_enabled_l0_sources(registry: Any, fixtures_dir: Path) -> None:
    for path in sorted((fixtures_dir / "http").glob("*.json")):
        entry = registry.by_id(path.stem)
        assert entry.enabled, f"{entry.id} is disabled but has a fixture"
        assert "L0" in entry.layers, f"{entry.id} is not an L0 source"
        assert entry.domains, "an L0 source must declare its egress allowlist"


def test_recorded_client_urls_are_all_allowed(registry: Any, fixtures_dir: Path) -> None:
    """Every URL in a fixture must be one the source is allowed to call."""
    for path in sorted((fixtures_dir / "http").glob("*.json")):
        entry = registry.by_id(path.stem)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for url in payload["responses"]:
            check_egress(entry.id, entry.domains, url)  # raises if a fixture breaks the allowlist


def test_policy_defaults_are_sane() -> None:
    policy = HttpPolicy()
    assert policy.timeout_s > 0
    assert policy.max_attempts >= 2
    assert policy.backoff_for(1) < policy.backoff_for(2) <= policy.max_backoff_s


def test_request_url_is_stable_for_fixtures() -> None:
    """Fixtures are keyed by URL, so equivalent requests must serialize identically."""
    a = request_url("https://x.test/p", {"b": "2", "a": "1"})
    b = request_url("https://x.test/p", {"a": "1", "b": "2"})
    assert a == b


def test_wiki_backoff_lookback_matches_the_fixture(registry: Any, fixtures_dir: Path) -> None:
    """The recorded days are exactly the ones a cold start computes from the same clock."""
    entry = registry.by_id("wiki_pageviews")
    payload = json.loads(fixture_path(fixtures_dir, entry.id).read_text(encoding="utf-8"))
    recorded_days = sorted(url.rstrip("/").split("/")[-3:] for url in payload["responses"])
    assert len(recorded_days) == 3

    today = recorded_at(fixtures_dir, entry.id).replace(hour=0, minute=0, second=0, microsecond=0)
    computed = days_to_fetch(cursor=None, now=recorded_at(fixtures_dir, entry.id))
    computed_days = [[f"{day.year}", f"{day.month:02d}", f"{day.day:02d}"] for day in computed]
    assert computed_days == [list(recorded) for recorded in recorded_days]
    assert all(day < today for day in computed), "a fixture must never contain today's day"


def test_wiki_lookback_is_bounded() -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    days = days_to_fetch(cursor=(now - timedelta(days=365)).strftime("%Y-%m-%d"), now=now)
    assert len(days) == 3
