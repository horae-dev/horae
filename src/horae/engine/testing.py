# SPDX-License-Identifier: Apache-2.0
"""In-memory implementations of the engine's ports, for tests and examples.

`FixturePlugins` is a `PluginRegistry` and a `ToolCaller`: register plugin manifests and a
Python handler per `collect` / `transform` tool, and it records every call in order (`calls`).
Sinks and notifiers need no handler: the engine never calls them (they apply intents later).
`FakeLlm` is an `LlmBackend` that answers from a script of responses. Nothing here starts a
process, opens a socket or calls a model.
"""

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.plugin import PluginManifest
from horae.contracts.run import Usage
from horae.engine.ports import (
    LlmError,
    LlmRequest,
    LlmResponse,
    ResolvedPlugin,
    ToolResult,
    UnknownPlugin,
)

Handler = Callable[[dict[str, Any], Any], ToolResult | Any]
"""`handler(params, input)`: a `ToolResult`, or plain data (a result without intents).
Raising `ToolCallError` fails the call."""


@dataclass(frozen=True)
class RecordedCall:
    """One call received by `FixturePlugins`."""

    ref: str
    params: dict[str, Any]
    input: Any
    timeout_s: float | None = None


@dataclass
class FixturePlugins:
    """In-memory plugins: a registry and a tool caller."""

    plugins: dict[str, ResolvedPlugin] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)

    def add(
        self,
        manifest: PluginManifest | dict[str, Any],
        handlers: dict[str, Handler] | None = None,
        *,
        commit: str | None = None,
    ) -> PluginManifest:
        """Register a plugin (a manifest or its parsed `plugin.yaml`) and its tool handlers.
        Namespaces resolve to plugins by the namespace of the tools and skills they provide."""
        if not isinstance(manifest, PluginManifest):
            manifest = PluginManifest.model_validate(manifest)
        resolved = ResolvedPlugin(manifest, commit)
        provides = manifest.provides
        for ref in [*provides.sources, *provides.transforms, *provides.sinks, *provides.notifiers]:
            self.plugins[ref.split(".", 1)[0]] = resolved
        for skill in provides.skills:
            self.plugins[skill.split("/", 1)[0]] = resolved
        self.handlers.update(handlers or {})
        return manifest

    def _resolve(self, namespace: str) -> ResolvedPlugin:
        try:
            return self.plugins[namespace]
        except KeyError:
            raise UnknownPlugin(namespace) from None

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        return self._resolve(ref.split(".", 1)[0])

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        return self._resolve(skill.split("/", 1)[0])

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        self.calls.append(RecordedCall(ref, copy.deepcopy(params), copy.deepcopy(input), timeout_s))
        result = self.handlers[ref](params, input)
        return result if isinstance(result, ToolResult) else ToolResult(data=result)

    @property
    def refs(self) -> list[str]:
        """The tools called, in order."""
        return [call.ref for call in self.calls]


def llm_response(
    output: Any,
    *,
    schema_errors: Iterable[str] = (),
    tokens_out: int = 10,
    usd: float = 0.001,
    intents: Any = None,
    cached: bool = False,
) -> LlmResponse:
    """A synthetic `LlmResponse` (backend `anthropic`, model `fixture-model`)."""
    return LlmResponse(
        output=output,
        schema_errors=tuple(schema_errors),
        usage=Usage(tokens_in=100, tokens_out=tokens_out, usd=usd),
        backend="anthropic",
        model="fixture-model",
        skill_version="1",
        intents=[] if intents is None else intents,
        cached=cached,
    )


@dataclass
class FakeLlm:
    """An `LlmBackend` that returns (or raises) `script` items in order."""

    script: list[LlmResponse | LlmError] = field(default_factory=list)
    requests: list[LlmRequest] = field(default_factory=list)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, LlmError):
            raise item
        return item
