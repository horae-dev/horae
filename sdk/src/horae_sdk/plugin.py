# SPDX-License-Identifier: Apache-2.0
"""`Plugin`: one stdio MCP server speaking the core's contract (`horae_sdk.protocol`).

The host's roles (`horae.contracts.plugin.Provides`):

- **source** (`collect` steps) and **transform** (`transform` steps): a function, or a
  `Source` / `Transform` subclass with a `run` method, taking the `Call` first and the tool's
  trusted parameters as keyword arguments (their annotations and defaults become the tool's
  input schema). It returns `Output(data, intents)`, or the data alone;
- **sink** and **notifier** (`act` / `notify` steps): a `Sink` / `Notifier` object
  (`horae_sdk.sinks`), whose operations the core calls.

```python
plugin = Plugin("hello", version="0.1.0", permissions=Permissions(intents=["task.create"]))

@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output: ...

plugin.run()  # serve over stdio
```

**Results.** Data must be JSON: an object is sent as `structuredContent` (with a short text
block, never a second copy), anything else as one JSON text block. Proposed intents (`dict`s,
e.g. from `horae_sdk.intents`) go in `_meta["dev.horae/intents"]`; one whose type is not in
the plugin's `permissions.intents` fails the call here (the core would reject the whole step).
A result is refused here, before it is sent, when it holds more than `MAX_INTENTS` (256)
intents (the core would refuse the whole step, `batch_too_large`) or when its message would
not fit the host's `MAX_MESSAGE_BYTES` (the host would kill the plugin, `output_too_large`).
`PluginError(message)` is a tool error (`isError`, the host's `tool_error`); any other
exception is logged to stderr and becomes a generic tool error naming its type, so an
exception's text never reaches the core unless the plugin chooses to.

Stdout is the JSON-RPC channel: never `print` in a plugin; log to stderr (`logging`).
"""

import inspect
import json
import logging
import sys
import typing
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, ContentBlock, TextContent

from horae_sdk.manifest import DEFAULT_HORAE, Permissions, manifest
from horae_sdk.protocol import (
    INPUT_META,
    INTENTS_META,
    MAX_INTENTS,
    MAX_MESSAGE_BYTES,
    MESSAGE_MARGIN,
    PATHS_META,
    Paths,
    meta_of,
)
from horae_sdk.sinks import Applied, ApplyRequest, Sink, SinkError, decode, encode

ToolRole = Literal["sources", "transforms"]
_CONTEXT = "horae_sdk_context"
_log = logging.getLogger("horae_sdk")


class PluginError(Exception):
    """A tool error the plugin reports (`isError`); its message reaches the core."""


@dataclass(frozen=True)
class Call:
    """One tool call from the core."""

    tool: str
    """The tool's ref, e.g. `hello.greet`."""
    params: dict[str, Any] = field(default_factory=dict)
    """The trusted parameters (also passed as keyword arguments)."""
    input: Any = None
    """A transform's input, the previous step's data (untrusted; `None` for a source)."""
    paths: Paths = field(default_factory=Paths)
    """The path permissions approved in `horae.lock` (trusted)."""


@dataclass(frozen=True)
class Output:
    """A tool's result: its data (JSON) and the intents it proposes."""

    data: Any = field(default_factory=dict)
    intents: Sequence[dict[str, Any]] = ()


class Tool:
    """A source or transform: `ref` and the function run for each call.

    Subclass `Source` or `Transform` with a `ref` and a `run(self, call, **params)` method,
    or pass `ref` and `fn`."""

    role: ClassVar[ToolRole]
    ref: str

    def __init__(self, ref: str | None = None, fn: Callable[..., Any] | None = None) -> None:
        if ref is not None:
            self.ref = ref
        if not isinstance(getattr(self, "ref", None), str):
            raise TypeError(f"{type(self).__name__} needs a ref")
        run = fn if fn is not None else getattr(self, "run", None)
        if not callable(run):
            raise TypeError(f"{self.ref}: give a function or define run(self, call, ...)")
        self.fn: Callable[..., Any] = run


