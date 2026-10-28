# SPDX-License-Identifier: Apache-2.0
"""What the shared backend conformance suite (`tests/test_llm_backends_shared.py`) knows about
each provider backend: its alias, endpoint, headers, payload keys, the ambient variables it
must ignore and how its recorded responses (`tests/fixtures/llm/<backend>/`) are shaped.

No network, ever: every client gets an `httpx2.MockTransport` (`Recorder`) that answers from
the synthetic fixtures and records each outgoing request."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from horae.contracts.instance import ModelAlias
from horae.llm.backends import AnthropicBackend, OllamaBackend, OpenAICompatibleBackend
from horae.llm.backends.anthropic import ANTHROPIC_VERSION
from horae.secrets import SecretResolver

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
DECLARED_VALUE = "sentinel-value-for-the-declared-secret"
"""The declared secret's (synthetic) value."""
AMBIENT = "ambient-value-never-sent"
PRICE = {"input_per_mtok": 3.0, "output_per_mtok": 15.0}


class Recorder:
    """A mock transport answering `responses` in order (the last one repeats); a string is a
    fixture name of `directory`."""

    def __init__(
        self, directory: str, *responses: str | Callable[[httpx2.Request], httpx2.Response]
    ) -> None:
        self.directory = directory
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if callable(answer):
            return answer(request)
        status, body = fixture(self.directory, answer)
        return httpx2.Response(status, json=body, headers={"request-id": "req_01Synthetic"})

    def body(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].content)


def fixture(directory: str, name: str) -> tuple[int, Any]:
    data = json.loads((FIXTURES / directory / f"{name}.json").read_text(encoding="utf-8"))
    return data["status"], data["body"]


Backend = AnthropicBackend | OpenAICompatibleBackend | OllamaBackend


@dataclass(frozen=True)
class Kind:
    """One provider backend, as the shared suite sees it."""

    name: str
    cls: Callable[..., Backend]
    spec: Mapping[str, Any]
    """The alias (without `price`)."""
    url: str
    """The only URL a request goes to."""
    headers: Mapping[str, str]
    """The exact headers sent, but `content-length` (with the declared secret)."""
    keys: frozenset[str]
    ambient: Mapping[str, str]
    """Provider variables that must be ignored."""
    system: Callable[[Any], str]
    user: Callable[[Any], str]
    max_tokens: Callable[[Any], int]
    set_text: Callable[[Any, str], None]
    """Replace the output text of a recorded response body."""
    bad_usage: Callable[[Any], None]
    """Make the usage of a recorded response body unusable."""
    set_usage: Callable[[Any, Any, Any], None]
    """Set the reported `(input, output)` token counts of a recorded response body."""
    retarget: Callable[[Any, str], None]
    """Point the backend's client at another URL (a tampered request)."""
    client: Callable[[Any], httpx2.AsyncClient]
    error_types: Mapping[str, str] = field(default_factory=dict)
    """The API error type each error fixture's message carries, if any."""
    refusal: str | None = None
    """The detail of the refusal fixture's error, if the provider has one."""
    tool_fixture: str = "tool_call"
    tool_detail: str = "tool call"

    def alias(self, **overrides: Any) -> ModelAlias:
        data = {**self.spec, "price": PRICE, **overrides}
        return ModelAlias.model_validate({k: v for k, v in data.items() if v is not None})

    def backend(self, alias: str, spec: ModelAlias, secrets: SecretResolver, r: Recorder) -> Any:
        return self.cls(alias, spec, secrets, transport=r.transport)

    def recorder(self, *responses: str | Callable[[httpx2.Request], httpx2.Response]) -> Recorder:
        return Recorder(self.name, *responses)


def _anthropic_text(body: Any, text: str) -> None:
    body["content"] = [{"type": "text", "text": text}]


def _anthropic_bad_usage(body: Any) -> None:
    body["usage"] = {"input_tokens": "1200", "output_tokens": 45}


def _anthropic_retarget(backend: Any, url: str) -> None:
    backend._sdk().base_url = httpx2.URL(url)


def _anthropic_usage(body: Any, tokens_in: Any, tokens_out: Any) -> None:
    body["usage"] = {"input_tokens": tokens_in, "output_tokens": tokens_out}


def _openai_usage(body: Any, tokens_in: Any, tokens_out: Any) -> None:
    body["usage"] = {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}


def _ollama_usage(body: Any, tokens_in: Any, tokens_out: Any) -> None:
    body["prompt_eval_count"], body["eval_count"] = tokens_in, tokens_out


def _openai_text(body: Any, text: str) -> None:
    body["choices"][0]["message"]["content"] = text


def _openai_bad_usage(body: Any) -> None:
    body["usage"] = {"prompt_tokens": "1200", "completion_tokens": 45}


def _ollama_text(body: Any, text: str) -> None:
    body["message"]["content"] = text


def _ollama_bad_usage(body: Any) -> None:
    body["prompt_eval_count"] = True


