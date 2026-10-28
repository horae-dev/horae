# SPDX-License-Identifier: Apache-2.0
"""Instance configuration, `horae.yaml` (PLAN.md §3.1, §3.4, §3.5, §7).

The file holds no secret values: only references to them (an environment variable or a file
under the secrets directory, `/run/secrets` by default).
"""

from typing import Annotated, Literal

from pydantic import Field, Strict, StringConstraints

from horae.contracts.types import (
    MAX_ITEMS,
    Contract,
    HttpsUrl,
    IanaTimezone,
    PluginName,
    SecretName,
    StrictFloat,
    Token,
)

LlmBackend = Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]
"""The LLM backends of PLAN.md §3.4 (the same list as `llm[].backend` in run records)."""

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


class ModelAlias(Contract):
    """What a model alias resolves to."""

    backend: LlmBackend
    """The backend adapter."""
    model: Token
    """The concrete model name for that backend."""
    base_url: HttpsUrl | None = None
    """Endpoint for `openai-compatible` and `ollama` backends (https only)."""
    secret: SecretName | None = None
    """The secret holding the backend's API key or token, e.g. `ANTHROPIC_API_KEY`."""


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
    """Explicit sources, by secret name. A secret without one is read from the environment
    variable of the same name, else from the file of the same name in `dir`."""


Timeout = Annotated[StrictFloat, Field(gt=0)]


class PluginSettings(Contract):
    """Instance settings for one installed plugin (it must be in `horae.lock`)."""

    startup_timeout_s: Timeout | None = None
    """Seconds the plugin may take to start. When omitted, the core default applies."""
    call_timeout_s: Timeout | None = None
    """Seconds one tool call may take. When omitted, the core default applies."""


class SchedulerConfig(Contract):
    """How pipelines with a `trigger` are started."""

    mode: Literal["builtin", "external"] = "builtin"
    """`builtin`: Horae's own cron. `external`: triggers are ignored and an external scheduler
    (Coolify, systemd, cron) calls `horae run <pipeline>`."""


class InstanceConfig(Contract):
    """An instance's `horae.yaml`."""

    timezone: IanaTimezone = "UTC"
    """Default IANA time zone for triggers and run contexts."""
    state_dir: InstancePath = "state"
    """The state directory (SQLite store, run records)."""
    models: Annotated[dict[ModelAliasName, ModelAlias], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Model aliases, by name."""
    plugins: Annotated[dict[PluginName, PluginSettings], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Per-plugin settings, by plugin name."""
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
