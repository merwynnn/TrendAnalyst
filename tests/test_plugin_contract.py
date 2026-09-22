"""Plugin-contract tests: boundary models, the budget leash, and the registry gate.

Two guarantees are asserted here rather than assumed:

* the module under test is incapable of touching the network (checked with `ast`, not by
  trusting a comment), and
* nothing sleeps — backoff and rate limiting are exercised by advancing a fake clock.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from trend_analyst.sources import base as base_module
from trend_analyst.sources.base import (
    Decision,
    FetchContext,
    PluginContractError,
    RawBatch,
    Signal,
    SourceBudget,
    SourcePlugin,
    TokenBucket,
    content_hash,
    load_plugin,
    normalize_payload,
    validate_plugin,
)
from trend_analyst.sources.registry import SourceEntry, load_registry


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, start: float = 0.0) -> None:
        self._monotonic = start
        self._wall = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += timedelta(seconds=seconds)


class FakeHttpClient:
    """Records what a plugin asked for; returns canned responses."""

    def __init__(self, responses: list[tuple[int, str]] | None = None) -> None:
        self.requests: list[str] = []
        self._responses = responses or [(200, "ok")]

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> base_module.HttpResponse:
        self.requests.append(url)
        status, text = self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]
        return base_module.HttpResponse(url=url, status_code=status, text=text)


def registry_entry(**overrides: Any) -> SourceEntry:
    fields: dict[str, Any] = {
        "id": "fake_source",
        "role": "Fake",
        "module": "trend_analyst.sources.tier_s.hn",
        "tier": "S",
        "layers": ("L0",),
        "schedule": "nightly",
        "budget_per_day": 10,
        "rps": 2.0,
        "cache_ttl_h": 24,
        "enabled": True,
        "domains": ("example.com",),
    }
    fields.update(overrides)
    return SourceEntry(**fields)


def make_plugin(**declared: Any) -> type[SourcePlugin]:
    """A plugin class with exactly the declarations a test wants.

    Built with `type()` rather than by assigning attributes afterwards: ABCs compute
    `__abstractmethods__` when the class is created, so post-hoc assignment would leave
    the class abstract and uninstantiable.
    """
    members: dict[str, Any] = {
        "id": "fake_source",
        "tier": "S",
        "layers": ("L0",),
        "schedule": "nightly",
        "budget_per_day": 10,
        "rps": 2.0,
        "cache_ttl_h": 24,
        "domains": ("example.com",),
    }
    members.update(declared)

    def fetch(self: SourcePlugin, ctx: FetchContext) -> RawBatch:
        return RawBatch(source_id=self.id, fetched_at=datetime.now(UTC), parts=())

    def parse(self: SourcePlugin, raw: RawBatch) -> list[Signal]:
        return []

    members["fetch"] = fetch
    members["parse"] = parse
    return cast("type[SourcePlugin]", type("FakePlugin", (SourcePlugin,), members))


# ---------------------------------------------------------------------------
# Boundary models and hashing (spec §5.2)
# ---------------------------------------------------------------------------
def test_payload_normalization_is_line_ending_and_whitespace_insensitive() -> None:
    assert normalize_payload("a\r\nb\r\n") == "a\nb"
    assert content_hash(["a\r\nb\r\n"]) == content_hash(["a\nb"])
    assert content_hash(["  x  "]) == content_hash(["x"])


def test_content_hash_changes_when_content_changes() -> None:
    assert content_hash(["one"]) != content_hash(["one!"])
    assert content_hash(["one"]) != content_hash(["two"])
    assert content_hash(["one"]) == content_hash(["one"])


def test_content_hash_ignores_whitespace_only_differences() -> None:
    """Normalization exists so a re-fetched identical page does not look like a change."""
    assert content_hash(["one "]) == content_hash(["one"])
    assert content_hash(["  one"]) == content_hash(["one"])
    assert content_hash(["one\n\n"]) == content_hash(["one"])


def test_content_hash_is_injective_across_boundaries() -> None:
    """["ab","c"] must not hash like ["a","bc"] — otherwise dedup would swallow items."""
    assert content_hash(["ab", "c"]) != content_hash(["a", "bc"])


def test_raw_batch_derives_its_hash_and_cursor() -> None:
    batch = RawBatch.from_parts(
        source_id="fake_source",
        parts=["payload"],
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        cursor="next-page",
    )
    assert batch.content_hash == content_hash(["payload"])
    assert batch.item_count == 1
    assert batch.byte_size == len(b"payload")
    assert batch.request_count == 1, "request_count defaults to the number of parts"
    assert batch.cursor == "next-page"
    assert batch.is_unchanged_since(batch.content_hash) is True
    assert batch.is_unchanged_since("other") is False
    assert batch.is_unchanged_since(None) is False


def test_raw_batch_hash_is_derived_not_stored() -> None:
    """A stored hash can go stale and lie about the payload it claims to describe."""
    batch = RawBatch.from_parts(
        source_id="s", parts=["a"], fetched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert "content_hash" not in batch.model_dump()
    assert batch.content_hash == content_hash(["a"])


def test_raw_batch_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        RawBatch(source_id="s", fetched_at=datetime(2026, 1, 1), parts=["x"])  # noqa: DTZ001


def test_raw_batch_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RawBatch(
            source_id="s",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            parts=["x"],
            surprise=1,  # type: ignore[call-arg]
        )


def test_signal_carries_the_grounding_fields() -> None:
    signal = Signal(
        source_id="fake_source",
        entity="standing desk",
        metric="complaints",
        value=42.0,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        url="https://example.com/thread",
        quote="the motor died in a month",
    )
    assert signal.is_grounded is True
    assert signal.metadata == {}
    assert signal.category is None

    ungrounded = signal.model_copy(update={"url": None})
    assert ungrounded.is_grounded is False


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("entity", "", "must not be empty"),
        ("metric", "   ", "must not be empty"),
        ("url", "ftp://example.com", "must be http"),
        ("ts", datetime(2026, 1, 1), "timezone-aware"),  # noqa: DTZ001
    ],
)
def test_signal_validation(field: str, value: Any, expected: str) -> None:
    payload: dict[str, Any] = {
        "source_id": "s",
        "entity": "e",
        "metric": "m",
        "value": 1.0,
        "ts": datetime(2026, 1, 1, tzinfo=UTC),
    }
    payload[field] = value
    with pytest.raises(ValidationError, match=expected):
        Signal(**payload)


# ---------------------------------------------------------------------------
# Rate limiting (spec §4.3)
# ---------------------------------------------------------------------------
def test_token_bucket_allows_capacity_then_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=2.0, capacity=2.0, clock=clock)

    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False, "the burst is spent"

    clock.advance(0.5)  # at 2 rps, half a second buys exactly one token
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False

    clock.advance(10.0)
    assert bucket.available == pytest.approx(2.0), "never refills past capacity"


@pytest.mark.parametrize(("rate", "capacity"), [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -2.0)])
def test_token_bucket_rejects_nonsense_configuration(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        TokenBucket(rate_per_second=rate, capacity=capacity, clock=FakeClock())


def test_token_bucket_rejects_zero_token_requests() -> None:
    bucket = TokenBucket(rate_per_second=1.0, capacity=1.0, clock=FakeClock())
    with pytest.raises(ValueError, match="tokens must be > 0"):
        bucket.try_acquire(0)


# ---------------------------------------------------------------------------
# The leash: budget, rate, 429 backoff (spec §4.3)
# ---------------------------------------------------------------------------
def test_budget_is_spent_and_then_exhausted() -> None:
    clock = FakeClock()
    budget = SourceBudget(
        source_id="s", budget_per_day=3, rps=1000.0, clock=clock, max_backoff_s=60.0
    )
    decisions: list[Decision] = []
    for _ in range(4):
        clock.advance(1.0)  # let the rate limiter refill so the budget is what refuses
        decisions.append(budget.try_acquire())

    assert decisions == ["ok", "ok", "ok", "budget_exhausted"]
    assert budget.spent_today == 3
    assert budget.remaining == 0
    assert budget.burn_pct == pytest.approx(100.0)


def test_rate_limit_refuses_requests_that_arrive_too_fast() -> None:
    clock = FakeClock()
    budget = SourceBudget(source_id="s", budget_per_day=100, rps=2.0, clock=clock)

    assert budget.try_acquire() == "ok"
    assert budget.try_acquire() == "rate_limited"
    clock.advance(0.5)
    assert budget.try_acquire() == "ok"


def test_429_starts_backoff_and_the_attempt_is_charged_to_the_budget() -> None:
    """Spec §4.3: on 429 the plugin MUST back off and record quota_spend."""
    clock = FakeClock()
    budget = SourceBudget(source_id="s", budget_per_day=10, rps=100.0, clock=clock)

    assert budget.try_acquire() == "ok"
    budget.record_response(429, retry_after_s=30.0)

    assert budget.spent_today == 1, "the attempt that earned the 429 is still spend"
    assert budget.rate_limit_hits == 1
    assert budget.backoff_remaining_s() == pytest.approx(30.0)
    assert budget.try_acquire() == "backing_off"

    clock.advance(29.9)
    assert budget.try_acquire() == "backing_off"
    clock.advance(0.2)
    assert budget.try_acquire() == "ok", "backoff expires on its own"


def test_backoff_doubles_without_retry_after_and_is_capped() -> None:
    clock = FakeClock()
    budget = SourceBudget(
        source_id="s", budget_per_day=100, rps=1000.0, clock=clock,
        backoff_base_s=30.0, max_backoff_s=100.0,
    )

    budget.record_response(429)
    assert budget.backoff_remaining_s() == pytest.approx(30.0)

    clock.advance(30.0)
    budget.record_response(429)
    assert budget.backoff_remaining_s() == pytest.approx(60.0)

    clock.advance(60.0)
    budget.record_response(429)
    assert budget.backoff_remaining_s() == pytest.approx(100.0), "capped at max_backoff_s"

    clock.advance(100.0)
    budget.record_response(429)
    assert budget.backoff_remaining_s() == pytest.approx(100.0), "stays at the cap"


def test_retry_after_is_capped_too() -> None:
    budget = SourceBudget(
        source_id="s", budget_per_day=10, rps=1000.0, clock=FakeClock(), max_backoff_s=100.0
    )
    budget.record_response(429, retry_after_s=10_000.0)
    assert budget.backoff_remaining_s() == pytest.approx(100.0)


def test_success_clears_backoff_and_resets_the_curve() -> None:
    clock = FakeClock()
    budget = SourceBudget(source_id="s", budget_per_day=100, rps=1000.0, clock=clock)

    budget.record_response(429)
    budget.record_response(429)
    budget.record_response(200)

    assert budget.backoff_remaining_s() == 0.0
    clock.advance(1000.0)
    budget.record_response(429)
    assert budget.backoff_remaining_s() == pytest.approx(30.0), "curve reset to the base"


def test_client_errors_do_not_start_backoff() -> None:
    budget = SourceBudget(source_id="s", budget_per_day=10, rps=1000.0, clock=FakeClock())
    budget.record_response(404)
    budget.record_response(500)
    assert budget.backoff_remaining_s() == 0.0
    assert budget.rate_limit_hits == 0


def test_reset_day_refills_the_budget() -> None:
    clock = FakeClock()
    budget = SourceBudget(source_id="s", budget_per_day=2, rps=1000.0, clock=clock)
    assert budget.try_acquire() == "ok"
    clock.advance(1.0)
    assert budget.try_acquire() == "ok"
    clock.advance(1.0)
    assert budget.try_acquire() == "budget_exhausted"
    budget.reset_day()
    assert budget.spent_today == 0
    assert budget.try_acquire() == "ok"


def test_snapshot_reports_what_health_needs() -> None:
    clock = FakeClock()
    budget = SourceBudget(source_id="wiki_pageviews", budget_per_day=2000, rps=100.0, clock=clock)
    for _ in range(1700):
        clock.advance(1.0)  # twice the budget: the budget is the binding constraint
        budget.try_acquire()
    budget.record_response(429, retry_after_s=15.0)

    snapshot = budget.snapshot()
    assert snapshot.source_id == "wiki_pageviews"
    assert snapshot.spent_today == 1700
    assert snapshot.remaining == 300
    assert snapshot.burn_pct == pytest.approx(85.0)
    assert snapshot.in_backoff is True
    assert snapshot.backoff_remaining_s == pytest.approx(15.0)
    assert snapshot.rate_limit_hits == 1


@pytest.mark.parametrize(("budget", "rps"), [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -0.5)])
def test_budget_rejects_nonsense_configuration(budget: int, rps: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        SourceBudget(source_id="s", budget_per_day=budget, rps=rps, clock=FakeClock())


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
def test_fetch_context_requires_a_matching_budget() -> None:
    budget = SourceBudget(source_id="wiki_pageviews", budget_per_day=10, rps=1.0, clock=FakeClock())
    with pytest.raises(ValueError, match="context/source mismatch"):
        FetchContext(
            run_id="run-1",
            source_id="hn_firebase",
            client=FakeHttpClient(),
            budget=budget,
            clock=FakeClock(),
        )


def test_fetch_context_is_frozen() -> None:
    budget = SourceBudget(source_id="s", budget_per_day=10, rps=1.0, clock=FakeClock())
    ctx = FetchContext(
        run_id="run-1", source_id="s", client=FakeHttpClient(), budget=budget, clock=FakeClock()
    )
    with pytest.raises(Exception, match=r"cannot assign to field|frozen"):
        ctx.run_id = "run-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The registry gate: a plugin may not disagree with (or widen) its registry entry
# ---------------------------------------------------------------------------
def test_matching_plugin_passes_validation() -> None:
    validate_plugin(registry_entry(), make_plugin())


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ({"budget_per_day": 999}, "budget_per_day"),
        ({"rps": 0.5}, "rps"),
        ({"layers": ("L2",)}, "layers"),
        ({"schedule": "weekly"}, "schedule"),
        ({"tier": "A"}, "tier"),
        ({"cache_ttl_h": 1}, "cache_ttl_h"),
        ({"id": "other"}, "id"),
        ({"domains": ("example.com", "evil.test")}, "domains"),
    ],
)
def test_plugin_disagreement_with_the_registry_is_an_error(
    declared: dict[str, Any], expected: str
) -> None:
    with pytest.raises(PluginContractError, match=expected):
        validate_plugin(registry_entry(), make_plugin(**declared))


def test_plugin_cannot_widen_its_own_egress_allowlist() -> None:
    """Spec §10: the allowlist comes from the registry. A plugin that adds a host is a bug."""
    with pytest.raises(PluginContractError, match=r"plugin .*evil\.test.*registry"):
        validate_plugin(registry_entry(), make_plugin(domains=("example.com", "evil.test")))


def test_plugin_must_be_a_source_plugin_subclass() -> None:
    class NotAPlugin:
        pass

    with pytest.raises(PluginContractError, match=r"not a SourcePlugin subclass"):
        validate_plugin(registry_entry(), NotAPlugin)  # type: ignore[arg-type]


def test_plugin_must_implement_fetch_and_parse() -> None:
    class Incomplete(SourcePlugin):
        id = "fake_source"
        tier = "S"
        layers = ("L0",)
        schedule = "nightly"
        budget_per_day = 10
        rps = 2.0
        cache_ttl_h = 24
        domains = ("example.com",)

    with pytest.raises(PluginContractError, match="does not implement"):
        validate_plugin(registry_entry(), Incomplete)


def test_load_plugin_reports_a_module_with_no_plugin_yet(repo_root: Path) -> None:
    """Remaining stub modules must fail loudly; the loader must say which source, precisely."""
    registry = load_registry(repo_root / "config" / "sources.yaml")
    entry = registry.by_id("google_books")  # deferred: throttled network, documented in-stub

    with pytest.raises(PluginContractError, match="defines no SourcePlugin with id='google_books'"):
        load_plugin(entry)


def test_load_plugin_returns_an_implemented_collector(repo_root: Path) -> None:
    """The other half of the lifecycle: a module that IS implemented loads and validates."""
    registry = load_registry(repo_root / "config" / "sources.yaml")

    plugin = load_plugin(registry.by_id("hn_firebase"))

    assert isinstance(plugin, SourcePlugin)
    assert plugin.id == "hn_firebase"


def test_load_plugin_imports_validates_and_instantiates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path of "add a source": module + registry entry + this loader."""
    plugin_class = make_plugin()
    fake_module = ModuleType("trend_analyst.sources.tier_s.fake")
    fake_module.Plugin = plugin_class  # type: ignore[attr-defined]

    monkeypatch.setattr(
        base_module.importlib, "import_module", lambda name: fake_module
    )

    instance = load_plugin(registry_entry())
    assert isinstance(instance, SourcePlugin)
    assert instance.id == "fake_source"
    assert repr(instance) == "<FakePlugin id='fake_source' tier=S>"


