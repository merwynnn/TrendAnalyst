"""HTTP client tests: the egress allowlist, retries, and fixture replay.

No test here touches the network: `HttpxClient` is driven through `httpx.MockTransport`,
and everything else replays a committed fixture. Sleeps are injected, so the retry paths
are verified by counting rather than waiting.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from trend_analyst.net import (
    EgressDeniedError,
    FixtureMissError,
    HttpFetchError,
    HttpPolicy,
    HttpxClient,
    RecordedHttpClient,
    RecordingHttpClient,
    check_egress,
    fixture_path,
    load_fixture,
    request_url,
)
from trend_analyst.sources.base import HttpResponse

URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
DOMAINS = ("hacker-news.firebaseio.com",)
POLICY = HttpPolicy(max_attempts=3, backoff_base_s=1.0, backoff_factor=2.0, max_backoff_s=10.0)


class Sleeper:
    """Records requested waits instead of sleeping them."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def json_transport(status: int, body: str = "{}") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, request=request)

    return transport(handler)


# ---------------------------------------------------------------------------
# The egress allowlist (spec §10)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://hacker-news.firebaseio.com/v0/topstories.json", True),
        ("https://HACKER-NEWS.FIREBASEIO.COM/v0/x.json", True),
        ("https://hacker-news.firebaseio.com./v0/x.json", True),
        ("https://evil.test/v0/x.json", False),
        ("https://hacker-news.firebaseio.com.evil.test/v0/x.json", False),
        ("ftp://hacker-news.firebaseio.com/x", False),
        ("file:///etc/passwd", False),
        ("https:///no-host", False),
    ],
)
def test_egress_allowlist(url: str, allowed: bool) -> None:
    if allowed:
        assert check_egress("hn_firebase", DOMAINS, url) == "hacker-news.firebaseio.com"
    else:
        with pytest.raises(EgressDeniedError):
            check_egress("hn_firebase", DOMAINS, url)


def test_denied_message_says_how_to_fix_it() -> None:
    with pytest.raises(EgressDeniedError) as excinfo:
        check_egress("shopify_public", ("*.myshopify.com",), "https://data.api.walmart.com/x")

    message = str(excinfo.value)
    assert "shopify_public" in message
    assert "data.api.walmart.com" in message
    assert "config/sources.yaml" in message


def test_wildcard_allows_subdomains_only() -> None:
    assert check_egress("s", ("*.myshopify.com",), "https://shop.myshopify.com/x")
    with pytest.raises(EgressDeniedError):
        check_egress("s", ("*.myshopify.com",), "https://myshopify.com/x")


def test_denied_url_is_never_requested() -> None:
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, text="{}", request=request)

    client = HttpxClient(source_id="s", allowed_domains=DOMAINS, transport=transport(handler))
    with pytest.raises(EgressDeniedError):
        client.get("https://evil.test/steal")

    assert called == [], "the allowlist must be checked before the request goes out"


# ---------------------------------------------------------------------------
# URLs and retries
# ---------------------------------------------------------------------------
def test_request_url_folds_and_sorts_params() -> None:
    assert request_url(URL) == URL
    assert request_url(URL, {"b": "2", "a": "1"}) == f"{URL}?a=1&b=2"
    assert (
        request_url("https://x.test/p?keep=1", {"a": "1"}) == "https://x.test/p?keep=1&a=1"
    )


def test_single_successful_request() -> None:
    client = HttpxClient(
        source_id="hn_firebase",
        allowed_domains=DOMAINS,
        policy=POLICY,
        sleep=Sleeper(),
        transport=json_transport(200, '{"ok": true}'),
    )
    response = client.get(URL)

    assert response.status_code == 200
    assert response.text == '{"ok": true}'
    assert response.url == URL


