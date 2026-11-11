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
    TypeAdapter,
    ValidationError,
    model_validator,
)

from horae.contracts.lockfile import GitRepoUrl
from horae.contracts.types import (
    _PORT,
    _URL_REST,
    HOST,
    MAX_ITEMS,
    AbsolutePath,
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

CORE_SECRET_NAMES = frozenset({"HORAE_ACT_KEY"})
"""The secrets the core itself reads (`horae.secrets.CORE_SECRETS`, #38): no `secrets.refs`
entry and no `deidentification.vocabulary` may name their file or environment variable, so a
plugin is never handed the core's key through a reference."""


def _not_core_secret(value: str) -> str:
    if value.upper() in CORE_SECRET_NAMES:
        raise ValueError("names a secret only the core may read")
    return value


PublicEnvVarName = Annotated[EnvVarName, AfterValidator(_not_core_secret)]
"""An `EnvVarName` that is not a core secret's (`CORE_SECRET_NAMES`, any case)."""
PublicSecretFileName = Annotated[SecretFileName, AfterValidator(_not_core_secret)]
"""A `SecretFileName` that is not a core secret's (`CORE_SECRET_NAMES`, any case)."""

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

    env: PublicEnvVarName
    """Not a core secret's variable (`HORAE_ACT_KEY`)."""


class FileSecretRef(Contract):
    """Read the secret from a file in the secrets directory (Docker or Compose secrets)."""

    file: PublicSecretFileName
    """Not a core secret's file (`HORAE_ACT_KEY`)."""


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


PluginRuntime = Literal["process", "container"]
"""How the host runs a plugin (schemas decision 74 A): `process`, a child process of Horae
with the same uid, network and filesystem (the "declared" tier); `container`, its own Docker
container (read-only, no network, only its own directory mounted, #49)."""


Cpus = Annotated[StrictFloat, Field(ge=0.1, le=64.0)]
"""CPUs a plugin container may use (`docker run --cpus`), 0.1 to 64."""
MemoryMb = Annotated[StrictInt, Field(ge=64, le=65536)]
"""Memory of a plugin container in MiB (`--memory`, and `--memory-swap` the same: no swap)."""
PidsLimit = Annotated[StrictInt, Field(ge=16, le=4096)]
"""Processes and threads a plugin container may hold at once (`--pids-limit`)."""
TmpMb = Annotated[StrictInt, Field(ge=1, le=1024)]
"""Size of a plugin container's `/tmp` tmpfs in MiB (it counts against its memory)."""
Nofile = Annotated[StrictInt, Field(ge=64, le=65536)]
"""Open files per process in a plugin container (`--ulimit nofile=N:N`)."""

DEFAULT_CPUS = 1.0
DEFAULT_MEMORY_MB = 512
DEFAULT_PIDS = 128
DEFAULT_TMP_MB = 64
DEFAULT_NOFILE = 1024


class ContainerLimits(Contract):
    """The resource limits of a plugin container (schemas decision 78 D). The defaults suit a
    4-core / 16 GB home server running a handful of plugins at once."""

    cpus: Cpus = DEFAULT_CPUS
    """CPUs (`--cpus`): a plugin never takes more than this share of the host."""
    memory_mb: MemoryMb = DEFAULT_MEMORY_MB
    """Memory in MiB (`--memory`, `--memory-swap` equal: no swap). Past it the kernel kills the
    plugin, reported as `oom_killed`. The `/tmp` tmpfs counts against it."""
    pids: PidsLimit = DEFAULT_PIDS
    """Processes and threads (`--pids-limit`): a fork loop fails instead of filling the host."""
    tmp_mb: TmpMb = DEFAULT_TMP_MB
    """Size of the `/tmp` tmpfs in MiB, the container's only scratch space. Less than
    `memory_mb`."""
    nofile: Nofile = DEFAULT_NOFILE
    """Open files per process (`--ulimit nofile`, soft and hard)."""

    @model_validator(mode="after")
    def _tmp_fits_in_memory(self) -> Self:
        if self.tmp_mb >= self.memory_mb:
            raise ValueError("tmp_mb must be less than memory_mb (the tmpfs is held in memory)")
        return self


class ContainerLimitsOverride(Contract):
    """A plugin's own limits: each field set wins over `plugin_defaults.limits`."""

    cpus: Cpus | None = None
    """CPUs (`--cpus`), 0.1 to 64."""
    memory_mb: MemoryMb | None = None
    """Memory in MiB (`--memory`, no swap), 64 to 65 536."""
    pids: PidsLimit | None = None
    """Processes and threads (`--pids-limit`), 16 to 4096."""
    tmp_mb: TmpMb | None = None
    """Size of the `/tmp` tmpfs in MiB, 1 to 1024 (less than the effective `memory_mb`)."""
    nofile: Nofile | None = None
    """Open files per process (`--ulimit nofile`), 64 to 65 536."""


class PluginDefaults(Contract):
    """Timeouts, runtime and container limits for every plugin without its own setting (the
    #24 plugin host reads them)."""

    startup_timeout_s: Timeout = DEFAULT_STARTUP_TIMEOUT_S
    """Seconds a plugin may take to start and answer the MCP handshake."""
    call_timeout_s: Timeout = DEFAULT_CALL_TIMEOUT_S
    """Seconds one tool call may take."""
    runtime: PluginRuntime = "process"
    """`process` (default) or `container` (schemas decision 74). With `container` a plugin that
    cannot run in a container is refused (`runtime_unavailable`), never run as a process."""
    limits: ContainerLimits = Field(default_factory=ContainerLimits)
    """CPU, memory, PID, `/tmp` and open-file limits of `runtime: container` (schemas decision
    78 D); the process runtime has none."""


class PluginSettings(Contract):
    """Instance settings for one installed plugin (it must be in `horae.lock`)."""

    startup_timeout_s: Timeout | None = None
    """Seconds the plugin may take to start. When omitted, `plugin_defaults` applies."""
    call_timeout_s: Timeout | None = None
    """Seconds one tool call may take. When omitted, `plugin_defaults` applies."""
    runtime: PluginRuntime | None = None
    """`process` or `container`. When omitted, `plugin_defaults.runtime` applies."""
    limits: ContainerLimitsOverride | None = None
    """Container limits of this plugin; each field omitted takes `plugin_defaults.limits`'."""


DEFAULT_CONTAINER_IMAGE = (
    # renovate: datasource=docker depName=python
    "python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
)
"""The plugin container image (schemas decision 74 G): the base image the Horae image is built
from (`PYTHON_IMAGE` in the `Dockerfile`, kept equal by a test), whose interpreter is the one
plugin venvs are built with."""

PinnedImage = Annotated[
    str,
    Strict(),
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$",
        max_length=512,
    ),
]
"""An image reference pinned by digest: `<name>[:<tag>]@sha256:<64 hex>`."""


