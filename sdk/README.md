# horae-sdk

Write [Horae](https://github.com/horae-dev/horae) plugins in Python: base classes for the
plugin roles, `plugin.yaml` helpers, intent builders, Obsidian Tasks escaping and a
recorded-input test harness. Apache-2.0.

A Horae plugin is an MCP server over stdio that the core's plugin host starts for one run
([ADR 0003](../docs/adr/0003-mcp-as-the-plugin-protocol.md)). It never writes anything
itself: it returns data and **proposes intents**, which the core validates against the
plugin's declared permissions, shows as a dry-run diff, journals and applies
([ADR 0002](../docs/adr/0002-intents-applied-by-the-core.md)). The SDK speaks exactly the
host's contract (`horae.plugins.host`, schemas decisions 35 and 36); any MCP server that
follows it works too.

## A minimal plugin

[`examples/hello/hello_plugin.py`](examples/hello/hello_plugin.py), a source and a transform:

```python
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, intents

plugin = Plugin("hello", version="0.1.0", permissions=Permissions(intents=["task.create"]))


@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output:
    task = intents.task_create(f"Greet {name} back", fingerprint=intents.fingerprint("hello", name))
    return Output({"greeting": f"Hello, {name}!"}, intents=[task])


@plugin.transform("hello.shout")
def shout(call: Call) -> dict[str, Any]:
    data = call.input if isinstance(call.input, dict) else {}
    return {key: value.upper() if isinstance(value, str) else value for key, value in data.items()}


if __name__ == "__main__":
    plugin.run()
```

A pipeline then calls `hello.greet: { name: Ada }` in a `collect` step and `hello.shout` in a
`transform` step.

## Roles

| Role | Manifest | SDK | Called by |
|---|---|---|---|
| source | `provides.sources` | `@plugin.source(ref)` or a `Source` subclass with `run` | a `collect` step |
| transform | `provides.transforms` | `@plugin.transform(ref)` or a `Transform` subclass | a `transform` step |
| sink | `provides.sinks` | a `Sink` subclass, `plugin.add(MySink(["ns.tool"]))` | the core, through the sink operations |
| notifier | `provides.notifiers` | a `Notifier` subclass | the core, through the sink operations |

A **source or transform** function takes the `Call` first, then the tool's parameters as
keyword arguments; their annotations and defaults become the tool's input schema. They are
the pipeline's **trusted** parameters. `call.input` is a transform's input (the previous
step's data: **untrusted**), `call.paths` the path permissions approved in `horae.lock`
(trusted configuration; `call.paths.allowed(path)` checks a path against them). Never take a
path to read or write from untrusted data. Return `Output(data, intents=[...])` or the data
alone; data must be JSON (an object travels as `structuredContent`). Raise
`PluginError("message")` for a tool error; any other exception is logged to stderr and
reported by type only. Never `print`: stdout is the protocol channel.

A **sink or notifier** is never called by a pipeline step. For an `act` / `notify` entry
`ns.tool`, the core calls the sink operations `ns.identity`, `ns.apply`, `ns.read`,
`ns.restore` and `ns.parent_exists` (the SDK registers them; they are listed in `tools/list`
but must not be in `provides`). Implement `identity(paths)` and `apply(request)`; a note sink
also implements `read`, `restore` and `parent_exists`. For a `note.*` intent **the core
computes the bytes** (`request.content`): write exactly those, atomically, only if the note
still has `request.expected_before_sha256` (else `SinkError("target_changed", ...)`), and
return `Applied.wrote(path, content)`. Other intents are applied by the sink itself; their
text is data. See [`examples/tasks_file/tasks_file_sink.py`](examples/tasks_file/tasks_file_sink.py).

## Manifest

```python
from horae_sdk import dump_manifest

data = plugin.manifest(
    command=["/opt/plugins/hello/.venv/bin/python", "/opt/plugins/hello/hello_plugin.py"]
)
print(dump_manifest(data))
```

`provides` comes from what the plugin registered, `permissions` from `Plugin(...,
permissions=Permissions(...))`: `intents`, `network` (host names), `secrets` (injected as
environment variables), `read` and `write` paths. Declare the least you need: undeclared
access is a bug, and blocked from M5. The SDK checks the usual mistakes (`ManifestError`);
the core's loader is the judge. The SDK's tests validate what it writes against
`schemas/plugin-manifest/v1.json` and the core's model.

**The command.** The host starts the plugin with an empty working directory and a fixed
`PATH` (`/usr/local/bin:/usr/bin:/bin`). Until Horae installs plugins into their own
environments (M4), pin the command to an **absolute** interpreter that has `horae-sdk` and an
absolute script (or `-m module`). The shipped `examples/hello/plugin.yaml` shows the shape
with `python3`; its end-to-end test pins the interpreter the same way.

## Intents

`horae_sdk.intents` builds the closed set: `note_create`, `note_append_section`,
`note_replace_owned_block`, `task_create`, `task_update_status`, `notify_push`, `http_post`,
plus `fingerprint(*parts)` for stable task identities. Each returns a `dict` checked against
the contract's cheap rules (`IntentError`); the core checks everything again. A note intent
may omit `path` (the core fills it from the `act` entry). Every intent type a plugin
proposes must be in its `permissions.intents`: the SDK refuses an undeclared one before it
leaves the plugin, as the core would refuse the whole step.

## Obsidian Tasks titles

A task title is untrusted data. Obsidian Tasks reads its fields from the raw line
(`- [ ] Pay 📅 2026-10-22 ⏰ 19:00`), so a title from a mail subject holding
`📅 2020-01-01 🔁 every day` would schedule the task. `intents.task_create` therefore
**escapes the title** (`horae_sdk.tasks.escape_title`):

- every signifier (📅 📆 🗓 ⏳ ⌛ 🛫 ➕ ✅ ❌ 🔁 ⏰, the priorities 🔺 ⏫ 🔼 🔽 ⏬, 🆔 ⛔ 🏁) becomes a
  character reference (`&#x1F4C5;`): Obsidian still shows the emoji, Tasks no longer sees it;
- Markdown and Obsidian markup characters (`` \ ` * _ [ ] ( ) < > # | ~ = ! $ % ^ { } & ``, and
  the second `:` of `::`) are backslash-escaped: no link, wikilink, embed, tag, highlight,
  comment, block id or inline field;
- line breaks and control or format characters (bidi, zero-width) are removed, and the title
  is cut to 500 characters on an escape boundary.

The escaping is **idempotent**, so a Tasks sink escapes again without double escaping:
`tasks.task_line(title, due=..., time=...)` and `tasks.task_line_for(intent)` always escape.
`escape=False` is only for a task that never reaches Tasks syntax. `tasks.unescape_title`
gives back what a reader sees (for a notification, say).

## Testing with recorded inputs

A recording is one call as the core makes it and what the plugin must answer:

```json
{
  "tool": "hello.shout",
  "params": {},
  "input": {"greeting": "Hello, Ada!"},
  "expect": {"data": {"greeting": "HELLO, ADA!"}, "intents": []}
}
```

```python
from horae_sdk.testing import replay_all


def test_recordings() -> None:
    replay_all(plugin, Path(__file__).parent / "recordings")
```

Each recording is replayed through MCP (an in-process client sending the same arguments and
`_meta` keys as the host, and reading the result as the host does). A difference raises
`ReplayMismatch` with a diff. `HORAE_SDK_RECORD=1 pytest` writes the current outputs into the
recordings instead (review the diff before committing). `expect: {"error": "..."}` checks a
tool error. `horae_sdk.testing.call` and `operation` are the async primitives, for sinks too.
Recordings must be synthetic: no personal data, no secret. For a source that calls a network
service, give the plugin a fake client in the test.

## Development

The SDK lives in `sdk/` of the core repository, as a uv workspace member: `uv sync` at the
root installs it, and the core's `ruff`, `mypy`, `pytest` and coverage gate cover it. The
end-to-end tests start the examples with the real plugin host. Releases are published to
PyPI from the core repository's `Release SDK` workflow, on a `sdk-v<version>` tag.
