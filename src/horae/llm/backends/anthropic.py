# SPDX-License-Identifier: Apache-2.0
"""The `anthropic` backend: the Anthropic Messages API with an API key (PLAN.md §3.4, #28).

One `ProviderBackend` per model alias (`backend: anthropic` in `horae.yaml`), built on the
official `anthropic` SDK (`AsyncAnthropic`, `messages.create`, API version `2023-06-01`).

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
`x-api-key` (the declared secret, put there at the last moment). Nothing else is sent: no
`x-stainless-*` platform or timeout header, no `accept-encoding`.

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
from horae.engine.ports import LlmBackendName, LlmError
from horae.llm.backends._shared import (
    INVALID,
    LIMITS,
    STATUS_KINDS,
    SYSTEM_PREAMBLE,
    USER_AGENT,
    Guard,
    count,
    parse,
    plain_schema,
    quiet_loggers,
    refusal,
    system_text,
    token,
    user_texts,
)
from horae.llm.base import Completion, CompletionResult, cost, input_bound
from horae.secrets import SecretError, SecretResolver

DEFAULT_BASE_URL = "https://api.anthropic.com"
"""The Anthropic API, when the alias has no `base_url`."""

ANTHROPIC_VERSION = "2023-06-01"
"""The `anthropic-version` header sent."""

PLACEHOLDER_KEY = "horae-declared-secret"
"""What the SDK is given as its API key; the guard replaces it with the declared secret."""

SENT_HEADERS = (
    "host",
    "accept",
    "content-type",
    "content-length",
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
        "/v1/messages",
        auth=_x_api_key,
        fixed=(("anthropic-version", ANTHROPIC_VERSION),),
        api="the Messages API",
    )


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
            transport = self._transport
            if transport is None:
                transport = httpx2.AsyncHTTPTransport(
                    verify=True, trust_env=False, limits=LIMITS, retries=0
                )
            http_client = anthropic.DefaultAsyncHttpxClient(
                transport=transport,
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

    def _usage(self, usage: Any, payload: Mapping[str, Any]) -> Usage:
        """The reported usage, or, when it is not usable, a conservative one: the payload's
        input bound and the whole `max_tokens`."""
        tokens_in = count(getattr(usage, "input_tokens", None))
        tokens_out = count(getattr(usage, "output_tokens", None))
        if tokens_in is None or tokens_out is None:
            return cost(self._spec.price, input_bound(payload), int(payload["max_tokens"]))
        return cost(
            self._spec.price,
            tokens_in,
            tokens_out,
            cache_read=count(getattr(usage, "cache_read_input_tokens", None)),
            cache_write=count(getattr(usage, "cache_creation_input_tokens", None)),
        )

    def _error(self, error: Exception) -> LlmError:
        if isinstance(error, anthropic.APITimeoutError):
            return LlmError("timeout", "the Anthropic API did not answer in time")
        if isinstance(error, anthropic.APIConnectionError):
            reason = refusal(error)
            if reason is not None:
                return LlmError("host_error", f"request refused: {reason}")
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
        message: Any = None
        try:
            message = await client.messages.create(
                model=payload["model"],
                max_tokens=payload["max_tokens"],
                system=payload["system"],
                messages=payload["messages"],
                output_config=payload["output_config"],
                timeout=timeout_s,
            )
        except anthropic.AnthropicError as error:
            failure = self._error(error)
        if failure is not None:
            # Raised here, outside the handler: no `__context__` leads to the SDK error.
            raise failure
        return self._result(message, payload)

    def _result(self, message: Any, payload: Mapping[str, Any]) -> CompletionResult:
        usage = self._usage(getattr(message, "usage", None), payload)
        stop = token(getattr(message, "stop_reason", None))

        def invalid(reason: str) -> LlmError:
            return LlmError("output_invalid", reason, usage=usage)

        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            category = token(getattr(details, "category", None))
            raise invalid("the model refused" + (f" ({category})" if category else ""))
        if stop in ("max_tokens", "model_context_window_exceeded"):
            limit = payload["max_tokens"]
            raise invalid(f"the output was truncated ({stop}, max_tokens {limit})")
        if stop not in ("end_turn", "stop_sequence"):
            raise invalid(f"unexpected stop reason {stop or 'unknown'}")
        texts: list[str] = []
        for block in getattr(message, "content", None) or ():
            if getattr(block, "type", None) != "text":
                kind = token(getattr(block, "type", None)) or "unknown"
                raise invalid(f"the response has {kind} content (no tools are allowed)")
            texts.append(str(getattr(block, "text", "")))
        output = parse("".join(texts))
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
