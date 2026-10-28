# SPDX-License-Identifier: Apache-2.0
"""The `anthropic` backend: the Anthropic Messages API with an API key (PLAN.md §3.4, #28).

One `ProviderBackend` per model alias (`backend: anthropic` in `horae.yaml`), built on the
official `anthropic` SDK (`AsyncAnthropic`, API version `2023-06-01`): its client posts the
rendered payload's exact canonical JSON bytes to `/v1/messages` and returns the raw response,
which Horae reads itself (the SDK maps the HTTP and connection errors).

**Render, then send** (`horae.llm.base`). `render(completion)` is pure: it builds the
exact `POST /v1/messages` body (no client, no secret, no I/O) and the same completion always
gives the same body. `send(payload, timeout_s)` posts exactly that payload: it refuses one
with any other key than `PAYLOAD_KEYS` (so never `tools`), resolves the secret and builds
the SDK client on its first call. `complete` is `send(render(...))`.

**The request.** `POST /v1/messages` with `model` (the alias' `model`), `max_tokens` (from the
step budget, `horae.llm.base.max_tokens`), `system` (Horae's `SYSTEM_PREAMBLE`, then the
skill's rendered prompt) and one user message: the untrusted input as JSON inside `<input>`
tags, followed, on the repair retry, by the previous attempt's schema errors as a JSON array
inside `<schema_errors>` tags. In both, every `<` is written as the JSON escape `\\u003c`, so
the data cannot close its tag; the preamble tells the model that both are data, never
instructions. **No tools**: `tools` and `tool_choice` are never sent. The output is
constrained to the skill's JSON Schema with structured outputs (`output_config.format`,
`{"type": "json_schema", "schema": ...}`), after `anthropic.transform_schema` (which moves
the constraints structured outputs do not support into descriptions; the full schema must
still be checked by the #31 validator). The text of the response must be one JSON value: no
`NaN` or infinite number (`1e999`), no duplicate key, not nested beyond the parser's limit.

**The API key comes from the declared secret only.** The alias' `secret` is resolved through
the instance's `SecretResolver` (decision 25) on the first call, never from
`ANTHROPIC_API_KEY` or any other ambient variable. The SDK is given a placeholder key and an
explicit `base_url` (so it reads neither `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, its
credential profiles nor `ANTHROPIC_BASE_URL`).

**Headers are rebuilt.** The SDK merges `ANTHROPIC_CUSTOM_HEADERS` from the environment into
every request (it can override any header, `Host` and `anthropic-version` included, or add an
`Authorization` header). A request hook on the HTTP client (`_shared.Guard`) therefore refuses
any request but `POST <base_url>/v1/messages` to the alias' origin, and replaces every header
with exactly: `host` (from the request URL), `accept`, `content-type`, `content-length` (the
body's length), `anthropic-version` (`ANTHROPIC_VERSION`), `user-agent` (`USER_AGENT`) and
`x-api-key` (the declared secret, put there at the last moment), plus `accept-encoding:
identity`. Nothing else is sent: no `x-stainless-*` platform or timeout header.

**The response is bounded and read strictly** (`_shared.BoundedTransport` wraps the SDK's
transport too): a body over `_shared.response_cap(max_tokens)` bytes once decoded (a gzip
bomb included) is `output_invalid`, an error body is cut at 64 KiB. The raw body must be
UTF-8 and one JSON object for `_shared.parse` (no `NaN`, duplicate key or excessive
nesting), read as plain JSON (no SDK model is built from it); anything the backend cannot
read is `output_invalid` with the conservative usage, and a reported usage is floored by the
request and output sizes (`_shared.floored`).

**The network path is explicit** (decision 38 C). The HTTP client ignores the ambient
environment: no `HTTP(S)_PROXY` / `ALL_PROXY`, no `.netrc`, no `SSL_CERT_FILE` /
`SSL_CERT_DIR` (`trust_env=False` on both the client and its transport; TLS verification
uses the system trust store through `httpcore2`'s default context, which reads no
`SSL_CERT_*`). Redirects are not followed; OpenTelemetry spans are off. A `base_url` must be
`https://` (the contract refuses `http://` for this backend). An explicit proxy or CA option
is a later decision.

**Logging.** The SDK logs request options (the prompt and the input) at `DEBUG` when
`ANTHROPIC_LOG=debug` or a logging configuration asks for it; building the client pins the
`anthropic`, `httpx2` and `httpcore2` loggers (and their children) to `WARNING`.

**Errors** are `LlmError`s whose messages Horae writes (the HTTP status and the API's error
`type`, never the SDK's or the API's message text), raised outside the SDK's exception
handler so that neither `__cause__` nor `__context__` leads back to the SDK error (whose
request holds the key): `timeout` (the SDK timeout, HTTP 408 or 504), `rate_limited` (429),
`auth_failed` (401, 403, or the secret cannot be resolved), `overloaded` (529, 503),
`invalid_request` (400, 404, 409, 413, 422, or a schema structured outputs cannot take),
`output_invalid` (a refusal, truncation at `max_tokens` or the context window, a tool call or
other non-text content, text that is not one acceptable JSON value; always with the usage the
response reported, or, if it reported none, a conservative one) and `host_error` (anything
else: a connection failure, a refused request, another status).

**Retries.** The SDK's retries are disabled (`max_retries=0`): the engine owns the single
repair retry, and the step's deadline bounds both attempts.
"""

