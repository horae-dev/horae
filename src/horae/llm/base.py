# SPDX-License-Identifier: Apache-2.0
"""The backend interface of the bounded LLM step (#28, PLAN.md §3.4, ADR 0005).

Two layers:

- **`ModelBackends`** is the engine's `LlmBackend` (`horae.engine.ports`). For one attempt
  (`LlmRequest`) it resolves the pipeline's model alias in `horae.yaml` (`models:` alias →
  backend, model, `base_url`, `secret`, `price`, `max_output_tokens`), gets the skill's prompt
  and output schema from a `PromptSource`, sizes `max_tokens` from the step budget, calls the
  alias' provider backend, and returns an `LlmResponse` whose `usage` holds the tokens and
  their cost.
- **`ProviderBackend`** is one provider adapter (`horae.llm.backends`: `anthropic` today;
  `claude-cli` #29, `openai-compatible` / `ollama` #30). It gets a `Completion` (system prompt,
  untrusted input, output schema, `max_tokens`, timeout), makes **one** request with **no
  tools**, and returns the parsed JSON output (`CompletionResult`), or raises `LlmError`.

**Render, then send.** Both layers split an attempt in two (the #34 design note): `render`
is pure (no I/O, no secret, no client) and builds the exact provider payload (model, system,
messages, `max_tokens`, output schema), a `RenderedRequest` whose `body()` is its canonical
JSON bytes, frozen when it is built; `send` makes the one network call with exactly those
bytes, and refuses a request whose payload was changed after rendering. The core can then
inspect what will leave the host before it does (e.g. a check that no resolved secret value
is in a cloud request, T10, #34/#53). `complete` is `send(render(...))`.

**Secrets.** A provider backend gets its API key only from the alias' declared `secret`,
through the instance's `SecretResolver` (decision 25), never from the ambient environment.
Provider backends are built lazily, on the first attempt that uses their alias, so a run
whose pipeline has no `llm` step (or whose alias is not used) never reads the key.

**Cost** (decision 38 A). `price` is per model alias in `horae.yaml`; Horae ships no price
list. Without one, `Usage.usd` is `0` and `Usage.priced` is `false`, and only the step's
`tokens_out` budget bounds the call (`horae doctor` warns about a cloud alias without one).

**Pre-flight budget** (decision 38 B). `render` refuses an attempt with `budget_exceeded`,
before any request, when it cannot afford one output token: the remaining `tokens_out` is
below 1, or, with a price, the input alone may cost more than the remaining `usd`, or what is
left after it buys no output token. The input cost is bounded without a `count_tokens` call:
a token is at least one UTF-8 byte, so the canonical JSON size of the whole rendered payload
(plus `INPUT_TOKEN_MARGIN` for what the provider adds) bounds the input tokens.

**What is recorded** (decision 38 E). `LlmCall.request` is `RenderedRequest.summary`: the
backend, model, `max_tokens`, and the SHA-256 and size of the skill prompt, the input, the
output schema and the exact body; never the payload itself (the input is already in the run
record's data, and a prompt can be long).

**Intents** (decision 38 D). Raw intents live in a reserved top-level `intents` property of
the skill's output object: when present it must be a list, and `ModelBackends` moves it into
`LlmResponse.intents` (after validating the whole output), removing it from `output`.

**Retries.** None here: the engine owns the single repair retry (`attempt` 2). Provider
backends disable their SDK's own retries.

**Integration points.**

- **#27 (skill format).** `RequestPrompts` reads the rendered prompt, the viewed input, the
  output schema and the skill version from the `LlmRequest` (`prompt`, `input`,
  `output_schema`, `skill_version`) with `getattr`, so it works before and after #27 adds
  those fields. Until then a request without them fails with kind `skill_unavailable` (the
  skill cannot be used yet; a skill that is present but malformed is #27's `skill_invalid`).
  Another `PromptSource` (a skill loader) can be passed to `ModelBackends` instead.
- **#31 (repair).** `ModelBackends(validate=...)` turns an output into its schema errors
  (`LlmResponse.schema_errors`, which drive the repair retry). Without a validator the
  provider's structured output is the only check and `schema_errors` is empty. **The
  validator is mandatory before production use** (decision 38 G): structured outputs drop
  the constraints they do not support (`anthropic.transform_schema` moves them into
  descriptions), so only the validator enforces the full schema.
- **#33 (budgets).** `worst_case` is what the usage ledger reserves before `send`; the
  runner's `LedgerLlm` (`horae.runner._budget`) calls `prepare` (the prompt, the provider
  backend and the input bound: the `PromptSource` reads happen there, outside the store's
  write lock), then, inside it, caps the request's budget with the day's remaining allowance
  and calls `render(capped, prepared)` (pure sizing and payload). `max_tokens` is the
  attempt's remaining `tokens_out` (capped by the alias' `max_output_tokens`), lowered so
  that the input bound plus the output cannot exceed the remaining `usd` when the alias has
  a price. For a provider backend that may write a prompt cache (`may_write_cache`), every
  token of the input bound is priced as a cache write (`input_price`), in both `max_tokens`
  and `worst_case`, so that a settled call never costs more than its reservation.
"""

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from horae.contracts.instance import ModelAlias, ModelPrice
from horae.contracts.run import Usage
from horae.engine.canonical import canonical_json
from horae.engine.ports import LlmBackendName, LlmError, LlmRequest, LlmResponse
from horae.secrets import SecretResolver

