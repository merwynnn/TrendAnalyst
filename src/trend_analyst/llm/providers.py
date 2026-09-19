"""Real provider transports: the concrete `Sender` the gateway calls (brief P3).

One function per provider family, and one factory that turns settings into a `Sender`. Two
design points, both of them lessons from this project:

* **A missing key is an error, not a skip.** Groq has no key configured here, and that is
  *useful*: the chain must fail over from "provider not configured" exactly as it does from a
  503. A sender that quietly returned an empty answer would hide a missing credential behind a
  schema violation.
* **Every request goes through the egress allowlist** (`net.check_egress`) with the provider
  hosts, because the specification's rule is about the code path, not about which module the
  URL was written in. An LLM gateway is still the network.

Provider quirks this file exists to absorb: Gemini nests the answer in
``candidates[0].content.parts[*].text`` and reports tokens as ``promptTokenCount`` /
``candidatesTokenCount``; the OpenAI-shaped APIs (Groq, Cerebras) use ``choices[0].message``
and ``usage.prompt_tokens``; Ollama uses ``message.content`` with ``prompt_eval_count`` /
``eval_count``. Four shapes, one `Sender` signature.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Final

import httpx

from trend_analyst.llm.gateway import ProviderSpec
from trend_analyst.net import check_egress

__all__ = ["PROVIDER_DOMAINS", "MissingCredentialError", "ProviderError", "settings_sender"]

#: Every host the gateway may reach. Keep in step with the providers below; the allowlist is
#: checked before the request, so a typo here fails loudly instead of reaching the wrong host.
PROVIDER_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "generativelanguage.googleapis.com",
        "api.groq.com",
        "api.cerebras.ai",
        "localhost",
        "127.0.0.1",
    }
)

_GEMINI_URL: Final = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_GROQ_URL: Final = "https://api.groq.com/openai/v1/chat/completions"
_CEREBRAS_URL: Final = "https://api.cerebras.ai/v1/chat/completions"

#: First HTTP status that counts as a failure worth failing over for.
_HTTP_ERROR: Final = 400


class ProviderError(RuntimeError):
    """The provider answered, and the answer was not usable."""


class MissingCredentialError(ProviderError):
    """No key for this provider: fail over, do not guess."""


def _secret(value: Any) -> str:
    """Read a key whether it arrived as a `SecretStr`, a plain string or None."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    text = str(getter() if callable(getter) else value)
    return "" if text in {"", "SET_ME", "None"} else text


def _post(
    url: str, *, headers: Mapping[str, str], body: Mapping[str, Any], timeout_s: float
) -> dict[str, Any]:
    """One POST, allowlisted and timeout-bounded. Raises `ProviderError` on any HTTP failure."""
    # `check_egress(source_id, allowed_domains, url)` -- the LLM gateway is a source of network
    # traffic like any other, so it passes the same allowlist check before every request.
    check_egress("llm_gateway", tuple(sorted(PROVIDER_DOMAINS)), url)
    try:
        response = httpx.post(url, headers=dict(headers), json=dict(body), timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport failure: {exc}") from exc
    if response.status_code >= _HTTP_ERROR:
        # The status and a bounded body slice: enough to debug a 429 or a bad model id, without
        # dumping a whole error page into the ledger.
        snippet = response.text[:240].replace("\n", " ")
        raise ProviderError(f"HTTP {response.status_code}: {snippet}")
    try:
        return dict(response.json())
    except ValueError as exc:
        raise ProviderError(f"answer was not JSON: {exc}") from exc


def _gemini(
    provider: ProviderSpec, prompt: str, *, api_key: str, timeout_s: float
) -> tuple[str, int, int]:
    url = _GEMINI_URL.format(model=provider.model) + f"?key={api_key}"
    payload = _post(
        url,
        headers={"content-type": "application/json"},
        body={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        },
        timeout_s=timeout_s,
    )
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(str(part.get("text", "")) for part in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"unexpected Gemini shape: {exc}") from exc
    usage = payload.get("usageMetadata") or {}
    return text, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0))


def _openai_shaped(
    provider: ProviderSpec, prompt: str, *, url: str, api_key: str, timeout_s: float
) -> tuple[str, int, int]:
    payload = _post(
        url,
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
        body={
            "model": provider.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        },
        timeout_s=timeout_s,
    )
    try:
        text = str(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"unexpected {provider.name} shape: {exc}") from exc
    usage = payload.get("usage") or {}
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def _ollama(
    provider: ProviderSpec, prompt: str, *, base_url: str, timeout_s: float
) -> tuple[str, int, int]:
    url = base_url.rstrip("/") + "/api/chat"
    payload = _post(
        url,
        headers={"content-type": "application/json"},
        body={
            "model": provider.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
        },
        timeout_s=timeout_s,
    )
    try:
        text = str(payload["message"]["content"])
    except (KeyError, TypeError) as exc:
        raise ProviderError(f"unexpected Ollama shape: {exc}") from exc
    return text, int(payload.get("prompt_eval_count", 0)), int(payload.get("eval_count", 0))


def settings_sender(
    settings: Any,
    *,
    timeout_s: float = 60.0,
    clients: Mapping[str, str] | None = None,
) -> Callable[[ProviderSpec, str], tuple[str, int, int]]:
    """Build the real `Sender` from settings.

    Args:
        settings: the loaded settings (only `llm` is read).
        clients: optional per-provider key override, used by the drill to force a failover
            without touching the credential file.
    """
    llm = settings.llm
    keys = {
        "gemini": _secret(getattr(llm, "gemini_api_key", None)),
        "groq": _secret(getattr(llm, "groq_api_key", None)),
        "cerebras": _secret(getattr(llm, "cerebras_api_key", None)),
        "ollama": "",
    }
    if clients:
        keys.update({str(name): str(value) for name, value in clients.items()})
    ollama_base = _secret(getattr(llm, "ollama_base_url", None)) or "http://127.0.0.1:11434"

    def send(provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        if provider.name == "ollama":
            return _ollama(provider, prompt, base_url=ollama_base, timeout_s=timeout_s)

        key = keys.get(provider.name, "")
        if not key:
            raise MissingCredentialError(f"no API key configured for {provider.name}")
        if provider.name == "gemini":
            return _gemini(provider, prompt, api_key=key, timeout_s=timeout_s)
        if provider.name == "groq":
            return _openai_shaped(
                provider, prompt, url=_GROQ_URL, api_key=key, timeout_s=timeout_s
            )
        if provider.name == "cerebras":
            return _openai_shaped(
                provider, prompt, url=_CEREBRAS_URL, api_key=key, timeout_s=timeout_s
            )
        raise ProviderError(f"no transport implemented for provider {provider.name!r}")

    return send


def render_prompt(payload: Mapping[str, Any], instructions: str) -> str:
    """One prompt from instructions plus a JSON payload — the shape every gate uses."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return instructions.strip() + "\n\nInput (JSON):\n" + body
