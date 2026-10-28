# Walking skeleton (M1)

A minimal instance that goes through the whole core: a source plugin proposes a note change,
the core routes, diffs, journals and applies it through a sink plugin, and the run can be
undone and replayed (PLAN.md §8, M1). No LLM, no secret, no network.

| File | |
|---|---|
| [`horae.yaml`](horae.yaml) | UTC, `state/`, plugin timeouts |
| [`pipelines/daily-fixture.yaml`](pipelines/daily-fixture.yaml) | `collect` `fixture-source.serve`, then `act` `file-sink.write_note` on `Daily/{{date}}.assistant.md` (`replace-owned`) |
| [`horae.lock`](horae.lock) | pins the two built-in plugins at 0.1.0 |

The plugins are the core's built-in examples:
[`fixture-source`](../../src/horae/plugins/builtin/fixture_source/README.md) serves the
packaged synthetic fixture and proposes a `note.replace_owned_block` of block `fixture`
without a path; the core fills the path from the `act` entry, and
[`file-sink`](../../src/horae/plugins/builtin/file_sink/README.md) writes the bytes the core
computed into the vault. A note travels between the core and `file-sink` in one MCP message,
so it can be at most about 5.95 MiB (`MAX_MCP_NOTE_BYTES`); the daily note here is a few
lines.

## The lock

`horae.lock` is hand-written until `horae plugin add` (M4) writes locks:

- `commit` is a placeholder (40 zeros): the plugins ship inside the core, and a commit of the
  core repository cannot name the commit that contains it. Nothing fetches it; M4's fetch and
  verify (#44, #46) must special-case built-in plugins;
- `file-sink`'s `paths.write` is the vault, `/srv/horae/vault` here: set it to your vault's
  absolute path (in your copy, see below). The end-to-end test builds its lock from this
  file at test time, replacing only that path with its temporary vault;
- `fixture-source` is granted no path, so it serves its packaged fixture only (a `file`
  parameter must be inside a `paths.read` entry of the lock).

## Trying it

`horae init <dir>` writes a copy of this example into a new directory, with a `horae.lock`
whose `file-sink` writes to `<dir>/vault` and the built-in plugins installed in
`<dir>/plugins` (see the [README](../../README.md#getting-started)):

```sh
uv run horae init /tmp/walking-skeleton
uv run horae run daily-fixture -i /tmp/walking-skeleton --dry-run
uv run horae run daily-fixture -i /tmp/walking-skeleton
```

To use this directory instead, **copy it first** (the end-to-end tests read these files, and
`state/` and `plugins/` must not be created inside the checkout), set `file-sink`'s
`paths.write` in the copy's `horae.lock` to your vault, and install the plugins:
`python -c 'from pathlib import Path; from horae.plugins.builtin import install;
install(Path("/tmp/copy/plugins"))'`. `horae diff`, `horae undo` and `horae run` then go
through those plugins; `horae diff --vault` / `horae undo --vault` use the built-in local
sinks instead (whose identity differs from `file-sink`'s, so undo refuses, `sink_mismatch`, a
run applied through the plugin).

`state/` and `plugins/` are created next to the files of your copy; they are not part of the
example.
