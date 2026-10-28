# SPDX-License-Identifier: Apache-2.0
"""The `ollama` backend: Ollama's native chat API (PLAN.md §3.4, #30, schemas decision 40).

Built on `horae.llm.backends._shared.JsonApiBackend` (a direct `httpx2` client; no `ollama`
SDK, and `OLLAMA_HOST` or any other variable is never read), with the posture of every
backend: the exact rendered bytes posted, every header rebuilt, no ambient proxy or CA file,
no redirect, quiet loggers, clean error chains.

**The endpoint.** `base_url` is the server root (`http://ollama:11434`, `http://localhost:
11434`); the request is `POST <base_url>/api/chat` and nothing else. `http://` is allowed only
to loopback, private and single-label hosts (decision 24).

**No key by default.** Ollama itself takes none; an alias may declare a `secret` for a proxy
in front of it, sent as `Authorization: Bearer <key>`.

**The request.** `model`, `stream: false`, `options.num_predict` (the `max_tokens` from the
step budget), `format` (the skill's output schema: Ollama's structured outputs) and two
messages, `system` (Horae's `SYSTEM_PREAMBLE`, then the skill's prompt) and `user` (the input
and, on the repair retry, the schema errors, tagged and escaped as for `anthropic`). No tools.

**The response.** `done` must be `true`; `done_reason` `length` is a truncation, tool calls
are refused, any other reason than `stop` (or none, as older servers send) is unexpected: all
`output_invalid`, with the usage. `message.content` must be one JSON value (parsed strictly).
Usage comes from `prompt_eval_count` / `eval_count`; when either is missing (Ollama may omit
`prompt_eval_count` when the prompt was cached) the call is charged conservatively; a reported
usage is floored by the request and output sizes (`_shared.floored`). Ollama is
local: without a `price` the cost is `0` (and `horae doctor` expects none).

**Errors**: the HTTP status table of `_shared.STATUS_KINDS` (Ollama's `error` is free text,
so the message carries the status only; a missing model is HTTP 404, `invalid_request`).
"""

from collections.abc import Mapping
from typing import Any

from horae.contracts.instance import ModelAlias
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
from horae.llm.base import Completion, CompletionResult
from horae.secrets import SecretResolver

PAYLOAD_KEYS = frozenset({"model", "stream", "messages", "format", "options"})
"""The keys of a rendered payload: never `tools`, `keep_alive`, `think` or anything else."""


class OllamaBackend(JsonApiBackend):
    """The `ollama` provider backend of one model alias (see the module docstring)."""

    BACKEND: LlmBackendName = "ollama"
    API = "the Ollama API"
    ENDPOINT = "/api/chat"

    def _payload_keys(self) -> frozenset[str]:
        return PAYLOAD_KEYS

    def _max_tokens(self, payload: Mapping[str, Any]) -> int:
        return int(payload["options"]["num_predict"])

    def render(self, completion: Completion) -> dict[str, Any]:
        """The `POST <base_url>/api/chat` body for `completion` (pure)."""
        return {
            "model": completion.model,
            "stream": False,
            "options": {"num_predict": completion.max_tokens},
            "format": plain_schema(completion.output_schema),
            "messages": [
                {"role": "system", "content": system_text(completion)},
                {"role": "user", "content": "\n\n".join(user_texts(completion))},
            ],
        }

    def _usage(self, data: Mapping[str, Any], payload: Mapping[str, Any]) -> Usage:
        prompt = count(data.get("prompt_eval_count"))
        output = count(data.get("eval_count"))
        if prompt is None or output is None:
            return self.conservative(payload)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = content if isinstance(content, str) else ""
        return self._floored(payload, prompt, output, text)

    def _result(self, data: dict[str, Any], payload: Mapping[str, Any]) -> CompletionResult:
        usage = self._usage(data, payload)

        def invalid(reason: str) -> LlmError:
            return LlmError("output_invalid", reason, usage=usage)

        message = data.get("message")
        if data.get("done") is not True or not isinstance(message, dict):
            raise invalid("the response is not a complete message")
        reason = token(data.get("done_reason"))
        if reason == "length":
            limit = self._max_tokens(payload)
            raise invalid(f"the output was truncated (length, max_tokens {limit})")
        if message.get("tool_calls"):
            raise invalid("the response has a tool call (no tools are allowed)")
        if data.get("done_reason") is not None and reason != "stop":
            raise invalid(f"unexpected done reason {reason or 'unknown'}")
        content = message.get("content")
        output = parse(content) if isinstance(content, str) else INVALID
        if output is INVALID:
            raise invalid("the response is not one JSON value")
        return CompletionResult(output=output, usage=usage, stop_reason=reason)


def ollama_backend(alias: str, spec: ModelAlias, secrets: SecretResolver) -> OllamaBackend:
    """The `BackendFactory` of `ollama`."""
    return OllamaBackend(alias, spec, secrets)


__all__ = ["PAYLOAD_KEYS", "OllamaBackend", "ollama_backend"]