MAX_ERROR_CHARS = 200
"""Longest provider detail kept in an `LlmError` message."""

INPUT_TOKEN_MARGIN = 512
"""Input tokens a provider may add to the rendered payload (message framing, the
structured-output instructions), on top of its size in bytes, for the pre-flight bound."""

INTENTS_KEY = "intents"
"""The reserved top-level property of a skill's output that holds its raw intents."""


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
    """The skill's prompt (the provider backend adds Horae's own preamble before it)."""
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

    @property
    def may_write_cache(self) -> bool:
        """Whether a request may be billed prompt-cache writes (the provider caches on its
        own, or the payload asks for it): its input bound is then priced at the cache write
        price (decision 41). `False` for `anthropic` (Horae sends no `cache_control`); a
        backend without the attribute is taken as `False`."""
        ...

    def render(self, completion: Completion) -> dict[str, Any]:
        """The exact JSON payload `send` will post for `completion`. Pure: no I/O, no
        secret; the same completion always gives the same payload. Raises `LlmError`
        (`invalid_request`) for a completion the provider cannot take."""
        ...

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        """One request with exactly `payload`; raises `LlmError` with one of the kinds of
        `ERROR_KINDS` and, whenever the provider answered, the usage the failed request still
        cost."""
        ...

    async def aclose(self) -> None:
        """Release the connection pool. Must not raise."""
        ...


