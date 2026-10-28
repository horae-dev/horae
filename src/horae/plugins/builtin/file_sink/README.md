# `file-sink` (built-in example plugin)

A sink that writes notes to a **local vault directory**. It exists for tests and examples
(PLAN.md M1, #25): the walking skeleton in
[`examples/walking-skeleton`](../../../../../examples/walking-skeleton/) and
[`tests/test_e2e.py`](../../../../../tests/test_e2e.py) use it.

## Entry

`file-sink.write_note` (an `act` sink) applies `note.create`, `note.append_section` and
`note.replace_owned_block`, routed by the entry's required `mode` and bound to its `path`:

```yaml
steps:
  - act:
      - file-sink.write_note: { path: "Daily/{{date}}.assistant.md", mode: replace-owned }
```

## The vault

v1 has no per-plugin configuration in `horae.yaml`, so the vault is the plugin's path
permission **as approved in `horae.lock`**: exactly one `permissions.paths.write` entry, an
absolute path. The manifest asks for none (where your vault is is your choice); the lock
grants it, and the core passes the approved paths with every sink operation.

```yaml
# horae.lock
plugins:
  file-sink:
    permissions:
      paths: { write: [/srv/horae/vault] }
      intents: [note.append_section, note.create, note.replace_owned_block]
```

## What it does, and does not, decide

**The core computes every note's bytes** (`horae.apply.notes`: ownership of `*.assistant.md`
notes, block markers, line endings), so the sink writes exactly what the dry-run diff showed.
The plugin implements the sink operations of `horae.plugins.host.sink`, which only the core
calls (they are not in `provides`, so no pipeline step can):

| Tool | Does |
|---|---|
| `file-sink.read` | the note's bytes (base64), or `null` |
| `file-sink.apply` | writes the core's bytes if the note still has the planned SHA-256 (`target_changed` otherwise) |
| `file-sink.restore` | the same for `horae undo`; `content: null` removes the note |
| `file-sink.parent_exists` | whether the note's directory exists |
| `file-sink.identity` | `local:<st_dev>:<st_ino>:<real path>` of the vault, so undo refuses another vault |

Reads and writes reuse the built-in `LocalFileSink` (`horae.apply.local`), so the safety rules
are the same: note paths only (no `..`, no hidden segment), no symbolic link on any component,
a single hard link, a regular file, a temporary file `fsync`ed then renamed over the note
relative to its directory descriptor, the note's mode kept. Errors come back as
`{"error": {"kind", "message"}}` (`symlink`, `hard_link`, `not_a_file`, `target_changed`…).

**Size.** A note goes over MCP base64-encoded, in one JSON-RPC message, and the host caps a
plugin's message at 8 MiB: so a note is at most `MAX_MCP_NOTE_BYTES` (6,242,304 bytes, about
5.95 MiB) either way, less than the built-in local sink's 16 MiB. Reading a larger note
returns `note_too_large` (the plugin keeps running), and the core refuses to send larger
content.

## Running it

The server is `python -m horae.plugins.builtin.file_sink` (the `mcp` SDK, stdio). As for
`fixture-source`, `horae.plugins.builtin.install(directory)` writes its manifest with the
command pinned to the running interpreter, until M4 gives each plugin its own environment.
Until M5 the plugin is not isolated: like every v1 plugin it runs as your user ("declared"
tier, ADR 0003), which is acceptable for first-party plugins only.
