# SPDX-License-Identifier: Apache-2.0
"""The backend interface of the bounded LLM step (#28, PLAN.md §3.4, ADR 0005).

Two layers:

- **`ModelBackends`** is the engine's `LlmBackend` (`horae.engine.ports`). For one attempt
  (`LlmRequest`) it resolves the pipeline's model alias in `horae.yaml` (`models:` alias →
  backend, model, `base_url`, `secret`, `price`), gets the skill's prompt and output schema
  from a `PromptSource`, sizes `max_tokens` from the step budget, calls the alias' provider
  backend, and returns an `LlmResponse` whose `usage` holds the tokens and their cost.
- **`ProviderBackend`** is one provider adapter (`horae.llm.backends`: `anthropic` today;
  `claude-cli` #29, `openai-compatible` / `ollama` #30). It gets a `Completion` (system prompt,
  untrusted input, output schema, `max_tokens`, timeout), makes **one** request with **no
  tools**, and returns the parsed JSON output (`CompletionResult`), or raises `LlmError`.

**Render, then send.** Both layers split an attempt in two (the #34 design note): `render`
is pure (no I/O, no secret, no client) and builds the exact provider payload (model, system,
messages, `max_tokens`, output schema), a `RenderedRequest` whose `body()` is its canonical
JSON bytes; `send` makes the one network call with exactly that payload. The core can then
inspect what will leave the host before it does (e.g. a check that no resolved secret value
is in a cloud request, T10, #34/#53). `complete` is `send(render(...))`.

**Secrets.** A provider backend gets its API key only from the alias' declared `secret`,
through the instance's `SecretResolver` (decision 25), never from the ambient environment.
Provider backends are built lazily, on the first attempt that uses their alias, so a run
whose pipeline has no `llm` step (or whose alias is not used) never reads the key.

**Cost** (decision 38). `price` is per model alias in `horae.yaml`; Horae ships no price
list. Without one, `Usage.usd` is `0` and `Usage.priced` is `false`, and only the step's
`tokens_out` budget bounds the call.

**Retries.** None here: the engine owns the single repair retry (`attempt` 2). Provider
backends disable their SDK's own retries.

**Integration points.**

- **#27 (skill format).** `RequestPrompts` reads the rendered prompt, the viewed input, the
  output schema and the skill version from the `LlmRequest` (`prompt`, `input`,
  `output_schema`, `skill_version`) once #27 adds those fields. Until then a request without
  them fails with kind `skill_unavailable`. Another `PromptSource` (a skill loader) can be
  passed to `ModelBackends` instead.
- **#31 (repair).** `ModelBackends(validate=...)` turns an output into its schema errors
  (`LlmResponse.schema_errors`, which drive the repair retry). Without a validator the
  provider's structured output (constrained decoding to the schema) is the only check and
  `schema_errors` is empty.
- **#33 (budgets).** `max_tokens` is the attempt's remaining `tokens_out`, lowered so that
  the output alone cannot exceed the remaining `usd` when the alias has a price.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from horae.contracts.instance import ModelAlias, ModelPrice
from horae.contracts.run import Usage
from horae.engine.canonical import canonical_json
from horae.engine.ports import LlmBackendName, LlmError, LlmRequest, LlmResponse
from horae.secrets import SecretResolver

MAX_ERROR_CHARS = 200
"""Longest provider detail kept in an `LlmError` message."""


@dataclass(frozen=True)
class Prompt:
    """What the model is sent for one skill: the rendered prompt, the input it may see and the
    JSON Schema its output must follow."""

    system: str
    """The skill's rendered prompt (trusted: it comes from the skill, not from the data)."""
    input: Any
    """The input the skill may see (untrusted data)."""
    output_schema: Mapping[str, Any]
    """The skill's output JSON Schema."""
    skill_version: str


class PromptSource(Protocol):
    """Gives the prompt of the skill an `LlmRequest` names."""

    def prompt(self, request: LlmRequest) -> Prompt:
        """The prompt for `request`; raises `LlmError` (e.g. `skill_unavailable`)."""
        ...


