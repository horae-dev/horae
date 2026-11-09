# SPDX-License-Identifier: Apache-2.0
"""Plugin manifest, `plugin.yaml` (PLAN.md §3.1, §3.3)."""

from collections.abc import Collection, Mapping, Sequence
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, Strict, StringConstraints, model_validator

from horae.contracts.intents import IntentType
from horae.contracts.skill import ViewPath, outermost, view_paths_problem
from horae.contracts.types import (
    MAX_ITEMS,
    AbsolutePath,
    Contract,
    Hostname,
    HttpsUrl,
    Line,
    LocalEndpoint,
    PipelineName,
    PluginName,
    PluginRef,
    SecretName,
    SemVer,
    SkillRef,
    UniqueList,
    url_host,
)

_SPEC = r"(==|!=|>=|<=|~=|>|<) *[0-9]+(\.[0-9]+)*"
VersionSpecifier = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^ *{_SPEC}( *, *{_SPEC})* *$", max_length=128),
]
"""Comma-separated version comparisons, e.g. `>=0.1,<0.2`."""

SpdxExpression = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+:() -]*$", max_length=128),
]
"""An SPDX license expression, e.g. `Apache-2.0` or `MIT OR Apache-2.0`. Only the syntax is
checked, not the license list."""

CommandArg = Annotated[str, Strict(), StringConstraints(pattern=r"^[^\x00]+$", max_length=4096)]

SINK_OPERATIONS = ("read", "apply", "restore", "restore_status", "parent_exists", "identity")
"""The sink operations (`horae.plugins.host.sink`, #25): for every namespace `<ns>` of a
plugin's `provides.sinks` and `provides.notifiers`, only the core calls `<ns>.<operation>`, a
tool the plugin lists in `tools/list` but may not provide (so no pipeline step can call it)."""


MAX_PERSONAL_FIELDS = 64
"""Most personal fields one tool declares (decision 68)."""


def _usable_paths(paths: list[str]) -> list[str]:
    problem = view_paths_problem(paths)
    if problem is not None:
        raise ValueError(problem)
    return paths


PersonalFieldPaths = Annotated[
    UniqueList[ViewPath],
    Field(min_length=1, max_length=MAX_PERSONAL_FIELDS),
    AfterValidator(_usable_paths),
]
"""The personal fields of one tool's output: 1 to `MAX_PERSONAL_FIELDS` view paths (the syntax
of a skill's `input_view`, relative to the tool's output), none under another and none
disagreeing with another on a value's shape (pydantic-only rules)."""

PersonalFields = Annotated[dict[PluginRef, PersonalFieldPaths], Field(max_length=MAX_ITEMS)]
"""`{<source or transform ref>: [view paths]}` (decision 68)."""


def merge_personal_fields(
    *declarations: Mapping[str, Sequence[str]], tools: Collection[str]
) -> dict[str, list[str]]:
    """The union of several `personal_fields` declarations (an installed manifest's and the
    lock's, decision 68 B), limited to `tools` (the refs the plugin provides), each tool's
    paths reduced to the outermost ones. Never fewer fields than any one declaration."""
    union: dict[str, list[str]] = {}
    for declared in declarations:
        for ref, paths in declared.items():
            if ref in tools:
                union.setdefault(ref, []).extend(paths)
    return {ref: list(outermost(paths)) for ref, paths in sorted(union.items())}


