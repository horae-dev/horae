# SPDX-License-Identifier: Apache-2.0
"""The `openai-compatible` backend: the Chat Completions API of OpenRouter, Mistral, vLLM and
other servers that speak it (PLAN.md §3.4, #30, schemas decision 40).

**A direct `httpx2` client, not the `openai` SDK.** One `POST` with a JSON body and a JSON
answer needs no SDK, and the SDK would bring what the `anthropic` backend has to neutralise
(`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID` headers, its own
retries and platform headers, debug logging of request options) plus a new dependency. Here
nothing reads the environment, and the posture is `horae.llm.backends._shared.JsonApiBackend`:
the key only from the declared secret, the exact rendered bytes posted, every header rebuilt,
no ambient proxy or CA file, no redirect, quiet loggers, clean error chains.

**The endpoint.** `base_url` is the API root **with** its version prefix, as the OpenAI SDKs
take it (`https://openrouter.ai/api/v1`, `https://api.mistral.ai/v1`, `http://vllm:8000/v1`);
the request is `POST <base_url>/chat/completions` and nothing else (the guard refuses any
other origin, path or query). `http://` is allowed only to loopback, private and single-label
hosts (decision 24).

**The key** is the alias' optional `secret`, sent as `Authorization: Bearer <key>`; an alias
without one (a local server) sends no `Authorization` header.

**The request.** `model`, `max_tokens` (from the step budget), `stream: false`, and two
messages: `system` (Horae's `SYSTEM_PREAMBLE`, then the skill's prompt) and `user` (the input
inside `<input>` tags and, on the repair retry, the schema errors inside `<schema_errors>`
tags, with every `<` escaped, as for `anthropic`). **No tools**: `tools`, `tool_choice` and
`functions` are never sent. The output is constrained per the alias' `structured_output`:

- `json_schema` (default): `response_format: {type: json_schema, json_schema: {name:
  "horae_output", schema, strict: false}}`. `strict: false` because OpenAI's strict mode
  rejects schemas whose objects do not list every property as required or that use keywords
  it does not support (`maxLength`, `pattern`…), and many servers ignore the flag; the #31
  validator checks the full schema (decision 38 G);
- `json_object`: `response_format: {type: json_object}` (a server without JSON Schema
  support), and the schema is appended to the system prompt;
- `none`: no `response_format`, the schema in the system prompt.

**The response.** One choice whose `message.content` is one JSON value (parsed strictly: no
`NaN`, infinite number or duplicate key). `finish_reason` must be `stop`; `length` or
`model_length` is a truncation, a non-empty `message.refusal` or `content_filter` a refusal,
tool calls (`tool_calls`, `function_call`) are refused: all `output_invalid`, with the usage.
Usage comes from `usage.prompt_tokens` / `completion_tokens`; `prompt_tokens_details.
cached_tokens`, when reported, is recorded as `cache_read_tokens` and taken out of `tokens_in`
(which stays the input billed at the full price). A response without usable usage is charged
conservatively (the input bound and the whole `max_tokens`).

**Errors**: the HTTP status table of `_shared.STATUS_KINDS`, with the API's error `type` (a
short identifier, never its message text) in the message.
"""

import json
from collections.abc import Mapping
from typing import Any

from horae.contracts.instance import ModelAlias, StructuredOutput
from horae.contracts.run import Usage
from horae.engine.ports import LlmBackendName, LlmError
from horae.llm.backends._shared import (
    INVALID,
    JsonApiBackend,
    count,
    parse,
    plain_schema,
    system_text,
    token,
    user_texts,
)
from horae.llm.base import Completion, CompletionResult, cost
from horae.secrets import SecretResolver

SCHEMA_NAME = "horae_output"
"""The `json_schema.name` sent."""

BASE_KEYS = frozenset({"model", "max_tokens", "stream", "messages"})
"""The keys of every rendered payload; `response_format` is added unless the alias'
`structured_output` is `none`. Never `tools`, `tool_choice` or `functions`."""