class RequestPrompts:
    """Reads the prompt from the request itself (the #27 fields of `LlmRequest`)."""

    def prompt(self, request: LlmRequest) -> Prompt:
        system = getattr(request, "prompt", None)
        schema = getattr(request, "output_schema", None)
        version = getattr(request, "skill_version", None)
        if not isinstance(system, str) or not isinstance(schema, Mapping):
            raise LlmError(
                "skill_unavailable",
                f"skill {request.skill} has no loaded prompt and output schema",
            )
        return Prompt(
            system=system,
            input=request.input,
            output_schema=schema,
            skill_version=version if isinstance(version, str) and version else "0",
        )


@dataclass(frozen=True)
class Completion:
    """One attempt for a provider: what `ProviderBackend.render` turns into a payload."""

    model: str
    """The concrete model name (`models.<alias>.model`)."""
    system: str
    input: Any
    output_schema: Mapping[str, Any]
    max_tokens: int
    timeout_s: float
    repair: tuple[str, ...] = ()
    """For the repair retry: the previous attempt's schema errors."""


@dataclass(frozen=True)
class CompletionResult:
    """What one provider request returned."""

    output: Any
    """The parsed JSON output."""
    usage: Usage
    """Tokens and their cost (`cost`)."""
    stop_reason: str | None = None


class ProviderBackend(Protocol):
    """One provider adapter. Makes one request per `send`, with no tools and no retry."""

    @property
    def name(self) -> LlmBackendName: ...

    def render(self, completion: Completion) -> dict[str, Any]:
        """The exact JSON payload `send` will post for `completion`. Pure: no I/O, no
        secret; the same completion always gives the same payload. Raises `LlmError`
        (`invalid_request`) for a completion the provider cannot take."""
        ...

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        """One request with exactly `payload`; raises `LlmError` with one of the kinds of
        `ERROR_KINDS` and, when the provider reported it, the usage the failed request still
        cost."""
        ...

    async def aclose(self) -> None:
        """Release the connection pool. Must not raise."""
        ...


@dataclass(frozen=True)
class RenderedRequest:
    """One attempt, rendered: everything `ModelBackends.send` needs, and nothing secret."""

    alias: str
    """The model alias of the pipeline."""
    backend: LlmBackendName
    model: str
    """The concrete model."""
    skill_version: str
    output_schema: Mapping[str, Any]
    """The skill's full output schema (for the validator; the payload holds the provider's
    form of it)."""
    payload: Mapping[str, Any]
    """The exact JSON body sent to the provider."""
    timeout_s: float

    def body(self) -> bytes:
        """`payload` as canonical JSON bytes (sorted keys, no whitespace, UTF-8)."""
        return canonical_json(self.payload)


ERROR_KINDS = (
    "timeout",
    "rate_limited",
    "auth_failed",
    "overloaded",
    "invalid_request",
    "output_invalid",
    "host_error",
)
"""The `LlmError` kinds a provider backend raises."""

BackendFactory = Callable[[str, ModelAlias, SecretResolver], ProviderBackend]
"""Builds the provider backend of one model alias: `(alias, spec, secrets)`."""

Validator = Callable[[Any, Mapping[str, Any]], Sequence[str]]
"""`validate(output, schema)`: the schema errors of `output` (empty when valid); #31."""


def cost(
    price: ModelPrice | None,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> Usage:
    """The `Usage` of a call: its tokens and, when the alias has a `price`, their cost.

    Cache tokens without a cache price are priced as input tokens."""
    if price is None:
        usd = 0.0
    else:
        read_price = price.input_per_mtok
        if price.cache_read_per_mtok is not None:
            read_price = price.cache_read_per_mtok
        write_price = price.input_per_mtok
        if price.cache_write_per_mtok is not None:
            write_price = price.cache_write_per_mtok
        usd = (
            tokens_in * price.input_per_mtok
            + tokens_out * price.output_per_mtok
            + (cache_read or 0) * read_price
            + (cache_write or 0) * write_price
        ) / 1_000_000
    return Usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        usd=usd,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        priced=price is not None,
    )


