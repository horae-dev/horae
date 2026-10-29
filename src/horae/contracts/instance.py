# SPDX-License-Identifier: Apache-2.0
"""Instance configuration, `horae.yaml` (PLAN.md §3.1, §3.4, §3.5, §7).

The file holds no secret values: only references to them (an environment variable or a file
under the secrets directory, `/run/secrets` by default).
"""

import ipaddress
import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    Strict,
    StrictInt,
    StringConstraints,
    model_validator,
)

from horae.contracts.types import (
    _PORT,
    _URL_REST,
    HOST,
    MAX_ITEMS,
    Contract,
    IanaTimezone,
    PipelineName,
    PluginName,
    SchemaVersion1,
    SecretName,
    StrictBool,
    StrictFloat,
    Token,
    _valid_a_label,
)

LlmBackend = Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]
"""The LLM backends of PLAN.md §3.4 (the same list as `llm[].backend` in run records)."""

BACKENDS_NEEDING_BASE_URL: tuple[LlmBackend, ...] = ("openai-compatible", "ollama")
"""Backends that have no default endpoint, so `base_url` is required."""

BACKENDS_NEEDING_SECRET: tuple[LlmBackend, ...] = ("anthropic",)
"""Backends that always authenticate with an API key, so `secret` is required."""

BACKENDS_NEEDING_HTTPS: tuple[LlmBackend, ...] = ("anthropic",)
"""Cloud backends whose `base_url`, when given, must be `https://` (the API key and the data
must not cross the network in clear; schemas decision 38 C)."""

LOCAL_BACKENDS: tuple[LlmBackend, ...] = ("ollama",)
"""Backends whose aliases run on the host or the LAN unless the alias says `local: false`;
every other backend is a cloud backend unless the alias says `local: true` (#34, schemas
decision 47 A). Never inferred from `base_url`."""

StructuredOutput = Literal["json_schema", "json_object", "none"]
"""How an `openai-compatible` alias constrains the output (schemas decision 40):
`json_schema` (the default) sends `response_format: {type: json_schema, ...}`, `json_object`
sends `{type: json_object}` for a server without JSON Schema support, `none` sends no
`response_format`; with the last two the schema is given in the system prompt."""

BACKENDS_WITH_STRUCTURED_OUTPUT: tuple[LlmBackend, ...] = ("openai-compatible",)
"""Backends whose alias may set `structured_output` and `max_tokens_field`."""

BACKENDS_WITH_COMMAND: tuple[LlmBackend, ...] = ("claude-cli",)
"""Backends that run a local executable, whose alias may set `command` (schemas decision 42)."""

CommandPath = Annotated[
    str, Strict(), StringConstraints(pattern=r"^/[^\x00-\x1f\x7f]*$", max_length=1024)
]
"""An absolute path to an executable (no control characters)."""

MaxTokensField = Literal["max_tokens", "max_completion_tokens"]
"""The request field an `openai-compatible` alias puts its output limit in (schemas decision
40): `max_tokens` (the default, what most servers take) or `max_completion_tokens` (what
OpenAI's reasoning models require, where it also bounds the hidden reasoning tokens)."""

MAX_OUTPUT_TOKENS = 1_000_000
"""Largest `models.<alias>.max_output_tokens` accepted (a typo guard)."""

ModelAliasName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[a-z][a-z0-9._-]*$", max_length=64)
]
"""A model alias, e.g. `default` or `cheap`, named by `llm.model` in pipelines."""

InstancePath = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[^\x00-\x1f\x7f]+$", max_length=1024)
]
"""A filesystem path, absolute or relative to the instance directory."""

EnvVarName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
]
"""The name of an environment variable to read a secret from."""

SecretFileName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$", max_length=255)
]
"""A file name inside the secrets directory: one path segment, not starting with `.`."""

# --- backend endpoints -----------------------------------------------------------------------

