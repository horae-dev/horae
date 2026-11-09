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
`ns.restore`, `ns.restore_status` and `ns.parent_exists` (the SDK registers them; they are
listed in `tools/list` but must not be in `provides`). Implement `identity(paths)` and
`apply(request)`; a note sink also implements `read`, `restore` and `parent_exists`.
`restore(paths, path, content, expected_sha256, force=False)` takes an optional `force` (SDK
0.4): true only when the user ran `horae undo --force`, it lets the sink also restore a note
it would refuse for being edited outside Horae's owned block; it never lifts the
compare-and-swap, and a `restore` without the parameter is still called (without it). To make
a `task.update_status` undoable, answer it with `Applied(..., details={"changed": True, ...})`
holding what you need to put the task back (the line you replaced), and implement
`restore_status(paths, details, force=False)`: the core journals those details, hands them back
untouched to `horae undo`, and your sink returns whether it wrote, or raises
`SinkError("target_changed")` when the task was edited since (unless `force`). Without
`restore_status` the change is listed as not undoable, as before. For a `note.*` intent **the core
computes the bytes** (`request.content`): write exactly those, atomically, only if the note
still has `request.expected_before_sha256` (else `SinkError("target_changed", ...)`), and
return `Applied.wrote(path, content)`. Other intents are applied by the sink itself; their
text is data. See [`examples/tasks_file/tasks_file_sink.py`](examples/tasks_file/tasks_file_sink.py).

### Upgrading a sink to 0.4.0 (release notes)

- **Override `restore` with the `force` keyword.** `Sink.restore` now has the signature
  `restore(self, paths, path, content, expected_sha256, force=False)`. A subclass that overrides
  it with the old four-parameter signature still runs (the SDK calls it without `force`), but
  **mypy rejects the override** (an incompatible override of the supertype) until you add
  `force: bool = False`. Do the same for `restore_status(self, paths, details, force=False)`
  if you implement it. `force` is true only for the user's `horae undo --force`; a sink with no
  owned-block rule of its own may accept it and ignore it, as the built-in `file-sink` does.
- **`<ns>.restore_status` is a reserved operation name.** Like `<ns>.read`, `.apply`,
  `.restore`, `.parent_exists` and `.identity`, it is now a tool only the core calls. A plugin
  that already provides its own tool called `<ns>.restore_status` (in `provides`) is **rejected
  when it is loaded**: rename that tool, and implement `Sink.restore_status` instead if you want
  `horae undo` to put a task status back.

## Checking the approved hosts