class ContainerConfig(Contract):
    """The container runtime's engine and image (schemas decision 74 B, G)."""

    image: PinnedImage = DEFAULT_CONTAINER_IMAGE
    """The image every plugin container runs, pinned by digest. It must provide
    `/usr/local/bin/python3.12`, the interpreter plugin venvs link to."""
    docker: CommandPath | None = None
    """The Docker CLI, as an absolute path. When omitted, `docker` on
    `/usr/local/bin:/usr/bin:/bin`."""
    mount_roots: Annotated[list[AbsolutePath], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=list
    )
    """Directories, besides the instance's own, under which a container plugin's declared
    `permissions.paths` may be mounted (schemas decision 78 B), e.g. a vault kept outside the
    instance. A declared path elsewhere is refused (`runtime_unavailable`)."""


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


SchedulerMode = Literal["builtin", "external"]
"""`builtin`: `horae serve` runs the pipelines' `trigger`s. `external`: it does not, and an
external scheduler calls `horae run <pipeline>`."""

MonitorKind = Literal["uptime-kuma", "healthchecks"]
"""How a push monitor is pinged (schemas decision 49 F): `uptime-kuma` sets the push URL's
`status` (`up` / `down`), `msg` and `ping` query parameters; `healthchecks` pings the URL
itself for success and `<url>/fail` for a failure."""