@dataclass(frozen=True)
class RenderedRequest:
    """One attempt, rendered: everything `ModelBackends.send` needs, and nothing secret.

    The payload's canonical JSON is frozen when the request is built: `body()` returns those
    bytes, and `ModelBackends.send` refuses a request whose `payload` no longer matches them
    (it was changed after it was inspected), then sends a fresh copy of the frozen body."""

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
    summary: Mapping[str, Any] = field(default_factory=dict)
    """What is recorded as `LlmCall.request` (digests and sizes; see the module docstring)."""
    input_bound: int = 0
    """The upper bound of the input tokens used by the pre-flight budget check."""
    max_tokens: int = 0
    """The payload's `max_tokens`: the most output tokens the attempt can cost."""
    may_write_cache: bool = False
    """The provider backend's `may_write_cache` (the input bound is priced as cache writes)."""
    _body: bytes = field(default=b"", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_body", canonical_json(self.payload))

    def body(self) -> bytes:
        """`payload` as canonical JSON bytes (sorted keys, no whitespace, UTF-8), as it was
        when the request was built."""
        return self._body


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summary(completion: Completion, backend: LlmBackendName, body: bytes) -> dict[str, Any]:
    """`LlmCall.request` of an attempt: digests and sizes, never the payload (decision 38 E).

    `system` is the skill's prompt, `input` and `schema` their canonical JSON, `body` the
    exact bytes sent."""
    system = completion.system.encode()
    data = canonical_json(completion.input)
    schema = canonical_json(dict(completion.output_schema))
    return {
        "backend": backend,
        "model": completion.model,
        "max_tokens": completion.max_tokens,
        "body_sha256": _digest(body),
        "body_bytes": len(body),
        "system_sha256": _digest(system),
        "system_bytes": len(system),
        "input_sha256": _digest(data),
        "input_bytes": len(data),
        "schema_sha256": _digest(schema),
        "schema_bytes": len(schema),
        "repair_errors": len(completion.repair),
    }


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

    Without a cache price, cache reads are priced as input tokens and cache writes at twice
    the input price (at least what providers charge: conservative)."""
    if price is None:
        usd = 0.0
    else:
        read_price = price.input_per_mtok
        if price.cache_read_per_mtok is not None:
            read_price = price.cache_read_per_mtok
        write_price = 2 * price.input_per_mtok
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


def input_bound(payload: Mapping[str, Any]) -> int:
    """An upper bound of the input tokens of `payload`: its canonical JSON size in bytes (a
    token is at least one byte) plus `INPUT_TOKEN_MARGIN`."""
    return len(canonical_json(payload)) + INPUT_TOKEN_MARGIN


def input_price(price: ModelPrice, *, may_write_cache: bool = False) -> float:
    """The price per million tokens of the input bound: the input price or, when the backend
    may write a prompt cache, the higher of it and the cache write price (twice the input
    price without `cache_write_per_mtok`, as `cost` charges it)."""
    if not may_write_cache:
        return price.input_per_mtok
    if price.cache_write_per_mtok is not None:
        return max(price.cache_write_per_mtok, price.input_per_mtok)
    return 2 * price.input_per_mtok


def max_tokens(
    request: LlmRequest,
    price: ModelPrice | None,
    *,
    input_tokens: int = 0,
    cap: int | None = None,
    may_write_cache: bool = False,
) -> int:
    """The `max_tokens` of an attempt (decision 38 B): its remaining `tokens_out`, capped by
    the alias' `max_output_tokens`, and, with a price, lowered so that `input_tokens` (an
    upper bound of the input, at `input_price`) plus the output stay within the remaining
    `usd`.

    Raises `LlmError("budget_exceeded")` when not one output token is affordable, so that no
    request is sent."""
    limit = request.budget.tokens_out
    if cap is not None:
        limit = min(limit, cap)
    if limit < 1:
        raise LlmError("budget_exceeded", "no output token is left in the step's tokens_out")
    if price is not None:
        per_mtok = input_price(price, may_write_cache=may_write_cache)
        left = request.budget.usd - input_tokens * per_mtok / 1_000_000
        if left < 0:
            raise LlmError(
                "budget_exceeded",
                f"the input (up to {input_tokens} tokens) may cost more than the step's "
                "remaining usd",
            )
        if price.output_per_mtok > 0:
            limit = min(limit, math.floor(left * 1_000_000 / price.output_per_mtok))
        if limit < 1:
            raise LlmError(
                "budget_exceeded", "the step's remaining usd buys no output token after the input"
            )
    return limit


def split_intents(output: Any, usage: Usage) -> tuple[Any, list[Any]]:
    """`(output without its intents, raw intents)` (decision 38 D): the reserved top-level
    `intents` of an output object, which must be a list when present."""
    if not isinstance(output, dict) or INTENTS_KEY not in output:
        return output, []
    intents = output[INTENTS_KEY]
    if not isinstance(intents, list):
        raise LlmError("output_invalid", f"the output's `{INTENTS_KEY}` is not a list", usage=usage)
    rest = {key: value for key, value in output.items() if key != INTENTS_KEY}
    return rest, intents


@dataclass(frozen=True)
class PreparedRequest:
    """An attempt before its budget is applied (`ModelBackends.prepare`)."""

    alias: str
    spec: ModelAlias
    backend: ProviderBackend
    probe: Completion
    """The completion with `max_tokens=1` (its payload sizes the input bound)."""
    skill_version: str
    input_bound: int
    may_write_cache: bool = False


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

    def prepare(self, request: LlmRequest) -> "PreparedRequest":
        """What `render` needs that does not depend on the attempt's budget: the alias, the
        skill's prompt (from the `PromptSource`, which may read files), the provider backend
        and the input bound. Raises `LlmError`: an unknown alias, an unavailable backend or
        skill."""
        spec = self.models.get(request.model)
        if spec is None:
            raise LlmError("invalid_request", f"unknown model alias {request.model!r}")
        prompt = self.prompts.prompt(request)
        backend = self._backend(request.model, spec)
        probe = Completion(
            model=spec.model,
            system=prompt.system,
            input=prompt.input,
            output_schema=prompt.output_schema,
            max_tokens=1,
            timeout_s=request.timeout_s,
            repair=request.repair,
        )
        return PreparedRequest(
            alias=request.model,
            spec=spec,
            backend=backend,
            probe=probe,
            skill_version=prompt.skill_version,
            input_bound=input_bound(backend.render(probe)),
            may_write_cache=getattr(backend, "may_write_cache", False) is True,
        )

    def output_limit(self, request: LlmRequest, prepared: "PreparedRequest") -> int:
        """The `max_tokens` that `render(request, prepared)` gives (or its `LlmError`)."""
        return max_tokens(
            request,
            prepared.spec.price,
            input_tokens=prepared.input_bound,
            cap=prepared.spec.max_output_tokens,
            may_write_cache=prepared.may_write_cache,
        )

    def render(
        self, request: LlmRequest, prepared: "PreparedRequest | None" = None
    ) -> RenderedRequest:
        """The attempt `request`, rendered (pure: no I/O and no secret; see the module
        docstring). `prepared` is `prepare` of `request` (or of a request that differs from
        it only in its budget), made now when not given. Raises `LlmError`: an unknown alias,
        an unavailable backend or skill, or `budget_exceeded` when the attempt cannot afford
        one output token."""
        if prepared is None:
            prepared = self.prepare(request)
        limit = self.output_limit(request, prepared)
        completion = replace(prepared.probe, max_tokens=limit)
        backend = prepared.backend
        payload = backend.render(completion)
        return RenderedRequest(
            alias=prepared.alias,
            backend=backend.name,
            model=prepared.spec.model,
            skill_version=prepared.skill_version,
            output_schema=prepared.probe.output_schema,
            payload=payload,
            timeout_s=prepared.probe.timeout_s,
            summary=summary(completion, backend.name, canonical_json(payload)),
            input_bound=prepared.input_bound,
            max_tokens=limit,
            may_write_cache=prepared.may_write_cache,
        )

    def worst_case(self, rendered: RenderedRequest) -> Usage:
        """The most `rendered` can cost (the usage ledger's reservation, decision 41): every
        token of its input bound at `input_price` (the cache write price when its backend
        `may_write_cache`) and every output token of its `max_tokens`: the bound that
        `max_tokens` keeps within the attempt's `usd`. Cache reads cost less than input."""
        spec = self.models.get(rendered.alias)
        price = None if spec is None else spec.price
        usage = cost(price, rendered.input_bound, rendered.max_tokens)
        if price is None or not rendered.may_write_cache:
            return usage
        per_mtok = input_price(price, may_write_cache=True)
        usd = (
            rendered.input_bound * per_mtok + rendered.max_tokens * price.output_per_mtok
        ) / 1_000_000
        return usage.model_copy(update={"usd": usd})

    async def send(self, rendered: RenderedRequest) -> LlmResponse:
        """Send `rendered` (exactly its frozen body) through its alias' provider backend."""
        spec = self.models.get(rendered.alias)
        if spec is None:
            raise LlmError("invalid_request", f"unknown model alias {rendered.alias!r}")
        body = rendered.body()
        try:
            unchanged = canonical_json(rendered.payload) == body
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            raise LlmError("invalid_request", "the payload was changed after it was rendered")
        backend = self._backend(rendered.alias, spec)
        result = await backend.send(json.loads(body), rendered.timeout_s)
        errors: tuple[str, ...] = ()
        if self.validate is not None:
            errors = tuple(self.validate(result.output, rendered.output_schema))
        output, intents = split_intents(result.output, result.usage)
        return LlmResponse(
            output=output,
            schema_errors=errors,
            usage=result.usage,
            backend=rendered.backend,
            model=rendered.model,
            skill_version=rendered.skill_version,
            request=dict(rendered.summary),
            intents=intents,
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
    "INPUT_TOKEN_MARGIN",
    "INTENTS_KEY",
    "MAX_ERROR_CHARS",
    "BackendFactory",
    "Completion",
    "CompletionResult",
    "ModelBackends",
    "PreparedRequest",
    "Prompt",
    "PromptSource",
    "ProviderBackend",
    "RenderedRequest",
    "RequestPrompts",
    "Validator",
    "cost",
    "input_bound",
    "input_price",
    "max_tokens",
    "split_intents",
    "summary",
]