import contextlib
from collections.abc import Mapping
from typing import Any

import anthropic
import httpx2
from anthropic.types import TextBlockParam

from horae.contracts.instance import ModelAlias
from horae.contracts.run import Usage
from horae.engine.canonical import canonical_json
from horae.engine.ports import LlmBackendName, LlmError
from horae.llm.backends._shared import (
    INVALID,
    LIMITS,
    RESPONSE_CAP,
    STATUS_KINDS,
    SYSTEM_PREAMBLE,
    USER_AGENT,
    Guard,
    count,
    floored,
    parse,
    plain_schema,
    quiet_loggers,
    refusal,
    response_cap,
    system_text,
    token,
    transport,
    unreadable,
    user_texts,
    utf8,
    utf8_size,
)
from horae.llm.base import Completion, CompletionResult, cost, input_bound
from horae.secrets import SecretError, SecretResolver

DEFAULT_BASE_URL = "https://api.anthropic.com"
"""The Anthropic API, when the alias has no `base_url`."""

ANTHROPIC_VERSION = "2023-06-01"
"""The `anthropic-version` header sent."""

MESSAGES_PATH = "/v1/messages"
"""The only path a request goes to (under `base_url`)."""

PLACEHOLDER_KEY = "horae-declared-secret"
"""What the SDK is given as its API key; the guard replaces it with the declared secret."""

SENT_HEADERS = (
    "host",
    "accept",
    "content-type",
    "content-length",
    "accept-encoding",
    "anthropic-version",
    "user-agent",
    "x-api-key",
)
"""The only request headers sent, each with a value the guard sets."""

PAYLOAD_KEYS = frozenset({"model", "max_tokens", "system", "messages", "output_config"})
"""The keys of a rendered payload: no `tools`, `tool_choice`, `thinking` or anything else."""

SDK_LOGGERS = ("anthropic", "httpx2", "httpcore2")
"""Loggers pinned to `WARNING` (with their children): at `DEBUG` they log request bodies."""


def quiet_sdk_loggers() -> None:
    """Pin `SDK_LOGGERS` and every child logger already created to at least `WARNING`."""
    quiet_loggers(SDK_LOGGERS)


def _x_api_key(key: str) -> tuple[str, str]:
    return ("x-api-key", key)


def _guard(base_url: str) -> Guard:
    return Guard(
        base_url,
        MESSAGES_PATH,
        auth=_x_api_key,
        fixed=(("anthropic-version", ANTHROPIC_VERSION),),
        api="the Messages API",
    )


def _get(value: Any, key: str) -> Any:
    """`value[key]` when `value` is a JSON object, else `None`."""
    return value.get(key) if isinstance(value, dict) else None


def _user_content(completion: Completion) -> list[TextBlockParam]:
    return [{"type": "text", "text": text} for text in user_texts(completion)]