class PushMonitor(Contract):
    """A push monitor pinged after every run of one pipeline (#39, schemas decision 49 F)."""

    secret: SecretName
    """The secret holding the push URL (it carries the monitor's token, so it is never written
    in `horae.yaml`): resolved like any secret (`secrets.refs`, else `<secrets.dir>/<NAME>`).
    `https://` to any host, `http://` only to the LAN (the `base_url` rule, decision 24)."""
    kind: MonitorKind = "uptime-kuma"
    """The monitor's push protocol."""


_PUSH_URL: TypeAdapter[str] = TypeAdapter(BackendUrl)


def is_push_url(value: str) -> bool:
    """Whether `value` may be a push monitor's URL (a `BackendUrl`, decision 24): the check
    `horae doctor` and the ping both make. Never says why: the error would quote the value."""
    try:
        _PUSH_URL.validate_python(value)
    except ValidationError:
        return False
    return True


MAX_CONCURRENT_RUNS = 16
"""Most scheduled runs `scheduler.max_concurrent` may allow at once."""
DEFAULT_MISFIRE_GRACE_S = 3600
MAX_MISFIRE_GRACE_S = 7 * 86400


class SchedulerConfig(Contract):
    """How pipelines with a `trigger` are started, and the push monitors of their runs."""

    mode: SchedulerMode = "builtin"
    """`builtin`: Horae's own cron. `external`: triggers are ignored and an external scheduler
    (Coolify, systemd, cron) calls `horae run <pipeline>`. The `HORAE_SCHEDULER` environment
    variable, when set, overrides it."""
    monitors: Annotated[dict[PipelineName, PushMonitor], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Push monitors by pipeline name: pinged `up` or `down` after each run of the pipeline,
    scheduled or `horae run` (not a dry run). A failed ping never fails the run."""
    max_concurrent: Annotated[StrictInt, Field(ge=1, le=MAX_CONCURRENT_RUNS)] = 1
    """Most scheduled runs in progress at once in `horae serve` (default 1: scheduled runs are
    serialised). A fire time that finds no free slot waits, first in first out, unless it
    would then start more than `misfire_grace_s` late. Whatever this is, two applies never
    run at once: they share the instance's apply lock (decision 34)."""
    misfire_grace_s: Annotated[StrictInt, Field(ge=1, le=MAX_MISFIRE_GRACE_S)] = (
        DEFAULT_MISFIRE_GRACE_S
    )
    """Seconds a fire time may start late (after a suspended host, or while waiting for a
    slot) before it is skipped and recorded instead of run (default 1 hour)."""


PUBLIC_URL_PATTERN = rf"^https://{HOST}(:{_PORT})?$"
"""`http.public_url`: an `https://` origin on a host name (no IP literal), an optional port, no
path, query, fragment or user info."""


def _default_port(value: str) -> str:
    return value.removesuffix(":443")


PublicUrl = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=PUBLIC_URL_PATTERN, max_length=300),
    AfterValidator(_default_port),
]
"""Normalised: the default port `:443` is dropped, as a browser's `Origin` drops it."""


MIN_PROXY_PREFIX = {4: 8, 6: 32}
"""The shortest `trusted_proxies` prefix: /8 for IPv4, /32 for IPv6 (a wider one, `/0` above
all, would trust any client's `X-Forwarded-For`)."""


def _ip_network(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise ValueError("not an IP address or a CIDR network (no host bits set)") from None
    if network.prefixlen < MIN_PROXY_PREFIX[network.version]:
        shortest = MIN_PROXY_PREFIX[network.version]
        raise ValueError(f"a trusted proxy network must be /{shortest} or narrower")
    return str(network)


IpNetwork = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^[0-9a-fA-F:.]{2,45}(/[0-9]{1,3})?$", max_length=49),
    AfterValidator(_ip_network),
]
"""An IP address or a CIDR network, e.g. `172.18.0.0/16` (normalised; host bits must be zero;
at least /8 for IPv4, /32 for IPv6)."""

MAX_TRUSTED_PROXIES = 16
MAX_MARKETPLACES = 16


