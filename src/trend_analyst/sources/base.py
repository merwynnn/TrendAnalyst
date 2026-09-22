"""The source plugin contract: what every endpoint must implement, and what it may spend.

Spec §4.1 in full, plus the two mechanisms the spec demands around it:

* **the boundary** — `fetch(ctx) -> RawBatch` and `parse(raw) -> list[Signal]`. The
  orchestrator only ever talks to this interface, which is what makes endpoints
  addable, removable and movable without touching pipeline code.
* **the leash** (spec §4.3) — a central token bucket per source id, a daily budget, and
  mandatory backoff on HTTP 429. A plugin cannot spend more than the registry allows,
  because it never gets to make a request on its own: it asks
  :class:`SourceBudget` for permission first.

Three properties are deliberate:

* **No HTTP library is imported here.** `HttpClient` is a `Protocol`; P1 supplies the
  httpx implementation and the tests supply recorded fixtures. `tests/test_plugin_contract.py`
  asserts that this module imports nothing network-capable.
* **Nothing sleeps.** Time comes from an injected :class:`Clock`, so backoff and rate
  limiting are tested by advancing a fake clock instead of waiting.
* **The registry is the single source of truth.** A plugin declares its budgets, and
  :func:`validate_plugin` fails if they disagree with `sources.yaml` — including the
  egress allowlist, so a plugin cannot widen its own network permissions.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trend_analyst.pipeline.layers import SourceLayer
from trend_analyst.sources.registry import Schedule, SourceEntry, Tier

__all__ = [
    "HTTP_CLIENT_ERROR",
    "HTTP_NOT_FOUND",
    "HTTP_OK",
    "Clock",
    "Decision",
    "FetchContext",
    "FixedClock",
    "HttpClient",
    "HttpResponse",
    "MissingCredentialError",
    "PluginContractError",
    "QuotaSnapshot",
    "RawBatch",
    "Signal",
    "SourceBudget",
    "SourcePlugin",
    "SourceSkippedError",
    "SystemClock",
    "TokenBucket",
    "content_hash",
    "load_plugin",
    "normalize_payload",
    "validate_plugin",
]

_BOUNDARY_CONFIG = ConfigDict(extra="forbid", frozen=True)

#: HTTP facts the leash reacts to. Named so the intent is readable at the call site.
_RATE_LIMIT_STATUS: Final = 429
_SUCCESS_STATUSES: Final = range(200, 300)


#: HTTP statuses plugins reason about by name rather than by number.
HTTP_OK: Final = 200
HTTP_CLIENT_ERROR: Final = 400
HTTP_NOT_FOUND: Final = 404


class PluginContractError(RuntimeError):
    """A plugin that does not honour the contract, or disagrees with the registry."""


class SourceSkippedError(RuntimeError):
    """A source is not run this time, for a reason the run log must record verbatim.

    Raised instead of returning an empty batch, because "we skipped this" and "this
    returned nothing" are different facts, and the run report shows them differently.
    The budget path uses it (spec §4.3: quota exhausted, or backing off after a 429), and
    so does a source that has nothing to do this pass.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MissingCredentialError(SourceSkippedError):
    """The plugin has no credential, so it declines to call.

    A subclass of `SourceSkippedError` on purpose: "we cannot call this" and "the call failed" are
    different outcomes, and the difference decides whether a monitor agent is looking at a gap in
    tonight's evidence or at an incident.
    """


# ---------------------------------------------------------------------------
# Time and HTTP: injected, never imported from a concrete implementation
# ---------------------------------------------------------------------------
@runtime_checkable
class Clock(Protocol):
    """Time source. Injected so that rate limiting and backoff are testable."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, for measuring intervals."""

    def now(self) -> datetime:
        """Timezone-aware wall clock (UTC)."""


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A pinned clock, for offline replays and deterministic tests.

    Wall time does not move: a replay must request exactly the URLs that were recorded, and
    several collectors derive those URLs from the date (Wikipedia's days, Arctic Shift's
    ``after=``). If time drifted, a replay would ask for days nobody recorded — correctly,
    but uselessly.

    Monotonic time, on the other hand, advances a second per read. That is deliberate: it
    is only used for rate limiting, and a clock that never moves would make a polite
    collector refuse its own requests for lack of elapsed time. The result is a replay that
    behaves as if the run took its time, without waiting for it.
    """

    def __init__(self, now: datetime, *, seconds_per_read: float = 1.0) -> None:
        self._now = now
        self._monotonic = 0.0
        self._step = seconds_per_read

    def monotonic(self) -> float:
        current = self._monotonic
        self._monotonic += self._step
        return current

    def now(self) -> datetime:
        return self._now


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP response, normalised. Deliberately tiny: status, body, headers."""

    url: str
    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def retry_after_s(self) -> float | None:
        """`Retry-After` in seconds, when the server sends one."""
        raw = self.headers.get("Retry-After") or self.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return None  # an HTTP-date form; the backoff curve covers it