def test_retries_5xx_then_succeeds() -> None:
    attempts: list[str] = []
    sleeper = Sleeper()
    retries: list[tuple[str, int, float, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        if len(attempts) < 3:
            return httpx.Response(503, text="unavailable", request=request)
        return httpx.Response(200, text="{}", request=request)

    client = HttpxClient(
        source_id="s",
        allowed_domains=DOMAINS,
        policy=POLICY,
        sleep=sleeper,
        transport=transport(handler),
        on_retry=lambda url, attempt, wait, detail: retries.append((url, attempt, wait, detail)),
    )
    response = client.get(URL)

    assert response.status_code == 200
    assert len(attempts) == 3
    assert sleeper.waits == [1.0, 2.0], "exponential backoff"
    assert [retry[1] for retry in retries] == [1, 2]
    assert "HTTP 503" in retries[0][3]


def test_retries_are_bounded() -> None:
    sleeper = Sleeper()
    client = HttpxClient(
        source_id="s", allowed_domains=DOMAINS, policy=POLICY, sleep=sleeper,
        transport=json_transport(500),
    )

    with pytest.raises(HttpFetchError) as excinfo:
        client.get(URL)

    error = excinfo.value
    assert error.attempts == 3
    assert error.last_status == 500
    assert len(sleeper.waits) == 2, "no sleep after the final attempt"


def test_backoff_is_capped() -> None:
    policy = HttpPolicy(max_attempts=5, backoff_base_s=1.0, backoff_factor=10.0, max_backoff_s=5.0)
    assert [policy.backoff_for(attempt) for attempt in range(1, 5)] == [1.0, 5.0, 5.0, 5.0]


def test_429_is_returned_without_retrying() -> None:
    """Spec §4.3: the budget backs off, not the client."""
    attempts: list[str] = []
    sleeper = Sleeper()

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(429, text="slow down", headers={"Retry-After": "30"}, request=request)

    client = HttpxClient(
        source_id="s", allowed_domains=DOMAINS, policy=POLICY, sleep=sleeper,
        transport=transport(handler),
    )
    response = client.get(URL)

    assert response.status_code == 429
    assert response.retry_after_s == 30.0
    assert len(attempts) == 1, "429 must not be retried blindly"
    assert sleeper.waits == []


def test_client_errors_are_returned_without_retrying() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(404, text="not found", request=request)

    client = HttpxClient(
        source_id="s", allowed_domains=DOMAINS, policy=POLICY, sleep=Sleeper(),
        transport=transport(handler),
    )
    assert client.get(URL).status_code == 404
    assert len(attempts) == 1


def test_transport_errors_are_retried() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text="{}", request=request)

    client = HttpxClient(
        source_id="s", allowed_domains=DOMAINS, policy=POLICY, sleep=Sleeper(),
        transport=transport(handler),
    )
    assert client.get(URL).status_code == 200
    assert len(attempts) == 2


def test_timeouts_are_retried_then_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = HttpxClient(
        source_id="s", allowed_domains=DOMAINS, policy=POLICY, sleep=Sleeper(),
        transport=transport(handler),
    )
    with pytest.raises(HttpFetchError) as excinfo:
        client.get(URL)

    assert "giving up" in str(excinfo.value)
    assert excinfo.value.last_status is None


def test_client_is_a_context_manager() -> None:
    with HttpxClient(
        source_id="s", allowed_domains=DOMAINS, sleep=Sleeper(), transport=json_transport(200)
    ) as client:
        assert client.get(URL).status_code == 200


# ---------------------------------------------------------------------------
# Fixture replay
# ---------------------------------------------------------------------------
def write_fixture(tmp_path: Path, responses: dict[str, object], **extra: object) -> Path:
    path = tmp_path / "hn_firebase.json"
    path.write_text(
        json.dumps(
            {
                "source_id": "hn_firebase",
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "responses": responses,
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_recorded_client_replays_and_logs_requests(tmp_path: Path) -> None:
    path = write_fixture(
        tmp_path,
        {URL: {"status": 200, "headers": {"content-type": "application/json"}, "body": "[1,2,3]"}},
    )
    client = RecordedHttpClient.from_path(path, allowed_domains=DOMAINS)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.text == "[1,2,3]"
    assert client.requests == [URL]
    assert client.recorded_urls == (URL,)


def test_recorded_client_folds_params_when_matching(tmp_path: Path) -> None:
    target = request_url("https://api.example.com/posts", {"limit": "10", "sort": "desc"})
    path = write_fixture(tmp_path, {target: {"status": 200, "body": "{}"}})
    client = RecordedHttpClient.from_path(path, allowed_domains=("api.example.com",))

    response = client.get(
        "https://api.example.com/posts", params={"sort": "desc", "limit": "10"}
    )
    assert response.status_code == 200


def test_recorded_client_reports_a_miss_loudly(tmp_path: Path) -> None:
    path = write_fixture(tmp_path, {URL: {"status": 200, "body": "[]"}})
    client = RecordedHttpClient.from_path(path, allowed_domains=DOMAINS)

    with pytest.raises(FixtureMissError) as excinfo:
        client.get("https://hacker-news.firebaseio.com/v0/item/1.json")

    assert "record_fixtures" in str(excinfo.value)


def test_recorded_client_enforces_the_allowlist_first(tmp_path: Path) -> None:
    path = write_fixture(tmp_path, {"https://evil.test/x": {"status": 200, "body": "[]"}})
    client = RecordedHttpClient.from_path(path, allowed_domains=DOMAINS)

    with pytest.raises(EgressDeniedError):
        client.get("https://evil.test/x")
    assert client.requests == []


def test_missing_fixture_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FixtureMissError, match="no recorded fixture"):
        load_fixture(tmp_path / "absent.json")


def test_fixture_without_responses_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"source_id": "x"}', encoding="utf-8")
    with pytest.raises(FixtureMissError, match="expected an object with 'responses'"):
        load_fixture(path)


def test_fixture_path_is_per_source(tmp_path: Path) -> None:
    assert fixture_path(tmp_path, "hn_firebase") == tmp_path / "http" / "hn_firebase.json"


# ---------------------------------------------------------------------------
# Recording (used only by the manual recorder)
# ---------------------------------------------------------------------------
class StubClient:
    """A stand-in for the real client, so the recorder's logic is testable offline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        target = request_url(url, params)
        self.calls.append(target)
        return HttpResponse(
            url=target,
            status_code=200,
            text='{"data": []}',
            headers={"content-type": "application/json", "set-cookie": "secret-session"},
        )


def test_recording_client_captures_and_dedupes() -> None:
    recorder = RecordingHttpClient(StubClient(), source_id="hn_firebase")

    recorder.get(URL)
    recorder.get(URL)  # the same URL twice is recorded once
    recorder.get("https://hacker-news.firebaseio.com/v0/item/1.json")

    fixture = recorder.fixture(recorded_at="2026-01-01T00:00:00+00:00")
    assert set(fixture["responses"]) == {URL, "https://hacker-news.firebaseio.com/v0/item/1.json"}
    assert len(recorder.requested) == 3
    assert fixture["responses"][URL]["body"] == '{"data": []}'
    assert fixture["responses"][URL]["headers"] == {"content-type": "application/json"}
    assert "set-cookie" not in fixture["responses"][URL]["headers"], (
        "only content-type and retry-after are kept: a fixture must never carry a session"
    )