class Provides(Contract):
    """What the plugin provides. Tools are `<namespace>.<tool>`; see `PluginRef` for how a
    namespace relates to the plugin name."""

    sources: UniqueList[PluginRef] = Field(default_factory=list)
    """Tools that read data (used in `collect` steps)."""
    transforms: UniqueList[PluginRef] = Field(default_factory=list)
    """Deterministic transforms (used in `transform` steps)."""
    sinks: UniqueList[PluginRef] = Field(default_factory=list)
    """Tools that apply intents (used in `act` steps)."""
    notifiers: UniqueList[PluginRef] = Field(default_factory=list)
    """Notification tools (used in `notify` steps)."""
    skills: UniqueList[SkillRef] = Field(default_factory=list)
    """Skills for LLM steps: a prompt and an output schema."""
    pipelines: UniqueList[PipelineName] = Field(default_factory=list)
    """Ready-made pipelines (a pipeline pack)."""
    personal_fields: PersonalFields = Field(default_factory=dict)
    """Fields of a source's or transform's output that hold personal data (decision 68), by
    tool ref, as view paths relative to that tool's output, e.g.
    `{school-fr.homework: ["children[].first_name", "homework[].teacher"]}`. Whenever that
    output reaches an `llm` step on a cloud model alias, the core de-identifies these fields
    as `person` and looks for their values in every string, whatever the skill declares. Each
    key must be one of `sources` or `transforms` (pydantic-only rule)."""

    @model_validator(mode="after")
    def _personal_fields_name_tools(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        unknown = sorted(set(self.personal_fields) - {*self.sources, *self.transforms})
        if unknown:
            raise ValueError(
                f"personal_fields: {', '.join(unknown)} is not one of the plugin's sources or "
                "transforms"
            )
        return self


class PathPermissions(Contract):
    """Filesystem access. Empty means no filesystem access."""

    read: UniqueList[AbsolutePath] = Field(default_factory=list)
    """Paths the plugin may read (normalised absolute paths)."""
    write: UniqueList[AbsolutePath] = Field(default_factory=list)
    """Paths the plugin may read and write (normalised absolute paths)."""


class Permissions(Contract):
    """Everything the plugin may touch. Undeclared access is a bug, blocked from M5."""

    network: UniqueList[Hostname] = Field(default_factory=list)
    """Hosts the plugin may connect to: public DNS names, `https` only, any port."""
    network_local: UniqueList[LocalEndpoint] = Field(default_factory=list)
    """Services on the local network the plugin may connect to (decision 66): loopback,
    private or link-local IP literals and `.local` / `.lan` / `.internal` / `.home.arpa`
    names, each with an explicit port, matched exactly (host and port), plain `http` allowed.
    Shown apart from `network` at approval ("local network")."""
    secrets: UniqueList[SecretName] = Field(default_factory=list)
    """Secrets injected (only) into this plugin's environment. Each must resolve, or the
    plugin does not start (`secret_missing`)."""
    optional_secrets: UniqueList[SecretName] = Field(default_factory=list)
    """Secrets injected like `secrets` when they resolve; one that does not is left out of the
    environment and the plugin still starts. Granted by `horae.lock` like any secret. Not
    also in `secrets`."""
    paths: PathPermissions = Field(default_factory=PathPermissions)
    """Filesystem paths."""
    intents: UniqueList[IntentType] = Field(default_factory=list)
    """Intents the plugin may propose or, for a sink, apply. Empty means a pure source."""

    @model_validator(mode="after")
    def _secrets_not_both(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        both = sorted(set(self.secrets) & set(self.optional_secrets))
        if both:
            raise ValueError(f"{', '.join(both)}: listed in both secrets and optional_secrets")
        return self

    @property
    def all_secrets(self) -> list[str]:
        """Every secret the plugin may be given: required, then optional."""
        return [*self.secrets, *self.optional_secrets]


class StdioRuntime(Contract):
    """A local plugin, started as a subprocess speaking MCP over stdio."""

    protocol: Literal["mcp-stdio"]
    command: Annotated[list[CommandArg], Field(min_length=1, max_length=MAX_ITEMS)]
    """Command line, as an argument list (no shell)."""


class HttpRuntime(Contract):
    """A remote service speaking MCP over streamable HTTP."""

    protocol: Literal["mcp-http"]
    url: HttpsUrl
    """The MCP endpoint (https only). Its host must be listed in `permissions.network`."""


Runtime = Annotated[StdioRuntime | HttpRuntime, Field(discriminator="protocol")]


TOOL_KINDS = ("sources", "transforms", "sinks", "notifiers")
"""The `provides` fields that list tools."""


def sink_namespaces(provides: Provides) -> frozenset[str]:
    """The namespaces of `provides.sinks` and `provides.notifiers`."""
    return frozenset(ref.split(".", 1)[0] for ref in (*provides.sinks, *provides.notifiers))


def reserved_tools(provides: Provides) -> frozenset[str]:
    """`<ns>.<operation>` for every sink or notifier namespace and every `SINK_OPERATIONS`."""
    return frozenset(
        f"{namespace}.{operation}"
        for namespace in sink_namespaces(provides)
        for operation in SINK_OPERATIONS
    )


class PluginManifest(Contract):
    """A plugin's `plugin.yaml`."""

    name: PluginName
    """Plugin name, e.g. `github`."""
    version: SemVer
    """Plugin version."""
    horae: VersionSpecifier
    """Compatible Horae core versions, e.g. `>=0.1,<0.2`."""
    description: Line | None = None
    """One-line description, at most 500 characters."""
    license: SpdxExpression | None = None
    """SPDX license expression, e.g. `Apache-2.0`."""
    homepage: HttpsUrl | None = None
    """Project page (https)."""
    provides: Provides
    permissions: Permissions
    runtime: Runtime | None = None
    """How to start or reach the plugin. Omitted for plugins that only ship skills or
    pipelines."""

    @model_validator(mode="after")
    def _sink_operations_not_provided(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        provided = {ref for kind in TOOL_KINDS for ref in getattr(self.provides, kind)}
        clash = sorted(provided & reserved_tools(self.provides))
        if clash:
            raise ValueError(
                f"{', '.join(clash)}: a sink operation of the plugin's own sink or notifier "
                "namespace cannot be provided (only the core calls it)"
            )
        return self

    @model_validator(mode="after")
    def _runtime_host_declared(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        if isinstance(self.runtime, HttpRuntime):
            host = url_host(self.runtime.url)
            if host not in self.permissions.network:
                raise ValueError(f"runtime host {host!r} is not in permissions.network")
        return self