@runtime_checkable
class HttpClient(Protocol):
    """The only way a plugin may touch the network (spec §10).

    Implementations MUST enforce the per-source egress allowlist and MUST time out.
    """

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """GET `url`. Raises on transport failure; returns the response otherwise."""

    def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        content: str | None = None,
    ) -> HttpResponse:
        """POST `url` with an optional form body.

        Needed for OAuth token endpoints (eBay Browse, and any Tier-A API using client credentials).
        A GET-only client would push a plugin into faking the dance, and a fixture would then pin
        the
        fiction.
        """


# ---------------------------------------------------------------------------
# Boundary models (spec §4.1, §5.2)
# ---------------------------------------------------------------------------
def normalize_payload(payload: str) -> str:
    """Normalize a payload before hashing (spec §5.2: "normalized then hashed").

    Line endings are unified and trailing whitespace is dropped, so the same logical
    content fetched twice does not look like a change and trigger useless work.
    """
    return payload.replace("\r\n", "\n").replace("\r", "\n").strip()


def content_hash(parts: tuple[str, ...] | list[str]) -> str:
    """SHA-256 over the normalized parts (spec §5.2).

    This single mechanism is what keeps nightly runs near ten minutes: an unchanged hash
    means skip parsing, skip scoring, skip the LLM.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(normalize_payload(part).encode("utf-8"))
        digest.update(b"\x00")  # separator: ["ab","c"] must not hash like ["a","bc"]
    return digest.hexdigest()


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (store UTC)")
    return value.astimezone(UTC)


class RawBatch(BaseModel):
    """What `fetch` returns: the raw payloads, untouched, plus the cursor to store."""

    model_config = _BOUNDARY_CONFIG

    source_id: str
    fetched_at: datetime
    parts: tuple[str, ...]
    cursor: str | None = None
    status_codes: tuple[int, ...] = ()
    request_count: int = 0
    #: The source itself reports there is nothing new since the stored cursor (the HTTP
    #: 304 idea, applied to a watermark). The orchestrator then skips parsing, scoring and
    #: the LLM entirely — which is what makes a second run nearly free (spec §5.1-5.2).
    not_modified: bool = False

    _check_fetched_at = field_validator("fetched_at")(_require_aware)

    @field_validator("source_id")
    @classmethod
    def _source_id_present(cls, value: str) -> str:
        if not value:
            raise ValueError("source_id is required")
        return value

    @property
    def content_hash(self) -> str:
        """Derived, never stored: a stored hash can go stale and lie (spec §5.2)."""
        return content_hash(self.parts)

    @property
    def item_count(self) -> int:
        return len(self.parts)

    @property
    def byte_size(self) -> int:
        return sum(len(part.encode("utf-8")) for part in self.parts)

    def is_unchanged_since(self, stored_hashes: str | set[str] | None) -> bool:
        """Whether this batch matches what the lake already holds.

        Accepts a set of recently stored hashes (or a single hash, or None). The set form is what
        the orchestrator passes: a source that emits several payloads per run needs its whole
        recent history checked, not only the most recent row.
        """
        if stored_hashes is None:
            return False
        if isinstance(stored_hashes, str):
            return stored_hashes == self.content_hash
        return self.content_hash in stored_hashes

    @classmethod
    def from_parts(
        cls,
        *,
        source_id: str,
        parts: list[str] | tuple[str, ...],
        fetched_at: datetime,
        cursor: str | None = None,
        status_codes: tuple[int, ...] = (),
        request_count: int | None = None,
        not_modified: bool = False,
    ) -> RawBatch:
        """Build a batch, counting requests when the caller does not."""
        return cls(
            source_id=source_id,
            fetched_at=fetched_at,
            parts=tuple(parts),
            cursor=cursor,
            status_codes=status_codes,
            request_count=len(parts) if request_count is None else request_count,
            not_modified=not_modified,
        )


class Signal(BaseModel):
    """A normalized fact (spec §7 `signals`): entity, source, metric, value, ts."""

    model_config = _BOUNDARY_CONFIG

    source_id: str
    entity: str
    metric: str
    value: float
    ts: datetime
    url: str | None = None
    quote: str | None = None
    category: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    _check_ts = field_validator("ts")(_require_aware)

    @field_validator("entity", "metric")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return value

    @property
    def is_grounded(self) -> bool:
        """A factual claim needs a quote AND a URL before it may reach scores or briefs."""
        return bool(self.quote) and bool(self.url)


# ---------------------------------------------------------------------------
# The leash: token bucket, daily budget, backoff (spec §4.3)
# ---------------------------------------------------------------------------
#: Why a request was refused. Reported in the run ledger so a stall is never mysterious.
Decision = Literal["ok", "rate_limited", "budget_exhausted", "backing_off"]


class TokenBucket:
    """Classic token bucket. Pure logic; time comes from the injected clock."""

    def __init__(self, *, rate_per_second: float, capacity: float, clock: Clock) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._rate = rate_per_second
        self._capacity = capacity
        self._clock = clock
        self._tokens = capacity
        self._updated_at = clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = max(now - self._updated_at, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated_at = now

    @property
    def available(self) -> float:
        """Tokens available right now, after refilling."""
        self._refill()
        return self._tokens

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take `tokens` if available. Never blocks, never sleeps."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


class QuotaSnapshot(BaseModel):
    """Per-source spend within one run, for the run report."""

    model_config = _BOUNDARY_CONFIG

    source_id: str
    budget_per_day: int
    spent_today: int = 0
    rate_limit_hits: int = 0
    in_backoff: bool = False
    backoff_remaining_s: float = 0.0

    @property
    def remaining(self) -> int:
        return max(self.budget_per_day - self.spent_today, 0)

    @property
    def burn_pct(self) -> float:
        return 100.0 * self.spent_today / self.budget_per_day if self.budget_per_day else 0.0


class SourceBudget:
    """The leash for one source: a rate, a daily budget, and 429 backoff.

    Usage in a plugin (P1)::

        decision = ctx.budget.try_acquire()
        if decision != "ok":
            return                       # the run continues without this source
        response = ctx.client.get(url)
        ctx.budget.record_response(response.status_code, retry_after_s=response.retry_after_s)

    A request that is *attempted* counts against the daily budget — including a 429,
    because spec §4.3 requires quota_spend to be recorded for it. Counting the attempt
    once is why :meth:`record_response` never increments spend again.
    """

    def __init__(
        self,
        *,
        source_id: str,
        budget_per_day: int,
        rps: float,
        clock: Clock,
        max_backoff_s: float = 900.0,
        backoff_base_s: float = 30.0,
        burst: float = 1.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if budget_per_day <= 0:
            raise ValueError("budget_per_day must be > 0")
        if rps <= 0:
            raise ValueError("rps must be > 0")
        self.source_id = source_id
        self.budget_per_day = budget_per_day
        self.rps = rps
        self.max_backoff_s = max_backoff_s
        self.backoff_base_s = backoff_base_s
        self._clock = clock
        self._sleep = sleep or time.sleep
        self._bucket = TokenBucket(rate_per_second=rps, capacity=max(burst, 1.0), clock=clock)
        self._spent = 0
        self._rate_limit_hits = 0
        self._backoff_until: float | None = None
        self._backoff_s = backoff_base_s

    # -- decisions ---------------------------------------------------------
    def acquire(self, *, max_wait_s: float = 5.0) -> Decision:
        """Ask permission, WAITING politely for a rate-limit slot (up to `max_wait_s`).

        The waiting is the point. A bucket that refuses the moment the rate is saturated
        makes a polite crawler silently collect a fraction of what it intended — measured
        while recording fixtures: 3 of 15 subreddits were fetched, then every further
        attempt was refused. Waiting is what "2 requests per second" means.

        It does NOT wait out a backoff or a budget refusal: those are decisions about the
        source rather than about timing, and the caller must react to them.
        """
        deadline = self._clock.monotonic() + max_wait_s
        # A bounded loop as well as a deadline: if a caller injects a clock whose monotonic
        # time does not advance with sleep, the deadline never arrives and this would spin
        # forever. A wrong clock must not become a hung pipeline.
        max_attempts = int(max_wait_s * self.rps) + 2
        for _ in range(max_attempts):
            decision = self.try_acquire()
            if decision != "rate_limited":
                return decision
            now = self._clock.monotonic()
            if now >= deadline:
                return "rate_limited"
            self._sleep(min(1.0 / self.rps, deadline - now))
        return "rate_limited"

    def try_acquire(self) -> Decision:
        """Ask permission for one request. Counts it against the budget when granted."""
        now = self._clock.monotonic()
        if self._backoff_until is not None:
            if now < self._backoff_until:
                return "backing_off"
            self._backoff_until = None
        if self._spent >= self.budget_per_day:
            return "budget_exhausted"
        if not self._bucket.try_acquire():
            return "rate_limited"
        self._spent += 1
        return "ok"

    def record_response(self, status_code: int, *, retry_after_s: float | None = None) -> None:
        """Feed a response back in: 429 starts (or extends) backoff, success clears it."""
        if status_code == _RATE_LIMIT_STATUS:
            self._rate_limit_hits += 1
            wait = retry_after_s if retry_after_s is not None else self._backoff_s
            wait = min(max(wait, 0.0), self.max_backoff_s)
            self._backoff_until = self._clock.monotonic() + wait
            self._backoff_s = min(self._backoff_s * 2, self.max_backoff_s)
            return
        if status_code in _SUCCESS_STATUSES:
            self._reset_backoff()

    def _reset_backoff(self) -> None:
        self._backoff_until = None
        self._backoff_s = self.backoff_base_s

    # -- state -------------------------------------------------------------
    @property
    def spent_today(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(self.budget_per_day - self._spent, 0)

    @property
    def burn_pct(self) -> float:
        return 100.0 * self._spent / self.budget_per_day

    @property
    def rate_limit_hits(self) -> int:
        return self._rate_limit_hits

    def backoff_remaining_s(self) -> float:
        """Seconds left in backoff, 0 when not backing off."""
        if self._backoff_until is None:
            return 0.0
        return max(self._backoff_until - self._clock.monotonic(), 0.0)

    def reset_day(self) -> None:
        """Start a new day: the budget refills, the rate limiter keeps its phase."""
        self._spent = 0

    def charge(self, spent: int) -> None:
        """Rehydrate today's spend from the ledger (spec §5.4).

        A resumed run must continue from what was already spent, not from zero — otherwise
        a crash becomes a way to spend the daily budget twice. Charging more than the
        budget is allowed and simply means the source is exhausted; refusing would lose
        the fact.
        """
        if spent < 0:
            raise ValueError("spent cannot be negative")
        self._spent = spent

    def snapshot(self) -> QuotaSnapshot:
        remaining_backoff = self.backoff_remaining_s()
        return QuotaSnapshot(
            source_id=self.source_id,
            budget_per_day=self.budget_per_day,
            spent_today=self._spent,
            rate_limit_hits=self._rate_limit_hits,
            in_backoff=remaining_backoff > 0,
            backoff_remaining_s=remaining_backoff,
        )


# ---------------------------------------------------------------------------
# The plugin contract (spec §4.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FetchContext:
    """Everything a plugin is allowed to know or use during a fetch.

    A dataclass rather than a pydantic model: this is live runtime context, not data
    crossing a serialization boundary (the boundary models are `RawBatch` and `Signal`).
    """

    run_id: str
    source_id: str
    client: HttpClient
    budget: SourceBudget
    clock: Clock
    cursor: str | None = None
    category: str | None = None
    dry_run: bool = False
    max_items: int | None = None

    def __post_init__(self) -> None:
        if self.source_id != self.budget.source_id:
            raise ValueError(
                f"context/source mismatch: source_id={self.source_id!r} "
                f"but budget is for {self.budget.source_id!r}"
            )


class SourcePlugin(ABC):
    """Base class for every data endpoint (spec §4.1).

    Subclasses declare the same budget the registry declares, then implement
    :meth:`fetch` and :meth:`parse`. They never construct an HTTP client, never sleep,
    and never read the clock directly — everything arrives through
    :class:`FetchContext`, which is what makes recorded-fixture tests possible.

    Both methods are abstract, so a plugin that forgets one cannot even be instantiated.
    """

    #: Stable identifier, used as the DB key. Must equal the registry key.
    id: ClassVar[str] = ""
    tier: ClassVar[Tier]
    layers: ClassVar[tuple[SourceLayer, ...]]
    schedule: ClassVar[Schedule]
    budget_per_day: ClassVar[int]
    rps: ClassVar[float]
    cache_ttl_h: ClassVar[int] = 24
    #: Egress allowlist; must equal the registry's, so a plugin cannot widen it.
    domains: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def fetch(self, ctx: FetchContext) -> RawBatch:
        """Fetch raw payloads. MUST respect ctx.budget before every request."""
        raise NotImplementedError

    @abstractmethod
    def parse(self, raw: RawBatch) -> list[Signal]:
        """Turn raw payloads into normalized signals. MUST NOT perform I/O."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r} tier={self.tier}>"


