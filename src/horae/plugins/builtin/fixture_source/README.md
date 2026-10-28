# `fixture-source` (built-in example plugin)

A source that serves **deterministic synthetic data** from a JSON fixture file. It exists for
tests and examples (PLAN.md M1, #25): the walking skeleton in
[`examples/walking-skeleton`](../../../../../examples/walking-skeleton/) and
[`tests/test_e2e.py`](../../../../../tests/test_e2e.py) use it. It needs no network, no
secret and no path permission.

## Tool

`fixture-source.serve` (a `collect` source), one optional parameter:

| Parameter | Default | Meaning |
|---|---|---|
| `file` | the packaged [`data.json`](data.json) | absolute path of a JSON fixture file |

```yaml
steps:
  - collect:
      - fixture-source.serve: {}                                  # the packaged fixture
      - fixture-source.serve: { file: /srv/fixtures/week.json }   # your own
```

A fixture file is an object with two optional keys:

- `data` (object, default `{}`): returned as the tool's output (`structuredContent`);
- `intents` (list, default `[]`): proposed as they are, through
  `_meta["dev.horae/intents"]`. The core validates them against the plugin's
  `permissions.intents` (`note.replace_owned_block` only) and routes them; a note intent may
  omit `path`, which the core fills from the `act` entry.

The answer depends only on the file (no clock, no environment), so a run replays identically.
The file is read once per call (at most 1 MiB) and never written; a missing, oversized or
malformed file is a tool error (`tool_error`).

## Running it

The server is `python -m horae.plugins.builtin.fixture_source` (the `mcp` SDK, stdio). The
packaged [`plugin.yaml`](plugin.yaml) says `python3`, which the plugin host looks up on its
fixed `PATH`, where Horae is usually not installed: `horae.plugins.builtin.install(directory)`
writes the manifest with the command pinned to the running interpreter, until M4 gives each
plugin its own environment.
