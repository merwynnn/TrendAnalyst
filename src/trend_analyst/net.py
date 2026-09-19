"""Network access with a per-source egress allowlist (spec §10).

Everything a plugin fetches goes through here, and here is where four rules are enforced
that a plugin cannot be trusted to remember:

* **Egress allowlist.** A source may only call the hosts its registry entry declares. The
  check happens *before* the request, and redirects are not followed (a 3xx could point
  anywhere; following it would quietly widen the allowlist).
* **Timeouts everywhere.** httpx is configured with a timeout, and a transport failure is
  a retryable event rather than a hang.
* **Bounded retries.** Transport errors and 5xx are retried with exponential backoff, up
  to a limit. **429 is deliberately NOT retried**: spec §4.3 says the plugin must back off
  and record quota spend, so the response is handed back to the caller and the
  :class:`~trend_analyst.sources.base.SourceBudget` decides what happens next.
* **No sleeping in tests.** The sleep function is injected, so retry behaviour is verified
  by counting, not by waiting.

Three clients share that policy:

``HttpxClient``      the real one.
``RecordedHttpClient``  replays a committed fixture — what every test uses.
``RecordingHttpClient`` wraps a real client and captures what it served, for
                     ``scripts/record_fixtures.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import httpx

from trend_analyst.sources.base import HttpResponse

__all__ = [
    "EgressDeniedError",
    "FixtureMissError",
    "HttpFetchError",
    "HttpPolicy",
    "HttpxClient",
    "RecordedHttpClient",
    "RecordingHttpClient",
    "check_egress",
    "fixture_path",
    "load_fixture",
    "request_url",
]


class EgressDeniedError(RuntimeError):
    """A source tried to call a host its registry entry does not declare (spec §10)."""


#: Statuses the retry loop reasons about by name.
_RATE_LIMIT_STATUS: Final = 429
_SERVER_ERROR_THRESHOLD: Final = 500


class HttpFetchError(RuntimeError):
    """Every attempt failed. Carries the attempt count so the ledger can record it."""

    def __init__(
        self, message: str, *, url: str, attempts: int, last_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.url = url
        self.attempts = attempts
        self.last_status = last_status


class FixtureMissError(RuntimeError):
    """A recorded fixture does not contain the requested URL.

    Loud on purpose: silently returning an empty body would turn "my fixture is stale"
    into "the source returned nothing", which is the kind of quiet lie this project
    refuses elsewhere.
    """


@dataclass(frozen=True, slots=True)
class HttpPolicy:
    """How patient a client is with a source that is having a bad night."""

    timeout_s: float = 20.0
    max_attempts: int = 3
    backoff_base_s: float = 1.0
    backoff_factor: float = 2.0
    max_backoff_s: float = 30.0
    user_agent: str = "TrendAnalyst/0.1 (+https://github.com/merwynnn/TrendAnalyst)"

    def backoff_for(self, attempt: int) -> float:
        """Seconds to wait before `attempt` (1-based) — exponential, capped."""
        raw = self.backoff_base_s * (self.backoff_factor ** (attempt - 1))
        return min(raw, self.max_backoff_s)


def request_url(url: str, params: Mapping[str, str] | None = None) -> str:
    """The URL as it goes on the wire: params folded in, order-stable.

    Fixtures are keyed by this exact string, so the same request must always serialize
    identically — hence the sorted parameters.
    """
    if not params:
        return url
    separator = "&" if urlsplit(url).query else "?"
    return f"{url}{separator}{urlencode(sorted(params.items()))}"


def check_egress(source_id: str, allowed_domains: tuple[str, ...], url: str) -> str:
    """Return the host of `url`, or raise if the source may not call it.

    Matching mirrors the registry's :meth:`SourceEntry.allows_host`: exact host, plus the
    explicit ``*.suffix`` form which authorises subdomains only.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise EgressDeniedError(
            f"{source_id}: refusing a non-http(s) URL: {url!r} (scheme {parts.scheme!r})"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressDeniedError(f"{source_id}: refusing a URL with no host: {url!r}")

    for pattern in allowed_domains:
        candidate = pattern.strip().lower().rstrip(".")
        if candidate.startswith("*."):
            if host.endswith(f".{candidate[2:]}"):
                return host
        elif host == candidate:
            return host

    raise EgressDeniedError(
        f"{source_id} may not call {host!r}: it is not in this source's registry `domains` "
        f"allowlist {sorted(allowed_domains)}. Add the host to config/sources.yaml (and to "
        "the plugin's declared domains) if this is intended."
    )


class HttpxClient:
    """The real client: allowlist, timeouts, bounded retries, no redirect following."""

    def __init__(
        self,
        *,
        source_id: str,
        allowed_domains: tuple[str, ...],
        policy: HttpPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        on_retry: Callable[[str, int, float, str], None] | None = None,
    ) -> None:
        self.source_id = source_id
        self.allowed_domains = allowed_domains
        self.policy = policy or HttpPolicy()
        self._sleep = sleep or time.sleep
        self._on_retry = on_retry
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.policy.timeout_s),
            follow_redirects=False,
            headers={"User-Agent": self.policy.user_agent},
            transport=transport,
        )

    def __enter__(self) -> HttpxClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _attempt(self, url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        response = self._client.get(url, headers=dict(headers or {}))
        return HttpResponse(
            url=str(response.url),
            status_code=response.status_code,
            text=response.text,
            headers=dict(response.headers),
        )

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """GET `url`, retrying transport errors and 5xx; returning 4xx as-is."""
        target = request_url(url, params)
        check_egress(self.source_id, self.allowed_domains, target)

        last_status: int | None = None
        last_detail = "no attempt was made"

        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                response = self._attempt(target, headers)
            except httpx.TransportError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            else:
                last_status = response.status_code
                if (
                    response.status_code == _RATE_LIMIT_STATUS
                    or response.status_code < _SERVER_ERROR_THRESHOLD
                ):
                    # 429 included on purpose: spec §4.3 gives the budget, not the client,
                    # the job of backing off. Any other 4xx is a final answer.
                    return response
                last_detail = f"HTTP {response.status_code}"

            if attempt == self.policy.max_attempts:
                break
            wait = self.policy.backoff_for(attempt)
            if self._on_retry is not None:
                self._on_retry(target, attempt, wait, last_detail)
            self._sleep(wait)

        raise HttpFetchError(
            f"{self.source_id}: giving up on {target} after {self.policy.max_attempts} "
            f"attempts ({last_detail})",
            url=target,
            attempts=self.policy.max_attempts,
            last_status=last_status,
        )


def fixture_path(fixtures_dir: Path, source_id: str) -> Path:
    """Where a source's recorded responses live."""
    return Path(fixtures_dir) / "http" / f"{source_id}.json"


def load_fixture(path: Path) -> dict[str, Any]:
    """Read a fixture file. Raises ``FixtureMissError`` when it is not there."""
    if not Path(path).is_file():
        raise FixtureMissError(
            f"no recorded fixture at {path}. Record one with "
            f"`uv run python -m scripts.record_fixtures --source <id>` (needs network)."
        )
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "responses" not in payload:
        raise FixtureMissError(f"{path} is not a fixture: expected an object with 'responses'")
    return payload


@dataclass
class RecordedHttpClient:
    """Replays a committed fixture. Every test uses one of these; none touch the network."""

    source_id: str
    allowed_domains: tuple[str, ...]
    fixture: Mapping[str, Any]
    requests: list[str] = field(default_factory=list)

    @classmethod
    def from_path(cls, path: Path, *, allowed_domains: tuple[str, ...]) -> RecordedHttpClient:
        payload = load_fixture(path)
        source_id = str(payload.get("source_id", Path(path).stem))
        return cls(source_id=source_id, allowed_domains=allowed_domains, fixture=payload)

    @property
    def recorded_urls(self) -> tuple[str, ...]:
        return tuple(self.fixture.get("responses", {}).keys())

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        target = request_url(url, params)
        check_egress(self.source_id, self.allowed_domains, target)
        self.requests.append(target)

        entries = self.fixture.get("responses", {})
        if target not in entries:
            raise FixtureMissError(
                f"fixture {self.source_id!r} has no response for {target}. "
                f"Recorded URLs: {len(entries)}. Re-record with "
                f"`uv run python -m scripts.record_fixtures --source {self.source_id}`."
            )
        entry = entries[target]
        return HttpResponse(
            url=target,
            status_code=int(entry.get("status", 200)),
            text=str(entry.get("body", "")),
            headers=dict(entry.get("headers", {})),
        )


class RecordingHttpClient:
    """Wraps a real client and captures every response, for the fixture recorder.

    Not used by the pipeline and never by a test: recording is a deliberate, manual step
    (`scripts/record_fixtures.py`), because it is the only thing in this project that is
    allowed to touch the real world outside a nightly run.
    """

    def __init__(self, inner: Any, *, source_id: str) -> None:
        self._inner = inner
        self.source_id = source_id
        self.responses: dict[str, dict[str, Any]] = {}
        self.requested: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        response: HttpResponse = self._inner.get(url, params=params, headers=headers)
        self.requested.append(response.url)
        self.responses.setdefault(
            response.url,
            {
                "status": response.status_code,
                "headers": {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in {"content-type", "retry-after"}
                },
                "body": response.text,
            },
        )
        return response

    def fixture(self, *, recorded_at: str) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "recorded_at": recorded_at,
            "responses": self.responses,
        }