def _declared(plugin: type[SourcePlugin], attribute: str) -> Any:
    return getattr(plugin, attribute, None)


def validate_plugin(entry: SourceEntry, plugin: type[SourcePlugin]) -> None:
    """Fail if a plugin disagrees with its registry entry.

    The registry is the source of truth for tier, layers, budgets and egress. Without
    this check a plugin could quietly widen its own permissions or outspend its leash —
    the exact failure mode spec §4.3 and §10 exist to prevent.
    """
    if not (inspect.isclass(plugin) and issubclass(plugin, SourcePlugin)):
        raise PluginContractError(f"{plugin!r} is not a SourcePlugin subclass")
    if inspect.isabstract(plugin):
        raise PluginContractError(f"{plugin.__name__} does not implement fetch/parse")

    expected: dict[str, Any] = {
        "id": entry.id,
        "tier": entry.tier,
        "layers": entry.layers,
        "schedule": entry.schedule,
        "budget_per_day": entry.budget_per_day,
        "rps": entry.rps,
        "cache_ttl_h": entry.cache_ttl_h,
        "domains": entry.domains,
    }
    mismatches: list[str] = []
    for attribute, wanted in expected.items():
        declared = _declared(plugin, attribute)
        if declared is None:
            mismatches.append(f"{attribute} is not declared")
        elif attribute in {"layers", "domains"}:
            if tuple(declared) != tuple(wanted):
                mismatches.append(
                    f"{attribute}: plugin {tuple(declared)} != registry {tuple(wanted)}"
                )
        elif declared != wanted:
            mismatches.append(f"{attribute}: plugin {declared!r} != registry {wanted!r}")

    if mismatches:
        raise PluginContractError(
            f"{plugin.__name__} disagrees with the registry entry {entry.id!r}: "
            + "; ".join(mismatches)
        )