class HttpConfig(Contract):
    """The public side of the HTTP API (`horae serve`, #37, #38)."""

    public_url: PublicUrl | None = None
    """The API's public origin behind the reverse proxy, e.g. `https://horae.example.org`: the
    base of the `/act` links the core mints (never taken from a request's `Host`), and the
    only `Origin` a `POST /act/...` may carry. Without it the core mints no links and
    notifications are sent without buttons. Its host must also be an allowed host
    (`HORAE_ALLOWED_HOSTS`)."""
    trusted_proxies: Annotated[list[IpNetwork], Field(max_length=MAX_TRUSTED_PROXIES)] = Field(
        default_factory=list
    )
    """The reverse proxies (addresses or networks) whose `X-Forwarded-For` header `/act`'s
    rate limit trusts: the client is the right-most address of that header that is not one
    of them. Empty (the default): the header is ignored and the client is the peer address."""


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

    vocabulary: PublicSecretFileName | None = None
    """A file in `secrets.dir` (never in the instance repository: it holds personal data)
    listing known terms, one `class: term` per line (`person`, `email`, `phone` or `place`;
    `#` starts a comment), replaced by pseudonyms wherever they appear in a string or key
    of the input a skill with field classes is sent (a cloud alias). Read like a secret
    file (a regular file of at most 64 KiB, UTF-8)."""


DEFAULT_MARKETPLACE = "https://github.com/horae-dev/marketplace"
"""The marketplace an instance uses unless `marketplaces` says otherwise."""


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
    """Timeouts and runtime for every plugin, unless `plugins.<name>` overrides them."""
    plugins: Annotated[dict[PluginName, PluginSettings], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Per-plugin settings, by plugin name."""
    container: ContainerConfig = Field(default_factory=ContainerConfig)
    """The container runtime's engine and image (used by plugins with `runtime: container`)."""
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
    http: HttpConfig = Field(default_factory=HttpConfig)
    """The HTTP API's public origin and trusted proxies (`/act` links, #38)."""
    marketplaces: Annotated[list[GitRepoUrl], Field(min_length=1, max_length=MAX_MARKETPLACES)] = (
        Field(default_factory=lambda: [DEFAULT_MARKETPLACE])
    )
    """Marketplace indexes, as `https://` git URLs, highest priority first (#43). Each holds a
    `marketplace.json`. Default: `https://github.com/horae-dev/marketplace`."""
    marketplace_pins: Annotated[dict[PluginName, GitRepoUrl], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Plugin name -> the one `marketplaces` entry that resolves it, when several indexes list
    the same plugin under different repositories (schemas decision 61)."""

    @model_validator(mode="after")
    def _check_plugin_limits(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        for name in self.plugins:
            try:
                self.plugin_limits(name)
            except ValidationError as error:
                reasons = "; ".join(
                    str(item["msg"]).removeprefix("Value error, ") for item in error.errors()
                )
                raise ValueError(f"plugins.{name}.limits: {reasons}") from None
        return self

    @model_validator(mode="after")
    def _check_marketplaces(self) -> Self:
        if len(set(self.marketplaces)) != len(self.marketplaces):
            raise ValueError("marketplaces lists the same URL twice")
        for name, url in self.marketplace_pins.items():
            if url not in self.marketplaces:
                raise ValueError(f"marketplace_pins.{name} is not one of `marketplaces`")
        return self

    def plugin_timeouts(self, plugin: str) -> PluginDefaults:
        """The effective timeouts and runtime of `plugin`: its own settings, else
        `plugin_defaults`."""
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
            runtime=self.plugin_runtime(plugin),
            limits=self.plugin_limits(plugin),
        )

    def plugin_runtime(self, plugin: str) -> PluginRuntime:
        """The effective runtime of `plugin`: `plugins.<plugin>.runtime`, else
        `plugin_defaults.runtime`."""
        own = self.plugins.get(plugin, PluginSettings()).runtime
        return self.plugin_defaults.runtime if own is None else own

    def plugin_limits(self, plugin: str) -> ContainerLimits:
        """The effective container limits of `plugin`: each field of its own `limits` that is
        set, else `plugin_defaults.limits`'."""
        own = self.plugins.get(plugin, PluginSettings()).limits
        defaults = self.plugin_defaults.limits
        if own is None:
            return defaults
        changes = own.model_dump(exclude_none=True)
        return ContainerLimits.model_validate(defaults.model_dump() | changes)