def _schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    transformed: dict[str, Any] | None = None
    with contextlib.suppress(Exception):  # any shape transform_schema cannot handle
        transformed = anthropic.transform_schema(plain_schema(schema))
    if transformed is None:
        raise LlmError(
            "invalid_request", "the skill's output schema cannot be used for structured output"
        )
    return transformed


class AnthropicBackend:
    """The `anthropic` provider backend of one model alias (see the module docstring).

    `transport` replaces the HTTP transport (tests inject an `httpx2.MockTransport`; no
    network is used then)."""

    def __init__(
        self,
        alias: str,
        spec: ModelAlias,
        secrets: SecretResolver,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if spec.secret is None:  # pragma: no cover - ModelAlias requires it for anthropic
            raise ValueError("backend 'anthropic' requires `secret`")
        self._alias = alias
        self._spec = spec
        self._secret_name = spec.secret
        self._secrets = secrets
        self._base_url = spec.base_url or DEFAULT_BASE_URL
        self._transport = transport
        self._guard = _guard(self._base_url)
        self._client: anthropic.AsyncAnthropic | None = None

    def _sdk(self) -> anthropic.AsyncAnthropic:
        """The SDK client, built on the first `send` (`render` needs none)."""
        quiet_sdk_loggers()
        if self._client is None:
            http_client = anthropic.DefaultAsyncHttpxClient(
                transport=transport(self._base_url, self._transport),
                trust_env=False,
                follow_redirects=False,
                limits=LIMITS,
                event_hooks={"request": [self._guard]},
            )
            self._client = anthropic.AsyncAnthropic(
                api_key=PLACEHOLDER_KEY,
                base_url=self._base_url,
                max_retries=0,
                open_telemetry=False,
                http_client=http_client,
            )
        return self._client

    @property
    def name(self) -> LlmBackendName:
        return "anthropic"

    def _resolve_secret(self) -> None:
        if self._guard.secret is not None:
            return
        failure: LlmError | None = None
        try:
            self._guard.secret = self._secrets.resolve(self._secret_name)
        except SecretError as error:
            failure = LlmError("auth_failed", f"model alias {self._alias!r}: {error}")
        if failure is not None:
            raise failure

    def conservative(self, payload: Mapping[str, Any]) -> Usage:
        """The usage charged when the response reported none usable: the payload's input
        bound and the whole `max_tokens`."""
        return cost(self._spec.price, input_bound(payload), int(payload["max_tokens"]))

    def _usage(self, usage: Any, payload: Mapping[str, Any], output: str) -> Usage:
        """The reported usage, floored (`_shared.floored`), or, when it is not usable,
        `conservative`."""
        tokens_in = count(_get(usage, "input_tokens"))
        tokens_out = count(_get(usage, "output_tokens"))
        if tokens_in is None or tokens_out is None:
            return self.conservative(payload)
        return floored(
            self._spec.price,
            payload,
            tokens_in,
            tokens_out,
            output_bytes=utf8_size(output),
            max_tokens=int(payload["max_tokens"]),
            cache_read=count(_get(usage, "cache_read_input_tokens")),
            cache_write=count(_get(usage, "cache_creation_input_tokens")),
        )

    def _invalid(self, reason: str, payload: Mapping[str, Any]) -> LlmError:
        return LlmError(
            "output_invalid", f"the Anthropic API {reason}", usage=self.conservative(payload)
        )

    def _error(self, error: Exception, payload: Mapping[str, Any]) -> LlmError:
        if isinstance(error, anthropic.APITimeoutError):
            return LlmError("timeout", "the Anthropic API did not answer in time")
        if isinstance(error, anthropic.APIConnectionError):
            reason = refusal(error)
            if reason is not None:
                return LlmError("host_error", f"request refused: {reason}")
            reason = unreadable(error)
            if reason is not None:
                return self._invalid(f"answered with {reason}", payload)
            return LlmError("host_error", "cannot reach the Anthropic API")
        if isinstance(error, anthropic.APIStatusError):
            status = error.status_code
            kind = STATUS_KINDS.get(status, "host_error")
            detail = token(error.type)
            suffix = f" ({detail})" if detail else ""
            return LlmError(kind, f"the Anthropic API answered HTTP {status}{suffix}")
        return LlmError("host_error", f"Anthropic SDK error ({type(error).__name__})")

    def render(self, completion: Completion) -> dict[str, Any]:
        """The `POST /v1/messages` body for `completion` (pure; see the module docstring)."""
        return {
            "model": completion.model,
            "max_tokens": completion.max_tokens,
            "system": system_text(completion),
            "messages": [{"role": "user", "content": _user_content(completion)}],
            "output_config": {
                "format": {"type": "json_schema", "schema": _schema(completion.output_schema)}
            },
        }

    async def complete(self, completion: Completion) -> CompletionResult:
        """`send(render(completion))`."""
        return await self.send(self.render(completion), completion.timeout_s)

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        """Post exactly `payload` (a `render` result: no other key, so never `tools`)."""
        if set(payload) != PAYLOAD_KEYS:
            raise LlmError("invalid_request", "the payload is not one `render` made")
        self._resolve_secret()
        client = self._sdk()
        failure: LlmError | None = None
        response: httpx2.Response | None = None
        cap = RESPONSE_CAP.set(response_cap(int(payload["max_tokens"])))
        try:
            response = await client.post(
                MESSAGES_PATH,
                cast_to=httpx2.Response,
                content=canonical_json(payload),
                options={"timeout": timeout_s},
            )
        except anthropic.AnthropicError as error:
            failure = self._error(error, payload)
        finally:
            RESPONSE_CAP.reset(cap)
        if failure is not None or response is None:
            # Raised here, outside the handler: no `__context__` leads to the SDK error.
            raise failure or LlmError("host_error", "cannot reach the Anthropic API")
        text = utf8(response.content)
        if text is None:
            raise self._invalid("answered with a body that is not UTF-8", payload)
        data = parse(text)
        if not isinstance(data, dict):
            raise self._invalid("answered with a body that is not one JSON object", payload)
        result: CompletionResult | None = None
        try:
            result = self._result(data, payload)
        except LlmError:
            raise
        except Exception:  # any shape `_result` did not expect is refused, never a crash
            failure = self._invalid("answered with a response Horae cannot read", payload)
        if failure is not None or result is None:
            raise failure or self._invalid("answered with no result", payload)
        return result

    def _result(self, data: dict[str, Any], payload: Mapping[str, Any]) -> CompletionResult:
        content = data.get("content")
        blocks: list[Any] = content if isinstance(content, list) else []
        text = "".join(
            block["text"]
            for block in blocks
            if _get(block, "type") == "text" and isinstance(block.get("text"), str)
        )
        usage = self._usage(data.get("usage"), payload, text)
        stop = token(data.get("stop_reason"))

        def invalid(reason: str) -> LlmError:
            return LlmError("output_invalid", reason, usage=usage)

        if stop == "refusal":
            category = token(_get(data.get("stop_details"), "category"))
            raise invalid("the model refused" + (f" ({category})" if category else ""))
        if stop in ("max_tokens", "model_context_window_exceeded"):
            limit = payload["max_tokens"]
            raise invalid(f"the output was truncated ({stop}, max_tokens {limit})")
        if stop not in ("end_turn", "stop_sequence"):
            raise invalid(f"unexpected stop reason {stop or 'unknown'}")
        if not isinstance(content, list):
            raise invalid("the response has no content")
        for block in blocks:
            if _get(block, "type") != "text":
                kind = token(_get(block, "type")) or "unknown"
                raise invalid(f"the response has {kind} content (no tools are allowed)")
        output = parse(text)
        if output is INVALID:
            raise invalid("the response is not one JSON value")
        return CompletionResult(output=output, usage=usage, stop_reason=stop)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):  # closing must not fail a run
                await client.close()


def anthropic_backend(alias: str, spec: ModelAlias, secrets: SecretResolver) -> AnthropicBackend:
    """The `BackendFactory` of `anthropic`."""
    return AnthropicBackend(alias, spec, secrets)


__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_BASE_URL",
    "PAYLOAD_KEYS",
    "PLACEHOLDER_KEY",
    "SDK_LOGGERS",
    "SENT_HEADERS",
    "SYSTEM_PREAMBLE",
    "USER_AGENT",
    "AnthropicBackend",
    "anthropic_backend",
    "quiet_sdk_loggers",
]
