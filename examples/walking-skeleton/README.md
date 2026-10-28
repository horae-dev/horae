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

`horae run` is #26. Until then, from Python (an editable checkout, `uv sync`). **Copy the
example first** and edit the copy, so the repository's files stay as they are (the end-to-end
test reads them) and `state/` and `plugins/` are not created inside the checkout:

```sh
cp -R examples/walking-skeleton /tmp/walking-skeleton
# then set file-sink's paths.write in /tmp/walking-skeleton/horae.lock to your vault
```

```python
import asyncio
from pathlib import Path

from horae.config import load_instance
from horae.plugins.builtin import install
from horae.runner import Runner, open_ports
from horae.store import Store

root = Path("/tmp/walking-skeleton")  # your copy, its horae.lock pointing at your vault
install(root / "plugins")  # manifests pinned to this interpreter
instance = load_instance(root)


async def main(dry_run: bool) -> None:
    async with open_ports(instance, root / "plugins") as live:
        with Store.open(instance.store_path) as store:
            record = await Runner(instance, store, live.ports).run("daily-fixture", dry_run=dry_run)
            print(record.id, record.status, [c.target for c in record.changes])


asyncio.run(main(dry_run=False))
```

Then `horae diff`, `horae replay` and `horae runs` work on the stored runs
(`-i /tmp/walking-skeleton`). `horae undo` still writes with the built-in local sink
(`--vault`), whose identity differs from `file-sink`'s, so it refuses (`sink_mismatch`) a run
applied through the plugin until #26 wires undo to the plugin host: from Python, undo with
`horae.apply.undo_run(store, run_id, live.sinks, clock=...)` inside `open_ports` (it fetches
the sink's identity itself), as [`tests/test_e2e.py`](../../tests/test_e2e.py) does.

`state/` and `plugins/` are created next to the files of your copy; they are not part of the
example.