class Source(Tool):
    """A source (`provides.sources`, `collect` steps): reads data, may propose intents."""

    role: ClassVar[ToolRole] = "sources"


class Transform(Tool):
    """A deterministic transform (`provides.transforms`): `call.input` is the previous
    step's data."""

    role: ClassVar[ToolRole] = "transforms"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _text(value: str) -> list[ContentBlock]:
    return [TextContent(text=value)]


def _error(message: str) -> CallToolResult:
    """A tool error (`isError`), the host's `tool_error` with `message`."""
    return CallToolResult(content=_text(message), is_error=True)


class Plugin:
    """A Horae plugin (see the module docstring)."""

    def __init__(
        self,
        name: str,
        *,
        version: str = "0.1.0",
        permissions: Permissions | None = None,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.permissions = permissions or Permissions()
        self.description = description
        self.server = MCPServer(name)
        self.tools: dict[str, Tool] = {}
        self.sinks: dict[str, Sink] = {}

    # --- registration ----------------------------------------------------------------------

    def add(self, component: Tool | Sink) -> None:
        """Register a source, transform, sink or notifier."""
        if isinstance(component, Sink):
            if component.namespace in self.sinks:
                raise ValueError(f"namespace {component.namespace} already has a sink")
            self.sinks[component.namespace] = component
            self._add_operations(component)
            return
        if component.ref in self.tools:
            raise ValueError(f"{component.ref} is registered twice")
        self.tools[component.ref] = component
        self.server.add_tool(self._handler(component), name=component.ref)

    def source(self, ref: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator: register the function as source `ref`."""
        return self._decorator(Source, ref)

    def transform(self, ref: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator: register the function as transform `ref`."""
        return self._decorator(Transform, ref)

    def _decorator(
        self, kind: type[Tool], ref: str
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add(kind(ref, fn))
            return fn

        return register

    def provides(self) -> dict[str, list[str]]:
        """`provides` as the manifest lists it (a tool that is both is listed under both)."""
        listed: dict[str, list[str]] = {}
        for tool in self.tools.values():
            listed.setdefault(tool.role, []).append(tool.ref)
        for sink in self.sinks.values():
            listed.setdefault(sink.role, []).extend(sink.tools)
        return listed

    def manifest(
        self,
        *,
        command: Sequence[str] | None,
        horae: str = DEFAULT_HORAE,
        license: str | None = None,
        homepage: str | None = None,
    ) -> dict[str, Any]:
        """This plugin's `plugin.yaml` document (`horae_sdk.manifest.manifest`)."""
        return manifest(
            name=self.name,
            version=self.version,
            provides=self.provides(),
            permissions=self.permissions,
            command=command,
            horae=horae,
            description=self.description,
            license=license,
            homepage=homepage,
        )

    def run(self) -> None:  # pragma: no cover (runs in the plugin's process)
        """Serve over stdio until the core closes stdin."""
        # Warnings go to stderr (never the protocol channel); only the SDK's own are muted.
        warnings.filterwarnings("ignore", module=r"horae_sdk(\.|$)")
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        self.server.run("stdio")

    # --- sources and transforms ------------------------------------------------------------

    def _handler(self, tool: Tool) -> Callable[..., Any]:
        """An MCP tool function with the tool's parameters (after the `Call`) and the SDK's
        context parameter."""
        signature = inspect.signature(tool.fn, eval_str=True)
        parameters = list(signature.parameters.values())
        if not parameters:
            raise TypeError(f"{tool.ref}: the first parameter takes the Call")
        hints = typing.get_type_hints(tool.fn, include_extras=True)
        rest = []
        for parameter in parameters[1:]:
            if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
                raise TypeError(f"{tool.ref}: parameter {parameter.name} must be a keyword")
            rest.append(parameter.replace(annotation=hints.get(parameter.name, Any)))
        if any(parameter.name == _CONTEXT for parameter in rest):
            raise TypeError(f"{tool.ref}: {_CONTEXT} is reserved")
        context = inspect.Parameter(_CONTEXT, inspect.Parameter.KEYWORD_ONLY, annotation=Context)

        async def handler(**arguments: Any) -> CallToolResult:
            ctx = arguments.pop(_CONTEXT)
            meta = meta_of(ctx.request_context.meta)
            call = Call(
                tool.ref,
                dict(arguments),
                meta.get(INPUT_META),
                Paths.from_wire(meta.get(PATHS_META)),
            )
            try:
                return self.result(tool.ref, await _maybe_await(tool.fn(call, **arguments)))
            except PluginError as error:
                return _error(str(error))
            except Exception as error:
                _log.exception("%s failed", tool.ref)
                return _error(f"{tool.ref} failed ({type(error).__name__})")

        handler.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=[*rest, context], return_annotation=CallToolResult
        )
        handler.__annotations__ = {
            **{parameter.name: parameter.annotation for parameter in rest},
            _CONTEXT: Context,
            "return": CallToolResult,
        }
        handler.__doc__ = tool.fn.__doc__
        handler.__name__ = tool.ref.replace(".", "_")
        return handler

    def result(self, ref: str, value: Any) -> CallToolResult:
        """`value` (an `Output` or the data) as the host reads it (see the module docstring);
        `PluginError` when it is not JSON or proposes an undeclared intent."""
        output = value if isinstance(value, Output) else Output(value)
        intents = [dict(intent) for intent in output.intents]
        if len(intents) > MAX_INTENTS:
            raise PluginError(
                f"{ref}: {len(intents)} intents, more than {MAX_INTENTS} in one result"
                " (the core refuses the whole step)"
            )
        for intent in intents:
            kind = intent.get("type")
            if kind not in self.permissions.intents:
                raise PluginError(f"{ref}: intent {str(kind)[:40]!r} is not in permissions.intents")
        try:
            text = json.dumps(output.data, allow_nan=False)
            size = _wire_size(output.data, intents, structured=isinstance(output.data, dict))
        except (TypeError, ValueError):
            raise PluginError(f"{ref}: the output is not JSON") from None
        if size > MAX_MESSAGE_BYTES - MESSAGE_MARGIN:
            raise PluginError(
                f"{ref}: the output is about {size} bytes, over the host's limit of"
                f" {MAX_MESSAGE_BYTES - MESSAGE_MARGIN} for one message"
            )
        meta = {INTENTS_META: intents} if intents else None
        if isinstance(output.data, dict):
            return CallToolResult(content=_text("ok"), structured_content=output.data, _meta=meta)
        return CallToolResult(content=_text(text), _meta=meta)

    # --- sinks and notifiers ---------------------------------------------------------------

    def _add_operations(self, sink: Sink) -> None:
        """Register `<ns>.read`, `.apply`, `.restore`, `.parent_exists` and `.identity`."""
        ns = sink.namespace
        taken = sorted(
            {f"{ns}.{op}" for op in ("read", "apply", "restore", "parent_exists", "identity")}
            & set(self.tools)
        )
        if taken:
            raise ValueError(f"{', '.join(taken)}: a sink operation cannot be a tool")
        declared = self.permissions.intents

        async def read(paths: dict[str, Any], path: str) -> CallToolResult:
            """The note's bytes (base64), or null."""
            return await _operation(lambda: _read(sink, paths, path))

        async def apply(
            paths: dict[str, Any],
            intent: dict[str, Any],
            content: str | None = None,
            expected_before_sha256: str | None = None,
            tool: str | None = None,
            params: dict[str, Any] | None = None,
            run_id: str | None = None,
            index: int | None = None,
            input_hash: str | None = None,
            dedupe_key: str | None = None,
        ) -> CallToolResult:
            """Apply one intent routed to this sink."""

            async def run() -> dict[str, Any]:
                if intent.get("type") not in declared:
                    raise SinkError(
                        "not_supported", f"{ns} applies no {intent.get('type')!s:.40} intent"
                    )
                request = ApplyRequest(
                    Paths.from_wire(paths),
                    intent,
                    tool,
                    params or {},
                    run_id,
                    index,
                    input_hash,
                    dedupe_key,
                    decode(content),
                    expected_before_sha256,
                )
                applied = await _maybe_await(sink.apply(request))
                if not isinstance(applied, Applied):
                    raise SinkError("sink_failed", f"{ns}.apply did not return Applied")
                return applied.to_wire()

            return await _operation(run)

        async def restore(
            paths: dict[str, Any],
            path: str,
            content: str | None = None,
            expected_sha256: str | None = None,
        ) -> CallToolResult:
            """Make the note hold content (null: remove it), compare-and-swap."""

            async def run() -> dict[str, Any]:
                await _maybe_await(
                    sink.restore(Paths.from_wire(paths), path, decode(content), expected_sha256)
                )
                return {}

            return await _operation(run)

        async def parent_exists(paths: dict[str, Any], path: str) -> CallToolResult:
            """Whether the note's directory exists."""

            async def run() -> dict[str, Any]:
                exists = await _maybe_await(sink.parent_exists(Paths.from_wire(paths), path))
                return {"exists": bool(exists)}

            return await _operation(run)

        async def identity(paths: dict[str, Any]) -> CallToolResult:
            """Where this sink writes."""

            async def run() -> dict[str, Any]:
                return {"identity": await _maybe_await(sink.identity(Paths.from_wire(paths)))}

            return await _operation(run)

        for function in (read, apply, restore, parent_exists, identity):
            self.server.add_tool(function, name=f"{ns}.{function.__name__}")


def _compact(value: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _wire_size(data: Any, intents: list[dict[str, Any]], *, structured: bool) -> int:
    """The UTF-8 size of what a result puts on the wire, without the JSON-RPC envelope
    (`MESSAGE_MARGIN` covers it): the data once (as `structuredContent`, or as a JSON text
    block, escaped again as a string), and the intents."""
    payload = _compact(data)
    if not structured:
        payload = _compact(payload)
    return len(payload.encode("utf-8", "surrogatepass")) + len(
        _compact(intents).encode("utf-8", "surrogatepass")
    )


async def _read(sink: Sink, paths: dict[str, Any], path: str) -> dict[str, Any]:
    content = await _maybe_await(sink.read(Paths.from_wire(paths), path))
    if content is not None and not isinstance(content, bytes):
        raise SinkError("sink_failed", "read did not return bytes")
    return {"content": encode(content)}


async def _operation(run: Callable[[], Any]) -> CallToolResult:
    """Run a sink operation: its result object, or `{"error": {kind, message}}`."""
    try:
        data: dict[str, Any] = await _maybe_await(run())
    except SinkError as error:
        data = {"error": {"kind": error.kind, "message": error.message}}
    except Exception as error:
        _log.exception("sink operation failed")
        data = {"error": {"kind": "sink_failed", "message": type(error).__name__}}
    try:
        size = _wire_size(data, [], structured=True)
    except (TypeError, ValueError):
        data, size = {"error": {"kind": "sink_failed", "message": "the result is not JSON"}}, 0
    if size > MAX_MESSAGE_BYTES - MESSAGE_MARGIN:
        data = {"error": {"kind": "output_too_large", "message": f"a result of {size} bytes"}}
    failure = data.get("error")
    summary = f"error: {failure['kind']}" if isinstance(failure, dict) else "ok"
    return CallToolResult(content=_text(summary), structured_content=data)


__all__ = [
    "Call",
    "Output",
    "Paths",
    "Plugin",
    "PluginError",
    "Source",
    "Tool",
    "Transform",
]
