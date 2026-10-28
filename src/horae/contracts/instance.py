# SPDX-License-Identifier: Apache-2.0
"""Instance configuration, `horae.yaml` (PLAN.md §3.1, §3.4, §3.5, §7).

The file holds no secret values: only references to them (an environment variable or a file
under the secrets directory, `/run/secrets` by default).
"""

import ipaddress
import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, Strict, StringConstraints, model_validator

from horae.contracts.types import (
    _PORT,
    _URL_REST,
    HOST,
    MAX_ITEMS,
    Contract,
    IanaTimezone,
    PluginName,
    SchemaVersion1,
    SecretName,
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
backslash, whitespace or control characters (schemas README, decision 24)."""


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
            ]
        }
    )

    backend: LlmBackend
    """The backend adapter."""
    model: Token
    """The concrete model name for that backend."""
    base_url: BackendUrl | None = None
    """Endpoint; required for `openai-compatible` and `ollama`. See `BackendUrl`."""
    secret: SecretName | None = None
    """The secret holding the backend's API key or token, e.g. `ANTHROPIC_API_KEY`; required
    for `anthropic`."""

    @model_validator(mode="after")
    def _backend_requirements(self) -> Self:
        if self.backend in BACKENDS_NEEDING_BASE_URL and self.base_url is None:
            raise ValueError(f"backend {self.backend!r} requires `base_url`")
        if self.backend in BACKENDS_NEEDING_SECRET and self.secret is None:
            raise ValueError(f"backend {self.backend!r} requires `secret`")
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


class SchedulerConfig(Contract):
    """How pipelines with a `trigger` are started."""

    mode: Literal["builtin", "external"] = "builtin"
    """`builtin`: Horae's own cron. `external`: triggers are ignored and an external scheduler
    (Coolify, systemd, cron) calls `horae run <pipeline>`."""


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