def _direct_retarget(backend: Any, url: str) -> None:
    backend._url = httpx2.URL(url)


def _chat_system(body: Any) -> str:
    message = body["messages"][0]
    assert message["role"] == "system"
    return str(message["content"])


def _chat_user(body: Any) -> str:
    assert len(body["messages"]) == 2
    message = body["messages"][1]
    assert message["role"] == "user"
    return str(message["content"])


ANTHROPIC = Kind(
    name="anthropic",
    cls=AnthropicBackend,
    spec={"backend": "anthropic", "model": "claude-fixture-1", "secret": "ANTHROPIC_API_KEY"},
    url="https://api.anthropic.com/v1/messages",
    headers={
        "host": "api.anthropic.com",
        "accept": "application/json",
        "content-type": "application/json",
        "accept-encoding": "identity",
        "anthropic-version": ANTHROPIC_VERSION,
        "user-agent": "horae",
        "x-api-key": DECLARED_VALUE,
    },
    keys=frozenset({"model", "max_tokens", "system", "messages", "output_config"}),
    ambient={
        "ANTHROPIC_API_KEY": AMBIENT,
        "ANTHROPIC_AUTH_TOKEN": AMBIENT,
        "ANTHROPIC_BASE_URL": "https://ambient.invalid",
        "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {AMBIENT}\nX-Extra: 1",
    },
    system=lambda body: str(body["system"]),
    user=lambda body: "\n\n".join(block["text"] for block in body["messages"][0]["content"]),
    max_tokens=lambda body: int(body["max_tokens"]),
    set_text=_anthropic_text,
    bad_usage=_anthropic_bad_usage,
    set_usage=_anthropic_usage,
    retarget=_anthropic_retarget,
    client=lambda backend: backend._sdk()._client,
    error_types={
        "rate_limited": "rate_limit_error",
        "auth_failed": "authentication_error",
        "server_error": "api_error",
        "overloaded": "overloaded_error",
        "invalid_request": "invalid_request_error",
    },
    refusal="the model refused (general_harms)",
    tool_fixture="tool_use",
    tool_detail="unexpected stop reason tool_use",
)

OPENAI = Kind(
    name="openai-compatible",
    cls=OpenAICompatibleBackend,
    spec={
        "backend": "openai-compatible",
        "model": "fixture-model-1",
        "base_url": "https://llm-gateway.internal.test/v1",
        "secret": "LLM_API_KEY",
    },
    url="https://llm-gateway.internal.test/v1/chat/completions",
    headers={
        "host": "llm-gateway.internal.test",
        "accept": "application/json",
        "content-type": "application/json",
        "accept-encoding": "identity",
        "user-agent": "horae",
        "authorization": f"Bearer {DECLARED_VALUE}",
    },
    keys=frozenset({"model", "max_tokens", "stream", "messages", "response_format"}),
    ambient={
        "OPENAI_API_KEY": AMBIENT,
        "OPENAI_BASE_URL": "https://ambient.invalid/v1",
        "OPENAI_ORG_ID": AMBIENT,
        "OPENAI_PROJECT_ID": AMBIENT,
        "OPENROUTER_API_KEY": AMBIENT,
        "MISTRAL_API_KEY": AMBIENT,
    },
    system=_chat_system,
    user=_chat_user,
    max_tokens=lambda body: int(body["max_tokens"]),
    set_text=_openai_text,
    bad_usage=_openai_bad_usage,
    set_usage=_openai_usage,
    retarget=_direct_retarget,
    client=lambda backend: backend._http(),
    error_types={
        "rate_limited": "rate_limit_error",
        "auth_failed": "invalid_request_error",
        "server_error": "server_error",
        "overloaded": "service_unavailable",
        "invalid_request": "invalid_request_error",
    },
    refusal="the model refused",
)

OLLAMA = Kind(
    name="ollama",
    cls=OllamaBackend,
    spec={
        "backend": "ollama",
        "model": "fixture-model-1",
        "base_url": "http://ollama:11434",
        "secret": "LLM_API_KEY",
    },
    url="http://ollama:11434/api/chat",
    headers={
        "host": "ollama:11434",
        "accept": "application/json",
        "content-type": "application/json",
        "accept-encoding": "identity",
        "user-agent": "horae",
        "authorization": f"Bearer {DECLARED_VALUE}",
    },
    keys=frozenset({"model", "stream", "messages", "format", "options"}),
    ambient={
        "OLLAMA_HOST": "http://ambient.invalid:11434",
        "OLLAMA_API_KEY": AMBIENT,
        "OLLAMA_ORIGINS": "*",
    },
    system=_chat_system,
    user=_chat_user,
    max_tokens=lambda body: int(body["options"]["num_predict"]),
    set_text=_ollama_text,
    bad_usage=_ollama_bad_usage,
    set_usage=_ollama_usage,
    retarget=_direct_retarget,
    client=lambda backend: backend._http(),
)

KINDS = (ANTHROPIC, OPENAI, OLLAMA)