TRUNCATED = ("length", "model_length")
"""`finish_reason`s that mean the output was cut."""


def _schema_text(schema: Mapping[str, Any]) -> str:
    return "\n\nThe output JSON Schema:\n" + json.dumps(
        plain_schema(schema), ensure_ascii=False, sort_keys=True
    )


class OpenAICompatibleBackend(JsonApiBackend):
    """The `openai-compatible` provider backend of one model alias (see the module
    docstring)."""

    BACKEND: LlmBackendName = "openai-compatible"
    API = "the Chat Completions API"
    ENDPOINT = "/chat/completions"

    @property
    def structured_output(self) -> StructuredOutput:
        return self._spec.structured_output or "json_schema"

    def _payload_keys(self) -> frozenset[str]:
        if self.structured_output == "none":
            return BASE_KEYS
        return BASE_KEYS | {"response_format"}

    def _max_tokens(self, payload: Mapping[str, Any]) -> int:
        return int(payload["max_tokens"])

    def render(self, completion: Completion) -> dict[str, Any]:
        """The `POST <base_url>/chat/completions` body for `completion` (pure)."""
        mode = self.structured_output
        system = system_text(completion)
        if mode != "json_schema":
            system += _schema_text(completion.output_schema)
        payload: dict[str, Any] = {
            "model": completion.model,
            "max_tokens": completion.max_tokens,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(user_texts(completion))},
            ],
        }
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "schema": plain_schema(completion.output_schema),
                    "strict": False,
                },
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _error_type(self, body: Any) -> str | None:
        error = body.get("error") if isinstance(body, dict) else None
        return token(error.get("type")) if isinstance(error, dict) else None

    def _usage(self, usage: Any, payload: Mapping[str, Any]) -> Usage:
        if not isinstance(usage, dict):
            return self.conservative(payload)
        prompt = count(usage.get("prompt_tokens"))
        completion = count(usage.get("completion_tokens"))
        if prompt is None or completion is None:
            return self.conservative(payload)
        details = usage.get("prompt_tokens_details")
        cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
        if cached is None or cached > prompt:
            return cost(self._spec.price, prompt, completion)
        return cost(self._spec.price, prompt - cached, completion, cache_read=cached)

    def _result(self, data: dict[str, Any], payload: Mapping[str, Any]) -> CompletionResult:
        usage = self._usage(data.get("usage"), payload)

        def invalid(reason: str) -> LlmError:
            return LlmError("output_invalid", reason, usage=usage)

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise invalid("the response does not have exactly one choice")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise invalid("the response's choice has no message")
        finish = token(choice.get("finish_reason"))
        refused = message.get("refusal")
        if (isinstance(refused, str) and refused) or finish == "content_filter":
            filtered = " (content_filter)" if finish == "content_filter" else ""
            raise invalid("the model refused" + filtered)
        if finish in TRUNCATED:
            limit = payload["max_tokens"]
            raise invalid(f"the output was truncated ({finish}, max_tokens {limit})")
        if message.get("tool_calls") or message.get("function_call"):
            raise invalid("the response has a tool call (no tools are allowed)")
        if finish != "stop":
            raise invalid(f"unexpected finish reason {finish or 'unknown'}")
        content = message.get("content")
        output = parse(content) if isinstance(content, str) else INVALID
        if output is INVALID:
            raise invalid("the response is not one JSON value")
        return CompletionResult(output=output, usage=usage, stop_reason=finish)


def openai_compatible_backend(
    alias: str, spec: ModelAlias, secrets: SecretResolver
) -> OpenAICompatibleBackend:
    """The `BackendFactory` of `openai-compatible`."""
    return OpenAICompatibleBackend(alias, spec, secrets)


__all__ = [
    "BASE_KEYS",
    "SCHEMA_NAME",
    "TRUNCATED",
    "OpenAICompatibleBackend",
    "openai_compatible_backend",
]
