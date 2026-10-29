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

To start a new plugin repository, use the
[plugin template](../docs/plugins/template.md) (`horae-dev/template-plugin`): an example
plugin, `plugin.yaml`, recorded-input tests and CI that pass as soon as it is created.

[`examples/hello/hello_plugin.py`](examples/hello/hello_plugin.py), a source and a transform:

```python
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, intents

plugin = Plugin("hello", version="0.1.0", permissions=Permissions(intents=["task.create"]))


@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output:
    task = intents.task_create(f"Greet {name} back", key=intents.fingerprint("hello", name))
    return Output({"greeting": f"Hello, {name}!"}, intents=[task])


@plugin.transform("hello.shout")
def shout(call: Call) -> Output:
    # call.input is the previous step's data: untrusted (it may be a mail)
    data: dict[str, Any] = call.input if isinstance(call.input, dict) else {}
    loud = {key: value.upper() if isinstance(value, str) else value for key, value in data.items()}
    subject = data.get("subject")
    if not isinstance(subject, str):
        return Output(loud)
    key = intents.fingerprint("hello", "reply", subject)
    return Output(loud, intents=[intents.task_create(f"Reply to {subject}", key=key)])


if __name__ == "__main__":
    plugin.run()
```

A pipeline then calls `hello.greet: { name: Ada }` in a `collect` step and `hello.shout` in a
`transform` step. The untrusted `subject` reaches the task only through `task_create`, which
escapes it ([Obsidian Tasks titles](#obsidian-tasks-titles)).

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
plus `fingerprint(*parts)` for stable task identities (`task_create(title, key=...)`). Each
returns a `dict` checked against the contract's cheap rules (`IntentError`; dates must be real
calendar dates); the core checks everything again.

`fingerprint("github", "pr", "42")` is `github:pr:42`. When a part holds a character outside
`A-Z a-z 0-9 . _ -`, is empty, or the result would pass 128 characters, the readable form is
lossy (`Re: x!` and `Re: x?` read the same), so the fingerprint is that readable form, cut to
110 characters, then `::` and 16 hex digits of the SHA-256 of the parts as a JSON array:
`mail:Re--x-::c900d574653a3029`. A clean fingerprint never holds `::` and never changes.

The SDK also refuses, before sending, a result the host would refuse: more than 256 intents
(the core refuses the whole step) or a message over the host's 8 MiB (`MAX_MESSAGE_BYTES`,
which would cost the plugin its process). A note intent
may omit `path` (the core fills it from the `act` entry). Every intent type a plugin
proposes must be in its `permissions.intents`: the SDK refuses an undeclared one before it
leaves the plugin, as the core would refuse the whole step.

## Obsidian Tasks titles

A task title is untrusted data. Obsidian Tasks reads its fields from the raw line
(`- [ ] Pay 📅 2026-10-22 ⏰ 19:00`), so a title from a mail subject holding
`📅 2020-01-01 🔁 every day` would schedule the task. `intents.task_create` therefore
**escapes the title** (`horae_sdk.tasks.escape_title`, threat model T02, schemas decision 50):

- every signifier (📅 📆 🗓 ⏳ ⌛ 🛫 ➕ ✅ ❌ 🔁 ⏰, the priorities 🔺 ⏫ 🔼 🔽 ⏬, 🆔 ⛔ 🏁) becomes a
  character reference (`&#x1F4C5;`): Obsidian still shows the emoji, Tasks no longer sees it;
- Markdown and Obsidian markup characters (`` \ ` * _ [ ] ( ) < > # | ~ = ! $ % ^ { } & ``) are
  backslash-escaped: no link, wikilink, embed, tag, math, block id or HTML;
- no pair is left: the second `:` of `::` is backslash-escaped (no inline field), the second
  `%` of `%%` and `=` of `==` become `&#x25;` and `&#x3D;` (no comment, no highlight, even for
  a parser that ignores backslashes there: those two are Obsidian extensions, not CommonMark).
  The check looks at what was written, so an escape already in the title (`\::`) or a
  zero-width character in between (`:` ZWSP `:`) cannot hide a pair;
- no bare link: the `:` of a URL scheme (`https:`, `obsidian:`, `javascript:`, `mailto:`) and
  the `.` of `www.` are backslash-escaped, so Obsidian does not autolink them (`10:30` and
  `Re: x` are left alone);