Since 0.5 the core sends the plugin's `permissions.network` **as approved in `horae.lock`**
(not the manifest's declared list) with every call, so a plugin can refuse a host the user did
not approve, even before M5 isolation (the egress proxy) exists. In a source or transform:

```python
@plugin.source("web.fetch")
def fetch(call: Call, url: str) -> Output:
    allowed = call.host_allowed(url)  # True, False, or None
    if allowed is False:
        raise PluginError("that host is not approved in horae.lock")
    ...
```

In a sink or notifier, `request.host_allowed(url)` on the `ApplyRequest` does the same.
`call.allowed_hosts` (`request.network.hosts`) is the list itself: a tuple, `()` when the lock
grants no host.

- `True`: `url` is an `https` URL whose host is one of the approved names. `False`: anything
  else, including an empty list.
- **`None` means unknown**: the core is older than this SDK and sent no list. That is not
  "refused": the plugin decides. Fall back to what you did before, your own configuration
  (the server or hosts the pipeline's trusted parameters name), and never treat `None` as
  "approved by the user".
- The matching is the lock's, nothing looser: names are lowercase and compared as written
  (no case folding, no trailing dot, no wildcard, no implicit subdomains); a non-ASCII name
  must be written as its `xn--` A-label; any port is accepted on an approved host; user info
  (`user@host`), a backslash, whitespace, a fragment, an IP literal, `localhost` and any scheme
  but `https` never match. Check the URL you are about to connect to, after any redirect.
- A plugin cannot influence the list: it travels in the request's `_meta["horae/network"]`,
  which the core sets, and a transform's input is under another key.

`horae_sdk.testing.call(..., network=[...])` (and a recording's `"network"` key) sends a list;
the default is `[]`, as the core sends for a plugin with no approved host, and `network=None`
(`"network": null`) sends none, as an older core does.

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
environment variables; the plugin does not start without them), `optional_secrets` (injected
only when they resolve; read them with `os.environ.get` and report `secret_missing` yourself
when a call needs one that is absent), `read` and `write` paths. Declare the least you need: undeclared
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
plus `fingerprint(*parts)` for stable task identities (`task_create(title, key=...)`, with an
optional `https://` `link=`, see [Task links](#task-links)). Each
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
(`- [ ] Pay ⏰ 19:00 📅 2026-10-22`), so a title from a mail subject holding
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
`tasks.task_line(title, due=..., time=..., link=..., done=..., fingerprint=...)` and
`tasks.task_line_for(intent, block_id=False)` always escape,
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

### Task links

`intents.task_create(title, key=..., link=...)` (SDK 0.2.0) attaches a link to the item the
task comes from, e.g. a pull request. The link is an absolute `https://` URL of at most 2048
characters whose host is a name, never an IP address (`127.0.0.1`, `127.1`, `[::1]`), with no
user info (`user@`), `%` or brackets in the authority, no backslash, and no control, format
(bidi, zero-width), space or separator character (Unicode categories `Cc`, `Cf`, `Zs`, `Zl`,
`Zp`) or lone surrogate anywhere (`tasks.is_task_link`, the core's `TaskLink`, schemas
decision 52); `obsidian://` and every other scheme are refused. It is kept in the intent exactly as given, and the core routes it
only to a `mode: tasks` entry whose trusted `link_prefix` it falls under (for example
`link_prefix: https://github.com/my-org/`, with a clean path ending in `/`), as for
`notify.push`; with no `link_prefix`, a task with a link is never routed. A `tasks` entry's
`link_prefix` may also be a list of 1 to 32 prefixes (schemas decision 60); with overlapping
entries the longest matching prefix wins. The host is
compared as a DNS origin (case-insensitively, one trailing dot ignored, `:443` equal to no
port), the path case-sensitively. A link proposed by an LLM skill must also be a URL of the
input the model was shown (the action guard).

**Upgrading a source to send `link`.** Once a source sends `link`, every `mode: tasks` entry
that receives its tasks needs a `link_prefix` covering those links, or the step fails at run
time with `intent_unrouted` (`task link not under a trusted link_prefix`). Add the prefix to
the pipelines before releasing the source. (Since decision 60 a task whose link is under no
prefix is skipped instead, and recorded as an `intent_skipped` violation in the run record, so
one off-owner item no longer fails the step.) The entry's `link_prefix` reaches a Tasks sink in
its apply call's `params`, like `mode`: a sink must accept it and may ignore it.

`tasks.task_line(title, link=...)` and `task_line_for(intent)` write it right after the
escaped title, before the date fields, as a Markdown link with the fixed text `link`:

```
- [ ] Review PR 42 [link](https://github.com/my-org/app/pull/42) 📅 2026-10-22
```

The title cannot open, close or fake the link (its brackets and parentheses are escaped), and
the destination is `tasks.link_destination(link)`, the same URL made inert in Markdown: the
characters that would end or open a link or start other markup (`( ) [ ] < >`, backtick, `$`,
`"`, `{ } | ^`) and every non-ASCII character are percent-encoded (UTF-8), a `%` that starts
no escape becomes `%25`, the `&` of an HTML entity becomes `%26` (Markdown decodes entities in
a destination, so `&#x2E;&#x2E;` could otherwise become `..` after routing checked the path),
and the second `:` of `::` and `=` of `==` become `%3A` / `%3D`. The encoding is idempotent and
percent-decodes to the same string as the link does. Tasks reads the link as part of the
description, never as a field. A destination longer than `tasks.MAX_DESTINATION` (4096)
characters, possible for a link of mostly non-ASCII characters (up to 12 characters each once
encoded), is left out: the line is written without the link, so it stays well under a reader's
size cap (plugin-obsidian analyses at most 16 KiB).

### Reminder order and the task block id

`tasks.task_line` writes Horae's `⏰ HH:MM` reminder right after the title (and link), **before**
the Tasks date fields (SDK 0.3.0; 0.2.0 wrote it last). The Tasks plugin reads fields from the
end of the line and stops at a signifier it does not know, so a trailing `⏰` would hide the
due date from Tasks queries; written first, Tasks sees `📅` and keeps `⏰ 19:00` in the
description:

```
- [ ] Pay the bill ⏰ 19:00 📅 2026-10-22 ^h-3f2a9c01b7de
```

`tasks.task_block_id(fingerprint)` is the hidden link from a line back to the task that
created it: `h-` and the first 12 hex digits of the SHA-256 of the fingerprint (a
`task.create` fingerprint, `ValueError` otherwise). `task_line(..., fingerprint=...)` and
`task_line_for(intent, block_id=True)` end the line with it as `^h-…`, an Obsidian block id:
hidden in reading view, ignored by Tasks, and kept when the text is edited. It is a one-way
digest, so a reader that needs the fingerprint back keeps its own index from block id to
fingerprint (plugin-obsidian does, under its `paths.write`). Without `fingerprint` (the
default) no block id is written. A title cannot fake one: its `^` is escaped and the real
block id is always last.

The `h-` prefix makes the id unlikely to collide with a user's own block ids. Two different
fingerprints can still share a block id: it keeps 48 bits, so the chance is about 2e-5 at
100,000 tasks (negligible, not zero). A sink's block-id to fingerprint index must detect a
second fingerprint mapping to an id it already holds and **fail closed** (refuse the write)
rather than overwrite the first entry, which would point a line at the wrong task.

#### Migrating from 0.2.0 (line order)

0.2.0 wrote `- [ ] Pay 📅 2026-10-22 ⏰ 19:00`; 0.3.0 writes `- [ ] Pay ⏰ 19:00 📅 2026-10-22`.
The trailing `⏰` (not a Tasks field) stopped Tasks reading fields from the end of the line, so
the due date was invisible to Tasks queries. Readers must accept **both** orders (plugin-obsidian's
parser does). Lines already written are **not rewritten** (a decision): their due date stays
invisible to Tasks until the line is edited and rewritten in the new order.

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
5. Task links: write `tasks.task_line("Review", link=LINK, due="2030-01-01")` for
   `LINK = "https://example.org/a(b)/c::d?q=x==y&amp;z#f"`. The description shows one
   clickable `link` that opens `https://example.org/a%28b%29/c:%3Ad?q=x=%3Dy%26amp;z#f`,
   the task is still due 2030-01-01, and no inline field or highlight appears.

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