_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = rf"{_OCTET}(\.{_OCTET}){{3}}"
_IPV6 = r"\[[0-9a-f:.]{2,45}\]"
_SINGLE_LABEL = r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
_HTTP_HOST = (
    rf"({_SINGLE_LABEL}"
    rf"|127(\.{_OCTET}){{3}}"
    rf"|10(\.{_OCTET}){{3}}"
    rf"|172\.(1[6-9]|2[0-9]|3[01])(\.{_OCTET}){{2}}"
    rf"|192\.168(\.{_OCTET}){{2}}"
    r"|\[::1\]"
    r"|\[f[cd][0-9a-f]{2}:[0-9a-f:.]{0,40}\])"
)
BACKEND_URL_PATTERN = (
    rf"^(https://({HOST}|{_IPV4}|{_IPV6})|http://{_HTTP_HOST})(:{_PORT})?([/?]{_URL_REST}*)?$"
)
_BACKEND_HOST = re.compile(r"^(https?)://(\[[^\]]*\]|[^/?:]+)")
_ULA = ipaddress.IPv6Network("fc00::/7")
_PORT_PREFIX = re.compile(r"^:[0-9]+")
_ENCODED_SEPARATOR = re.compile(r"%(2f|2e|5c)", re.IGNORECASE)


def _check_backend_url(url: str) -> str:
    match = _BACKEND_HOST.match(url)
    if match is None:  # pragma: no cover - the pattern guarantees a match
        raise ValueError("not an http(s) URL")
    scheme, host = match.groups()
    if host.startswith("["):
        try:
            address: ipaddress.IPv6Address | None = ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            address = None
        if address is None:
            raise ValueError("the host is not a valid IPv6 address")
        # The pattern already limits http IPv6 hosts to [::1] and fc/fd prefixes; re-checked
        # here so that loosening the pattern can never open plain http to a public address.
        if scheme == "http" and not (address.is_loopback or address in _ULA):  # pragma: no cover
            raise ValueError("http is only allowed to loopback, private or single-label hosts")
    for label in host.split("."):
        if label.startswith("xn--") and not _valid_a_label(label):
            raise ValueError("an `xn--` label is not a valid IDNA A-label")
    path = _PORT_PREFIX.sub("", url[match.end() :]).split("?", 1)[0]
    if _ENCODED_SEPARATOR.search(path):
        raise ValueError("the path has an encoded `/`, `.` or `\\` (%2F, %2E, %5C)")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("the path has a `.` or `..` segment")
    return url


BackendUrl = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=BACKEND_URL_PATTERN, max_length=2048),
    AfterValidator(_check_backend_url),
]
"""An LLM backend endpoint. `https://` to any host name (including `localhost`) or IP
literal; `http://` only to a loopback address (`localhost`, `127.0.0.0/8`, `[::1]`), a
private address (`10/8`, `172.16/12`, `192.168/16`, IPv6 ULA `fc00::/7`) or a single-label
host such as a Compose service name (`ollama`). Optional port; no user info, fragment,
backslash, whitespace or control characters, and a path with no `.` / `..` segment and no
encoded `/`, `.` or `\\` (`%2F`, `%2E`, `%5C`, which a server may decode into a traversal
past the guard's path check; schemas README, decisions 24 and 40)."""


MAX_PRICE_PER_MTOK = 10000.0
"""Largest price accepted, in US dollars per million tokens (a typo guard)."""

PricePerMtok = Annotated[StrictFloat, Field(ge=0, le=MAX_PRICE_PER_MTOK)]
"""A price in US dollars per million tokens."""


class ModelPrice(Contract):
    """What a model's tokens cost, in US dollars per million tokens (schemas decision 38).

    Horae ships no price list: prices change and differ per account, so the instance states
    the ones it pays. Cache prices are optional: cache reads without one are priced as input
    tokens, cache writes as twice the input price (conservative)."""

    input_per_mtok: PricePerMtok
    output_per_mtok: PricePerMtok
    cache_read_per_mtok: PricePerMtok | None = None
    cache_write_per_mtok: PricePerMtok | None = None


