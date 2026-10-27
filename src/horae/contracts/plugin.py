# SPDX-License-Identifier: Apache-2.0
"""Plugin manifest, `plugin.yaml` (PLAN.md §3.1, §3.3)."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from horae.contracts._base import (
    Contract,
    Hostname,
    HttpUrl,
    NonEmptyStr,
    PipelineName,
    PluginName,
    PluginRef,
    SecretName,
    SemVer,
    SkillRef,
    UniqueList,
)
from horae.contracts.intents import IntentType

_SPEC = r"(==|!=|>=|<=|~=|>|<)\s*[0-9]+(\.[0-9]+)*"
VersionSpecifier = Annotated[
    str, StringConstraints(pattern=rf"^\s*{_SPEC}(\s*,\s*{_SPEC})*\s*$", max_length=128)
]
"""Comma-separated version comparisons, e.g. `>=0.1,<0.2`."""


class Provides(Contract):
    """What the plugin provides. Every entry is qualified with the plugin's name."""

    sources: UniqueList[PluginRef] = Field(default_factory=list)
    """Tools that read data (used in `collect` steps)."""
    transforms: UniqueList[PluginRef] = Field(default_factory=list)
    """Deterministic transforms (used in `transform` steps)."""
    sinks: UniqueList[PluginRef] = Field(default_factory=list)
    """Tools that turn results into intents (used in `act` steps)."""
    notifiers: UniqueList[PluginRef] = Field(default_factory=list)
    """Notification tools (used in `notify` steps)."""
    skills: UniqueList[SkillRef] = Field(default_factory=list)
    """Skills for LLM steps: a prompt and an output schema."""
    pipelines: UniqueList[PipelineName] = Field(default_factory=list)
    """Ready-made pipelines (a pipeline pack)."""


class PathPermissions(Contract):
    """Filesystem access. Empty means no filesystem access."""

    read: UniqueList[NonEmptyStr] = Field(default_factory=list)
    """Paths the plugin may read."""
    write: UniqueList[NonEmptyStr] = Field(default_factory=list)
    """Paths the plugin may read and write."""


class Permissions(Contract):
    """Everything the plugin may touch. Undeclared access is a bug, blocked from M5."""

    network: UniqueList[Hostname] = Field(default_factory=list)
    """Hosts the plugin may connect to."""
    secrets: UniqueList[SecretName] = Field(default_factory=list)
    """Secrets injected (only) into this plugin's environment."""
    paths: PathPermissions = Field(default_factory=PathPermissions)
    """Filesystem paths."""
    intents: UniqueList[IntentType] = Field(default_factory=list)
    """Intents the plugin may propose. Empty means a pure source."""


class StdioRuntime(Contract):
    """A local plugin, started as a subprocess speaking MCP over stdio."""

    protocol: Literal["mcp-stdio"]
    command: Annotated[list[NonEmptyStr], Field(min_length=1)]
    """Command line, as an argument list (no shell)."""


class HttpRuntime(Contract):
    """A remote service speaking MCP over streamable HTTP."""

    protocol: Literal["mcp-http"]
    url: HttpUrl
    """The MCP endpoint."""


Runtime = Annotated[StdioRuntime | HttpRuntime, Field(discriminator="protocol")]


class PluginManifest(Contract):
    """A plugin's `plugin.yaml`."""

    name: PluginName
    """Plugin name; prefixes every tool it provides."""
    version: SemVer
    """Plugin version."""
    horae: VersionSpecifier
    """Compatible Horae core versions, e.g. `>=0.1,<0.2`."""
    provides: Provides
    permissions: Permissions
    runtime: Runtime | None = None
    """How to start or reach the plugin. Omitted for plugins that only ship skills or
    pipelines."""