def test_load_plugin_refuses_two_plugins_with_the_same_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_plugin()
    second = make_plugin()
    second.__name__ = "AnotherPlugin"
    fake_module = ModuleType("trend_analyst.sources.tier_s.fake")
    fake_module.First = first  # type: ignore[attr-defined]
    fake_module.Second = second  # type: ignore[attr-defined]
    monkeypatch.setattr(
        base_module.importlib, "import_module", lambda name: fake_module
    )

    with pytest.raises(PluginContractError, match="defines several plugins"):
        load_plugin(registry_entry())


# ---------------------------------------------------------------------------
# Zero-network guarantee
# ---------------------------------------------------------------------------
def test_base_module_imports_nothing_network_capable() -> None:
    """The contract module must be incapable of network access, provably.

    Checked with `ast` instead of trusting a comment: a future edit that reaches for
    httpx inside the contract fails here, before it can make tests flaky.
    """
    source = Path(base_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"httpx", "requests", "urllib", "urllib3", "socket", "aiohttp", "http"}
    assert not (imported & forbidden), f"the contract module must not import {imported & forbidden}"


def test_decision_literal_is_the_documented_set() -> None:
    """The ledger records one of these; a new state must be added deliberately."""
    assert set(Decision.__args__) == {"ok", "rate_limited", "budget_exhausted", "backing_off"}