- line breaks and control or format characters (bidi, zero-width) are removed, and the title
  is cut to 500 characters **after** escaping, on an escape boundary, never between a
  character and its combining marks or emoji modifier (not a full grapheme segmentation: a
  flag made of two regional indicators may be split). A title with nothing printable left
  is `(untitled)` (escaped): the builder never raises on a title, so one hostile item never
  aborts a whole `collect`.

**The intent's title is the Tasks-safe form**, not the plain text (the acceptance criterion
of #42 puts the escaping in the builder). A sink writing Tasks syntax writes it as it is:
`tasks.task_line(title, due=..., time=...)` and `tasks.task_line_for(intent)` always escape,
and the escaping is **idempotent**, so a title the builder escaped is unchanged. A sink for
any other target (a push notification, a log) shows `tasks.plain_text(title)`: references and
backslash escapes resolved, **for plain-text display only**. Never feed `plain_text` to a
Markdown or Tasks target (it is the hostile text again), and it is not an exact inverse of
the escaping (line breaks, controls and a cut tail are gone). `escape=False` is only for a task
that never reaches Tasks syntax. Follow-up (decision 50): escape at the sink only and keep the
plain title in the intent.

Idempotence has a price: a backslash escape already in the title (`\#`) and a reference in
the form the SDK writes (`&#x1F4C5;`, `&#x25;`, `&#x3D;`: uppercase hex, of a signifier, `%` or
`=`) are kept, and show as the character, never as markup. Any other reference (`&amp;`,
lowercase `&#x1f4c5;`) has its `&` and `#` escaped and shows literally.

### Manual check in a real vault

The tests port the Tasks plugin's own field regexes (emoji and Dataview formats, with the
optional U+FE0F) and check that an escaped title never matches them; they cannot run
Obsidian. Before a release that changes `horae_sdk.tasks`, check by hand:

1. In a scratch vault with the **Tasks** plugin (and Dataview, if you use it), write the line
   `tasks.task_line(TITLE, due="2030-01-01")` gives for the title
   `📅 2020-01-01 🔁 every day #t [[x]] %%c%% ==h== https://example.org www.example.org`:

   ```
   - [ ] &#x1F4C5; 2020-01-01 &#x1F501; every day \#t \[\[x\]\] \%&#x25;c\%&#x25; \=&#x3D;h\=&#x3D; https\://example.org www\.example.org 📅 2030-01-01
   ```
2. In reading view and in Live Preview, the description reads
   `📅 2020-01-01 🔁 every day #t [[x]] %%c%% ==h== https://example.org www.example.org`,
   all as plain text: **no due date** from the title (only 2030-01-01), **no recurrence**,
   **no tag** (`#t` is not in the tag pane), **no link** (no `x` in the graph or backlinks, no
   clickable URL), **no comment** (`c` is shown), **no highlight**.
3. A ```` ```tasks ```` query `due before 2025-01-01` does not list it; `is recurring` does not
   list it; `tags include #t` does not list it; the task's edit modal shows due 2030-01-01
   and no recurrence.
4. With Dataview, `TASK WHERE due` shows the task with its 2030-01-01 date only.

Record the Obsidian, Tasks and Dataview versions checked in the release notes.

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
recordings instead and names each rewritten file on stderr (review the diff before
committing); it is refused when `CI` or `GITHUB_ACTIONS` is set, and never writes through a
symbolic link. `expect` holds `data` and `intents`, or `{"error": "..."}` alone (a substring of
a tool error): any other key is refused. `replay` also works inside a running event loop;
`areplay` and `areplay_all` are the async variants, `horae_sdk.testing.call` and `operation`
the async primitives, for sinks too.
Recordings must be synthetic: no personal data, no secret. For a source that calls a network
service, give the plugin a fake client in the test.

## Development

The SDK lives in `sdk/` of the core repository, as a uv workspace member: `uv sync` at the
root installs it, and the core's `ruff`, `mypy` and `pytest` cover it; CI gates its coverage
(85%) separately from the core's. The tests need the core (its host, contracts and schemas),
so they are not in the sdist. The
end-to-end tests start the examples with the real plugin host. Releases are published to
PyPI from the core repository's `Release SDK` workflow, on a `sdk-v<version>` tag.
