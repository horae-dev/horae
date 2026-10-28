# SPDX-License-Identifier: Apache-2.0
"""What is specific to the `openai-compatible` and `ollama` backends (#30); the shared suite
(`tests/test_llm_backends_shared.py`) covers everything they have in common with `anthropic`.
No network: recorded synthetic responses only."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from horae.contracts.instance import ModelAlias
from horae.engine import LlmError
from horae.llm import Completion, CompletionResult
from horae.llm.backends import FACTORIES, OllamaBackend, OpenAICompatibleBackend
from horae.llm.backends.openai_compatible import SCHEMA_NAME
from horae.secrets import SecretResolver
from llm_backend_kinds import DECLARED_VALUE, OLLAMA, OPENAI, Kind, fixture

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://example.invalid/brief.json",
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}
PLAIN = {key: value for key, value in SCHEMA.items() if not key.startswith("$")}
COMPLETION = Completion("fixture-model-1", "Do the thing.", {"a": 1}, SCHEMA, 100, 30.0)


@pytest.fixture
def secrets(tmp_path: Path) -> SecretResolver:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "LLM_API_KEY").write_text(DECLARED_VALUE, encoding="utf-8")
    return SecretResolver({}, directory, environ={})


def run(
    kind: Kind, secrets: SecretResolver, respond: Any, **spec: Any
) -> tuple[CompletionResult | LlmError, list[httpx2.Request]]:
    recorder = kind.recorder(respond)
    backend = kind.backend("default", kind.alias(**spec), secrets, recorder)

    async def go() -> CompletionResult:
        try:
            result: CompletionResult = await backend.complete(COMPLETION)
            return result
        finally:
            await backend.aclose()

    try:
        return asyncio.run(go()), recorder.requests
    except LlmError as error:
        return error, recorder.requests


def body_of(kind: Kind, change: Any) -> Any:
    def respond(request: httpx2.Request) -> httpx2.Response:
        _, body = fixture(kind.name, "success")
        change(body)
        return httpx2.Response(200, json=body)

    return respond


def failed(result: Any) -> LlmError:
    assert isinstance(result, LlmError), result
    return result


# --- the factories and the contract ---------------------------------------------------------


def test_the_factories_build_the_backends(secrets: SecretResolver) -> None:
    openai = FACTORIES["openai-compatible"]("a", OPENAI.alias(), secrets)
    ollama = FACTORIES["ollama"]("b", OLLAMA.alias(), secrets)
    assert isinstance(openai, OpenAICompatibleBackend)
    assert isinstance(ollama, OllamaBackend)
    assert (openai.name, ollama.name) == ("openai-compatible", "ollama")


def test_structured_output_is_for_openai_compatible_only() -> None:
    for mode in ("json_schema", "json_object", "none"):
        assert OPENAI.alias(structured_output=mode).structured_output == mode
    for kind in ("ollama", "anthropic"):
        with pytest.raises(ValidationError, match="takes no `structured_output`"):
            ModelAlias.model_validate(
                {
                    "backend": kind,
                    "model": "m",
                    "base_url": "https://llm.internal.test",
                    "secret": "LLM_API_KEY",
                    "structured_output": "json_object",
                }
            )
    with pytest.raises(ValidationError):
        OPENAI.alias(structured_output="xml")


@pytest.mark.parametrize(
    "url", ["http://llm.example.org/v1", "http://8.8.8.8:11434", "http://ollama.example.org"]
)
def test_plain_http_to_a_public_host_is_refused(url: str) -> None:
    for backend in ("openai-compatible", "ollama"):
        with pytest.raises(ValidationError):
            ModelAlias(backend=backend, model="m", base_url=url)


def test_a_key_is_optional_and_then_no_authorization_is_sent(tmp_path: Path) -> None:
    empty = SecretResolver({}, tmp_path, environ={})
    for kind in (OPENAI, OLLAMA):
        result, sent = run(kind, empty, "success", secret=None)
        assert isinstance(result, CompletionResult)
        assert "authorization" not in sent[0].headers
        assert set(sent[0].headers) == {
            "host",
            "accept",
            "content-type",
            "content-length",
            "user-agent",
        }
        assert empty.resolved() == ()


def test_the_base_url_path_is_kept(secrets: SecretResolver) -> None:
    _, sent = run(OPENAI, secrets, "success", base_url="https://gw.internal.test:8443/api/v1/")
    assert str(sent[0].url) == "https://gw.internal.test:8443/api/v1/chat/completions"
    assert sent[0].headers["host"] == "gw.internal.test:8443"
    _, sent = run(OLLAMA, secrets, "success", base_url="http://10.0.0.5:11434/ollama")
    assert str(sent[0].url) == "http://10.0.0.5:11434/ollama/api/chat"


def test_the_sent_bytes_are_the_canonical_rendered_body(secrets: SecretResolver) -> None:
    for kind in (OPENAI, OLLAMA):
        backend = kind.backend("default", kind.alias(), secrets, kind.recorder("success"))
        payload = backend.render(COMPLETION)
        _, sent = run(kind, secrets, "success")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert sent[0].content == canonical.encode()


# --- openai-compatible ---------------------------------------------------------------------


def test_openai_json_schema_structured_output(secrets: SecretResolver) -> None:
    backend = OPENAI.backend("default", OPENAI.alias(), secrets, OPENAI.recorder("success"))
    payload = backend.render(COMPLETION)
    assert payload["stream"] is False
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": PLAIN, "strict": False},
    }
    assert "JSON Schema" not in payload["messages"][0]["content"]


@pytest.mark.parametrize("mode", ["json_object", "none"])
def test_openai_fallback_modes_put_the_schema_in_the_system_prompt(
    secrets: SecretResolver, mode: str
) -> None:
    result, sent = run(OPENAI, secrets, "success", structured_output=mode)
    assert isinstance(result, CompletionResult)
    body = json.loads(sent[0].content)
    if mode == "json_object":
        assert body["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in body
    system = body["messages"][0]["content"]
    assert system.endswith("The output JSON Schema:\n" + json.dumps(PLAIN, sort_keys=True))


def test_openai_a_payload_of_another_mode_is_refused(secrets: SecretResolver) -> None:
    recorder = OPENAI.recorder("success")
    schema_mode = OPENAI.backend("a", OPENAI.alias(), secrets, recorder)
    none_mode = OPENAI.backend("b", OPENAI.alias(structured_output="none"), secrets, recorder)
    with pytest.raises(LlmError) as info:
        asyncio.run(none_mode.send(schema_mode.render(COMPLETION), 30.0))
    assert info.value.kind == "invalid_request"
    assert recorder.requests == []


def test_openai_cached_tokens_are_recorded(secrets: SecretResolver) -> None:
    result, _ = run(OPENAI, secrets, "success_cached")
    assert isinstance(result, CompletionResult)
    usage = result.usage
    assert (usage.tokens_in, usage.cache_read_tokens, usage.tokens_out) == (200, 1000, 10)
    assert usage.usd == pytest.approx((200 * 3 + 1000 * 3 + 10 * 15) / 1e6)


def test_openai_inconsistent_cached_tokens_are_ignored(secrets: SecretResolver) -> None:
    def odd(body: Any) -> None:
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": 5000}

    result, _ = run(OPENAI, secrets, body_of(OPENAI, odd))
    assert isinstance(result, CompletionResult)
    assert (result.usage.tokens_in, result.usage.cache_read_tokens) == (1200, None)


def test_openai_content_filter_is_a_refusal(secrets: SecretResolver) -> None:
    error = failed(run(OPENAI, secrets, "content_filter")[0])
    assert error.kind == "output_invalid"
    assert "the model refused (content_filter)" in error.message
    assert error.usage is not None


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (lambda body: body.update(choices=[]), "exactly one choice"),
        (lambda body: body.update(choices=[body["choices"][0]] * 2), "exactly one choice"),
        (lambda body: body.update(choices=["x"]), "exactly one choice"),
        (lambda body: body["choices"][0].update(message=None), "has no message"),
        (lambda body: body["choices"][0].update(finish_reason="eos"), "finish reason eos"),
        (lambda body: body["choices"][0].update(finish_reason=None), "finish reason unknown"),
        (lambda body: body["choices"][0]["message"].update(content=None), "not one JSON"),
        (
            lambda body: body["choices"][0]["message"].update(function_call={"name": "f"}),
            "tool call",
        ),
        (lambda body: body["choices"][0].update(finish_reason="model_length"), "truncated"),
    ],
)
def test_openai_unusable_responses(secrets: SecretResolver, change: Any, detail: str) -> None:
    error = failed(run(OPENAI, secrets, body_of(OPENAI, change))[0])
    assert error.kind == "output_invalid"
    assert detail in error.message
    assert error.usage is not None


def test_openai_a_non_object_body_is_output_invalid(secrets: SecretResolver) -> None:
    for content in (b"<html>gateway</html>", b"[1, 2]"):

        def respond(request: httpx2.Request, content: bytes = content) -> httpx2.Response:
            return httpx2.Response(200, content=content)

        error = failed(run(OPENAI, secrets, respond)[0])
        assert error.kind == "output_invalid"
        assert "not one JSON object" in error.message
        assert error.usage is not None
        assert error.usage.tokens_out == 100  # charged conservatively


def test_an_error_body_without_a_type_gives_the_status_only(secrets: SecretResolver) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(418, json={"error": {"code": 418, "message": "Synthetic: teapot"}})

    error = failed(run(OPENAI, secrets, respond)[0])
    assert (error.kind, error.message) == (
        "host_error",
        "the Chat Completions API answered HTTP 418",
    )


# --- ollama ----------------------------------------------------------------------------------


def test_ollama_native_request(secrets: SecretResolver) -> None:
    backend = OLLAMA.backend("default", OLLAMA.alias(), secrets, OLLAMA.recorder("success"))
    payload = backend.render(COMPLETION)
    assert payload["format"] == PLAIN
    assert payload["stream"] is False
    assert payload["options"] == {"num_predict": 100}
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


def test_ollama_without_a_price_costs_nothing(secrets: SecretResolver) -> None:
    result, _ = run(OLLAMA, secrets, "success", price=None)
    assert isinstance(result, CompletionResult)
    assert (result.usage.tokens_in, result.usage.tokens_out) == (1200, 45)
    assert (result.usage.usd, result.usage.priced) == (0.0, False)


def test_ollama_without_a_done_reason_is_accepted(secrets: SecretResolver) -> None:
    result, _ = run(OLLAMA, secrets, body_of(OLLAMA, lambda body: body.pop("done_reason")))
    assert isinstance(result, CompletionResult)
    assert result.stop_reason is None


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (lambda body: body.update(done=False), "not a complete message"),
        (lambda body: body.update(message="x"), "not a complete message"),
        (lambda body: body.update(done_reason="unload"), "done reason unload"),
        (lambda body: body.update(done_reason="Weird Reason"), "done reason unknown"),
        (lambda body: body["message"].update(content=7), "not one JSON value"),
    ],
)
def test_ollama_unusable_responses(secrets: SecretResolver, change: Any, detail: str) -> None:
    error = failed(run(OLLAMA, secrets, body_of(OLLAMA, change))[0])
    assert error.kind == "output_invalid"
    assert detail in error.message


def test_ollama_a_missing_model_is_an_invalid_request(secrets: SecretResolver) -> None:
    error = failed(run(OLLAMA, secrets, "invalid_request")[0])
    assert (error.kind, error.message) == ("invalid_request", "the Ollama API answered HTTP 404")