def max_tokens(request: LlmRequest, price: ModelPrice | None) -> int:
    """The `max_tokens` of an attempt: its remaining `tokens_out`, lowered (with a price) so
    that the output tokens alone stay within the remaining `usd`; at least 1."""
    limit = request.budget.tokens_out
    if price is not None and price.output_per_mtok > 0:
        affordable = math.floor(request.budget.usd * 1_000_000 / price.output_per_mtok)
        limit = min(limit, affordable)
    return max(1, limit)


@dataclass
class ModelBackends:
    """The engine's `LlmBackend`: model aliases resolved to provider backends.

    `factories` maps a backend name to its `BackendFactory` (`horae.llm.backends.FACTORIES`
    for the built-in ones; `horae.llm.backends.model_backends` fills it in). An alias whose
    backend has no factory fails with `backend_unavailable`."""

    models: Mapping[str, ModelAlias]
    secrets: SecretResolver
    factories: Mapping[str, BackendFactory]
    prompts: PromptSource = field(default_factory=RequestPrompts)
    validate: Validator | None = None
    _backends: dict[str, ProviderBackend] = field(default_factory=dict, init=False, repr=False)

    def _backend(self, alias: str, spec: ModelAlias) -> ProviderBackend:
        backend = self._backends.get(alias)
        if backend is None:
            factory = self.factories.get(spec.backend)
            if factory is None:
                raise LlmError(
                    "backend_unavailable",
                    f"backend {spec.backend!r} of model alias {alias!r} is not available",
                )
            backend = factory(alias, spec, self.secrets)
            self._backends[alias] = backend
        return backend

    def render(self, request: LlmRequest) -> RenderedRequest:
        """The attempt `request`, rendered (pure: no I/O and no secret; see the module
        docstring). Raises `LlmError` (unknown alias, unavailable backend or skill)."""
        spec = self.models.get(request.model)
        if spec is None:
            raise LlmError("invalid_request", f"unknown model alias {request.model!r}")
        prompt = self.prompts.prompt(request)
        backend = self._backend(request.model, spec)
        completion = Completion(
            model=spec.model,
            system=prompt.system,
            input=prompt.input,
            output_schema=prompt.output_schema,
            max_tokens=max_tokens(request, spec.price),
            timeout_s=request.timeout_s,
            repair=request.repair,
        )
        return RenderedRequest(
            alias=request.model,
            backend=backend.name,
            model=spec.model,
            skill_version=prompt.skill_version,
            output_schema=prompt.output_schema,
            payload=backend.render(completion),
            timeout_s=request.timeout_s,
        )

    async def send(self, rendered: RenderedRequest) -> LlmResponse:
        """Send `rendered` (exactly its payload) through its alias' provider backend."""
        spec = self.models.get(rendered.alias)
        if spec is None:
            raise LlmError("invalid_request", f"unknown model alias {rendered.alias!r}")
        backend = self._backend(rendered.alias, spec)
        result = await backend.send(rendered.payload, rendered.timeout_s)
        errors: tuple[str, ...] = ()
        if self.validate is not None:
            errors = tuple(self.validate(result.output, rendered.output_schema))
        return LlmResponse(
            output=result.output,
            schema_errors=errors,
            usage=result.usage,
            backend=rendered.backend,
            model=rendered.model,
            skill_version=rendered.skill_version,
            request=dict(rendered.payload),
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """`send(render(request))`: the engine's `LlmBackend.complete`."""
        return await self.send(self.render(request))

    async def aclose(self) -> None:
        """Close every provider backend built so far."""
        backends, self._backends = list(self._backends.values()), {}
        for backend in backends:
            await backend.aclose()


__all__ = [
    "ERROR_KINDS",
    "MAX_ERROR_CHARS",
    "BackendFactory",
    "Completion",
    "CompletionResult",
    "ModelBackends",
    "Prompt",
    "PromptSource",
    "ProviderBackend",
    "RenderedRequest",
    "RequestPrompts",
    "Validator",
    "cost",
    "max_tokens",
]