class ModelAlias(Contract):
    """What a model alias resolves to."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "required": ["backend"],
                        "properties": {"backend": {"enum": list(BACKENDS_NEEDING_BASE_URL)}},
                    },
                    "then": {
                        "required": ["base_url"],
                        "properties": {"base_url": {"type": "string"}},
                    },
                },
                {
                    "if": {
                        "required": ["backend"],
                        "properties": {"backend": {"enum": list(BACKENDS_NEEDING_SECRET)}},
                    },
                    "then": {
                        "required": ["secret"],
                        "properties": {"secret": {"type": "string"}},
                    },
                },
                {
                    "if": {
                        "required": ["backend"],
                        "properties": {
                            "backend": {"not": {"enum": list(BACKENDS_WITH_STRUCTURED_OUTPUT)}}
                        },
                    },
                    "then": {
                        "not": {
                            "anyOf": [
                                {"required": ["structured_output"]},
                                {"required": ["max_tokens_field"]},
                            ]
                        }
                    },
                },
                {
                    "if": {
                        "required": ["backend"],
                        "properties": {"backend": {"not": {"enum": list(BACKENDS_WITH_COMMAND)}}},
                    },
                    "then": {"not": {"required": ["command"]}},
                },
                {
                    "if": {
                        "required": ["backend"],
                        "properties": {"backend": {"enum": list(BACKENDS_NEEDING_HTTPS)}},
                    },
                    "then": {
                        "properties": {"base_url": {"type": "string", "pattern": "^https://"}}
                    },
                },
            ]
        }
    )

    backend: LlmBackend
    """The backend adapter."""
    model: Token
    """The concrete model name for that backend."""
    base_url: BackendUrl | None = None
    """Endpoint; required for `openai-compatible` and `ollama`. See `BackendUrl`. For
    `openai-compatible` it is the API root **with** its version prefix, as the OpenAI SDKs
    take it (`https://api.mistral.ai/v1`, `https://openrouter.ai/api/v1`,
    `http://vllm:8000/v1`): requests go to `<base_url>/chat/completions`. For `ollama` it is
    the server root (`http://ollama:11434`): requests go to `<base_url>/api/chat`."""
    secret: SecretName | None = None
    """The secret holding the backend's API key or token, e.g. `ANTHROPIC_API_KEY`; required
    for `anthropic`, optional for `openai-compatible` and `ollama` (sent as `Authorization:
    Bearer`; a local server usually needs none)."""
    price: ModelPrice | None = None
    """What the model costs, for `Usage.usd` and the step's `usd` budget. Without it a call
    is recorded with `usd: 0` and `priced: false`, and only `tokens_out` bounds the step
    (`horae doctor` warns about a cloud alias without one)."""
    max_output_tokens: Annotated[StrictInt, Field(ge=1, le=MAX_OUTPUT_TOKENS)] | None = None
    """A cap on `max_tokens` for every call of this alias, below the step's `tokens_out`
    (e.g. the model's own output limit)."""
    structured_output: StructuredOutput | None = None
    """`openai-compatible` only: how the output is constrained (`json_schema` when unset, or
    `json_object` / `none` for a server that lacks JSON Schema support). See
    `StructuredOutput`."""
    command: CommandPath | None = None
    """`claude-cli` only: the absolute path of the native `claude` executable (not the npm
    package's `#!/usr/bin/env node` script, whose `node` is not on the child `PATH`; symlinks
    are resolved when the backend first runs it). Unset, `claude` is looked up on the fixed
    child `PATH` (`/usr/local/bin:/usr/bin:/bin`), never on Horae's own `PATH` (schemas
    decision 42)."""
    max_tokens_field: MaxTokensField | None = None
    """`openai-compatible` only: the request field of the output limit (`max_tokens` when
    unset, or `max_completion_tokens`). See `MaxTokensField`."""
    local: StrictBool | None = None
    """Whether the model runs on the host or the LAN (`true`) or is a cloud service
    (`false`). Unset: `ollama` is local, every other backend (`anthropic`, `claude-cli`,
    `openai-compatible`) is cloud (`LOCAL_BACKENDS`); never inferred from `base_url`. A cloud
    alias gets de-identified input when the skill declares field classes, and `horae doctor`
    expects it to have a `price` (#34, schemas decision 47 A)."""

    @property
    def is_local(self) -> bool:
        """`local`, or the backend's default when unset (`LOCAL_BACKENDS`)."""
        if self.local is not None:
            return self.local
        return self.backend in LOCAL_BACKENDS

    @model_validator(mode="after")
    def _backend_requirements(self) -> Self:
        if self.backend in BACKENDS_NEEDING_BASE_URL and self.base_url is None:
            raise ValueError(f"backend {self.backend!r} requires `base_url`")
        if self.backend in BACKENDS_NEEDING_SECRET and self.secret is None:
            raise ValueError(f"backend {self.backend!r} requires `secret`")
        if (
            self.backend in BACKENDS_NEEDING_HTTPS
            and self.base_url is not None
            and not self.base_url.startswith("https://")
        ):
            raise ValueError(f"backend {self.backend!r} requires an `https://` `base_url`")
        if self.backend not in BACKENDS_WITH_STRUCTURED_OUTPUT:
            for name in ("structured_output", "max_tokens_field"):
                if getattr(self, name) is not None:
                    raise ValueError(f"backend {self.backend!r} takes no `{name}`")
        if self.backend not in BACKENDS_WITH_COMMAND and self.command is not None:
            raise ValueError(f"backend {self.backend!r} takes no `command`")
        return self


class EnvSecretRef(Contract):
    """Read the secret from an environment variable."""

    env: EnvVarName


class FileSecretRef(Contract):
    """Read the secret from a file in the secrets directory (Docker or Compose secrets)."""

    file: SecretFileName


SecretRef = EnvSecretRef | FileSecretRef
"""Where a secret's value comes from: `{env: NAME}` or `{file: name}`."""


class SecretsConfig(Contract):
    """Where secret values are read from."""

    dir: InstancePath = "/run/secrets"
    """Directory of secret files."""
    refs: Annotated[dict[SecretName, SecretRef], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Explicit sources, by secret name. A secret without one is read from the file of the
    same name in `dir` only: environment variables are read only through an explicit
    `{env: VAR}` reference."""


MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 600.0

Timeout = Annotated[StrictFloat, Field(ge=MIN_TIMEOUT_S, le=MAX_TIMEOUT_S)]
"""A plugin timeout in seconds, 1 to 600."""

DEFAULT_STARTUP_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 30.0


class PluginDefaults(Contract):
    """Timeouts for every plugin without its own setting (the #24 plugin host reads them)."""

    startup_timeout_s: Timeout = DEFAULT_STARTUP_TIMEOUT_S
    """Seconds a plugin may take to start and answer the MCP handshake."""
    call_timeout_s: Timeout = DEFAULT_CALL_TIMEOUT_S
    """Seconds one tool call may take."""


class PluginSettings(Contract):
    """Instance settings for one installed plugin (it must be in `horae.lock`)."""

    startup_timeout_s: Timeout | None = None
    """Seconds the plugin may take to start. When omitted, `plugin_defaults` applies."""
    call_timeout_s: Timeout | None = None
    """Seconds one tool call may take. When omitted, `plugin_defaults` applies."""


MIN_RUN_TIMEOUT_S = 1.0
MAX_RUN_TIMEOUT_S = 86400.0
DEFAULT_RUN_TIMEOUT_S = 900.0

RunTimeout = Annotated[StrictFloat, Field(ge=MIN_RUN_TIMEOUT_S, le=MAX_RUN_TIMEOUT_S)]
"""A run timeout in seconds, 1 to 86400 (one day)."""


class RunsConfig(Contract):
    """Bounds of one pipeline run (#19)."""

    timeout_s: RunTimeout = DEFAULT_RUN_TIMEOUT_S
    """Seconds a whole run may take. Past it the run is cancelled: the current step is
    `failed` with kind `timeout`, later steps are `skipped` and the run is `failed`. Per-call
    limits are separate: `plugin_defaults.call_timeout_s` and the LLM step's `timeout_s`."""


MIN_CACHE_TTL_S = 60
MAX_CACHE_TTL_S = 366 * 86400
DEFAULT_CACHE_TTL_S = 7 * 86400
MAX_CACHE_ENTRIES = 100_000
DEFAULT_CACHE_ENTRIES = 1000


class CacheConfig(Contract):
    """The LLM cache: accepted outputs reused on unchanged input, the $0 path (#32)."""

    ttl_s: Annotated[StrictInt, Field(ge=MIN_CACHE_TTL_S, le=MAX_CACHE_TTL_S)] = DEFAULT_CACHE_TTL_S
    """Seconds an accepted output is reused for (60 to 366 days; default 7 days), counted
    from when it was produced: a hit does not extend it, so an unchanged input asks the model
    again at most once per `ttl_s`. Lowering it applies at once (to every entry, at lookup);
    raising it applies to new entries (an entry keeps the expiry it was written with)."""
    max_entries: Annotated[StrictInt, Field(ge=1, le=MAX_CACHE_ENTRIES)] = DEFAULT_CACHE_ENTRIES
    """Most entries kept: at the start of each (non-dry) run, expired entries are deleted, then
    the oldest beyond this many (first in, first out: a hit does not make an entry recent)."""


class SchedulerConfig(Contract):
    """How pipelines with a `trigger` are started."""

    mode: Literal["builtin", "external"] = "builtin"
    """`builtin`: Horae's own cron. `external`: triggers are ignored and an external scheduler
    (Coolify, systemd, cron) calls `horae run <pipeline>`."""


class DailyBudget(Contract):
    """A ceiling on what LLM calls may spend in one day (schemas decision 41).

    The day is the instance's `timezone` day. At least one of `usd` and `tokens_out` is
    required. `usd` bounds only the calls of priced model aliases (an alias without `price`
    records `usd: 0`); `tokens_out` bounds every call."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["usd"], "properties": {"usd": {"type": "number"}}},
                {"required": ["tokens_out"], "properties": {"tokens_out": {"type": "integer"}}},
            ]
        }
    )

    usd: Annotated[StrictFloat, Field(gt=0)] | None = None
    """Maximum spend per day, in US dollars."""
    tokens_out: Annotated[StrictInt, Field(ge=1)] | None = None
    """Maximum output tokens per day."""

    @model_validator(mode="after")
    def _one_ceiling(self) -> Self:
        if self.usd is None and self.tokens_out is None:
            raise ValueError("a daily budget needs `usd`, `tokens_out` or both")
        return self


class PipelineBudgets(Contract):
    """The budgets of one pipeline, on top of the instance's."""

    daily: DailyBudget
    """What this pipeline's LLM calls may spend per day."""


class BudgetsConfig(Contract):
    """Ceilings across runs (#33, schemas decision 41); each LLM step keeps its own `budget`.

    Before every LLM call the remaining daily allowance (the ceiling minus what the usage
    ledger records for the day, reservations of calls in flight included) caps the step's
    budget; when nothing is left the call is refused (`budget_exceeded`) and the step is
    `degraded`."""

    daily: DailyBudget | None = None
    """What every pipeline's LLM calls together may spend per day."""
    pipelines: Annotated[dict[PipelineName, PipelineBudgets], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Per-pipeline ceilings, by pipeline name (both apply: the lower allowance wins)."""


class DeidentificationConfig(Contract):
    """De-identification before a cloud model (#34, schemas decision 47)."""

    vocabulary: SecretFileName | None = None
    """A file in `secrets.dir` (never in the instance repository: it holds personal data)
    listing known terms, one `class: term` per line (`person`, `email`, `phone` or `place`;
    `#` starts a comment), replaced by pseudonyms wherever they appear in a string or key
    of the input a skill with field classes is sent (a cloud alias). Read like a secret
    file (a regular file of at most 64 KiB, UTF-8)."""


class InstanceConfig(Contract):
    """An instance's `horae.yaml`."""

    schema_version: SchemaVersion1 = 1
    """Optional. Lets a later `horae.yaml` v2 be told apart from v1."""
    timezone: IanaTimezone = "UTC"
    """Default IANA time zone for triggers and run contexts."""
    state_dir: InstancePath = "state"
    """The state directory (SQLite store, run records)."""
    models: Annotated[dict[ModelAliasName, ModelAlias], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Model aliases, by name."""
    plugin_defaults: PluginDefaults = Field(default_factory=PluginDefaults)
    """Timeouts for every plugin, unless `plugins.<name>` overrides them."""
    plugins: Annotated[dict[PluginName, PluginSettings], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Per-plugin settings, by plugin name."""
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    runs: RunsConfig = Field(default_factory=RunsConfig)
    """Bounds of every pipeline run (the run timeout)."""
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    """Daily LLM spend ceilings across runs (#33)."""
    cache: CacheConfig = Field(default_factory=CacheConfig)
    """The LLM cache's expiry policy (#32)."""
    deidentification: DeidentificationConfig = Field(default_factory=DeidentificationConfig)
    """De-identification of declared fields before a cloud model (#34)."""

    def plugin_timeouts(self, plugin: str) -> PluginDefaults:
        """The effective timeouts of `plugin`: its own settings, else `plugin_defaults`."""
        own = self.plugins.get(plugin, PluginSettings())
        defaults = self.plugin_defaults
        return PluginDefaults(
            startup_timeout_s=(
                defaults.startup_timeout_s
                if own.startup_timeout_s is None
                else own.startup_timeout_s
            ),
            call_timeout_s=(
                defaults.call_timeout_s if own.call_timeout_s is None else own.call_timeout_s
            ),
        )