def _find_plugin_class(module: ModuleType, entry: SourceEntry) -> type[SourcePlugin]:
    candidates = [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, SourcePlugin) and member is not SourcePlugin
    ]
    matching = [candidate for candidate in candidates if _declared(candidate, "id") == entry.id]
    if not matching:
        raise PluginContractError(
            f"{module.__name__} defines no SourcePlugin with id={entry.id!r} "
            f"(found: {[c.__name__ for c in candidates] or 'none'})"
        )
    if len(matching) > 1:
        raise PluginContractError(
            f"{module.__name__} defines several plugins with id={entry.id!r}: "
            f"{[c.__name__ for c in matching]}"
        )
    return matching[0]


def load_plugin(entry: SourceEntry, *, settings: Any = None) -> SourcePlugin:
    """Import a registry entry's module, verify the contract, and instantiate it.

    This is the whole "add a source" lifecycle of spec §4.3 made mechanical: add a
    module, add a registry entry, and this function either returns a valid plugin or
    explains precisely what is wrong. Tier-A plugins that take credentials receive
    them when their constructor asks for `settings`; everything else is built bare.
    """
    module = importlib.import_module(entry.module)
    plugin_class = _find_plugin_class(module, entry)
    validate_plugin(entry, plugin_class)
    # Dynamic dispatch by design: Tier-A plugins declare `settings` for credentials,
    # Tier-S plugins take none. The Any cast is the honesty marker for that split.
    constructor: Any = plugin_class
    instance: SourcePlugin
    if "settings" in inspect.signature(plugin_class.__init__).parameters:
        instance = constructor(settings=settings)
    else:
        instance = constructor()
    return instance
