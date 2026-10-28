# SPDX-License-Identifier: Apache-2.0
"""End to end: the M1 walking skeleton with real plugin subprocesses (PLAN.md §8 M1, #25).

`examples/walking-skeleton` (its `horae.yaml` and pipeline; its `horae.lock` with the vault
replaced by a temporary one) runs through the real stack: `open_ports` builds the
`HostRegistry`, the `PluginHost` and the MCP sinks; `fixture-source` and `file-sink` run as
stdio MCP subprocesses (`horae.plugins.builtin.install` pins them to this interpreter).

M1 is done when "a fixture pipeline writes a note, `--dry-run` shows the diff and writes
nothing, `undo` restores it, and a replay gives the same output": one test walks these steps
in order, on one instance. The dry run is under the write guard of `write_guard.py`, which
allows only Horae's own state (the store, the locks, the host's per-run directories) and the
two plugin processes; their own writes are below the guard, so the vault is also compared
byte for byte. No network: the plugins are local processes and touch only the vault.

The module must stay under 60 seconds in CI (#25 acceptance criterion): a module fixture
measures it and fails the module's last test past `MAX_MODULE_S`. Each step starts at most two
plugin processes.
"""

import asyncio
import datetime
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.apply import plan_changes, undo_run
from horae.config import Instance, load_instance
from horae.contracts import Lockfile, RunRecord
from horae.plugins.builtin import BUILTIN, install
from horae.plugins.host import HostRegistry, McpSinks, host_for_instance
from horae.replay import LockRegistry, replay
from horae.runner import Runner, open_ports
from horae.store import Store
from write_guard import arm, tree, under

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "walking-skeleton"
PIPELINE = "daily-fixture"
START = datetime.datetime(2026, 10, 28, 6, 0, tzinfo=datetime.UTC)
NOTE = "Daily/2026-10-28.assistant.md"
ORIGINAL = b"# 2026-10-28\r\n\r\nMy own words, kept byte for byte.\r\n"
BLOCK = (
    b"<!-- horae:begin fixture -->\r\n"
    b"- [ ] Water the plants (fx-1)\r\n"
    b"- [ ] Read chapter 3 (fx-2)\r\n"
    b"<!-- horae:end fixture -->\r\n"
)


MAX_MODULE_S = 60.0


@pytest.fixture(scope="module", autouse=True)
def _module_under_a_minute() -> Iterator[None]:
    """Fail (at the module's teardown) when the module took `MAX_MODULE_S` or more."""
    start = time.monotonic()
    yield
    elapsed = time.monotonic() - start
    assert elapsed < MAX_MODULE_S, f"the end-to-end module took {elapsed:.1f} s"


def _clock() -> datetime.datetime:
    return START


def example_lock(vault: Path) -> dict[str, Any]:
    """The example `horae.lock`, with file-sink's vault replaced by `vault`."""
    data: dict[str, Any] = yaml.safe_load((EXAMPLE / "horae.lock").read_text(encoding="utf-8"))
    data["plugins"]["file-sink"]["permissions"]["paths"]["write"] = [str(vault)]
    return data


def build(tmp_path: Path) -> tuple[Path, Path]:
    """An instance made from the example (with its plugins installed) and a vault holding the
    day's note, written by its user (CRLF line endings, mode 0640)."""
    vault = tmp_path / "vault"
    (vault / "Daily").mkdir(parents=True)
    (vault / NOTE).write_bytes(ORIGINAL)
    os.chmod(vault / NOTE, 0o640)
    root = tmp_path / "instance"
    root.mkdir()
    shutil.copy(EXAMPLE / "horae.yaml", root / "horae.yaml")
    shutil.copytree(EXAMPLE / "pipelines", root / "pipelines")
    lock = yaml.safe_dump(example_lock(vault), sort_keys=False)
    (root / "horae.lock").write_text(lock, encoding="utf-8")
    install(root / "plugins")
    return root, vault


def _contents(root: Path) -> dict[str, tuple[bytes | None, int]]:
    """Bytes and modes under `root` (mtimes change when undo rewrites a note)."""
    return {name: (data, mode) for name, (data, mode, _) in tree(root).items()}


def _plugin_process(args: Any) -> bool:
    argv = [os.fsdecode(arg) for arg in args] if isinstance(args, (list, tuple)) else []
    return argv[:2] == [sys.executable, "-m"] and argv[2:] in ([m] for m in BUILTIN.values())


async def _run(instance: Instance, *, dry_run: bool) -> tuple[RunRecord, Any]:
    """Run the pipeline through the live ports; for a dry run also plan its diff."""
    async with open_ports(instance, instance.root / "plugins") as live:
        with Store.open(instance.store_path) as store:
            runner = Runner(instance, store, live.ports, clock=_clock)
            record = await runner.run(PIPELINE, dry_run=dry_run)
            changes = await plan_changes(record, live.sinks) if dry_run else None
    return record, changes


async def _undo(instance: Instance, run_id: str, sinks_for: Any = None) -> Any:
    async with open_ports(instance, instance.root / "plugins") as live:
        sinks = live.sinks if sinks_for is None else sinks_for(live)
        with Store.open(instance.store_path) as store:
            return await undo_run(store, run_id, sinks, clock=_clock, call_timeout_s=30)


def test_walking_skeleton_run_dry_run_undo_and_replay(tmp_path: Path) -> None:
    root, vault = build(tmp_path)
    instance = load_instance(root)
    original = _contents(vault)
    configuration = {name: tree(root / name) for name in ("pipelines", "plugins")}
    Store.open(instance.store_path).close()  # created (and migrated) before the guard

    # 1. --dry-run shows the diff and writes nothing (only Horae's own state).
    state = instance.state_dir
    db = os.path.realpath(instance.store_path)
    state_files = {db + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    state_files.add(os.path.realpath(state))  # `mkdir(exist_ok=True)` of the state directory
    own = under(state / "locks", state / "plugin-runs")
    with pytest.MonkeyPatch.context() as patch:
        seen = arm(patch, lambda real: real in state_files or own(real), _plugin_process)
        dry, changes = asyncio.run(_run(instance, dry_run=True))
    assert seen == [], f"the dry run tried to write: {seen}"
    assert _contents(vault) == original
    assert dry.dry_run
    assert dry.status == "ok"
    assert dry.changes == []
    assert changes.problems == ()
    assert changes.refused is None
    (diff,) = changes.files
    assert diff.path == NOTE
    assert diff.status == "modify"
    assert diff.diff is not None
    assert "+- [ ] Water the plants (fx-1)\r\n" in diff.diff  # the note's line endings
    assert (vault / NOTE).read_bytes() == ORIGINAL

    # 2. A run writes the note (through file-sink's subprocess).
    run, _ = asyncio.run(_run(instance, dry_run=False))
    assert run.status == "ok", run.error
    assert [change.target for change in run.changes] == [NOTE]
    (intent,) = run.intents
    assert intent.applied_by == "file-sink.write_note"
    assert intent.proposed_by == "fixture-source.serve"
    assert (vault / NOTE).read_bytes() == ORIGINAL + b"\r\n" + BLOCK
    assert diff.after_sha256 is not None
    assert os.stat(vault / NOTE).st_mode & 0o777 == 0o640

    # 3. A replay gives the same output (the dry run's and the run's).
    pipeline = instance.pipelines[PIPELINE]
    for record in (dry, run):
        result = replay(
            record,
            pipeline,
            plugins=LockRegistry(instance.lock, pipeline, record),
            lock_digest=instance.lock_digest(),
            config_digest=instance.config_digest(),
        )
        assert result.outcome == "identical", (result.error, result.intents_diff)

    # 4. undo restores the note byte for byte (and its mode).
    undone = asyncio.run(_undo(instance, run.id))
    assert undone.refused is None
    assert undone.complete
    (note,) = undone.notes
    assert note.target == NOTE
    assert note.sink_identity is not None
    assert note.sink_identity.startswith("mcp:file-sink:local:")
    assert _contents(vault) == original
    with Store.open(instance.store_path) as store:
        summary = store.run_summary(run.id)
        assert summary is not None
        assert summary.status == "undone"
    assert {name: tree(root / name) for name in ("pipelines", "plugins")} == configuration


def test_undo_refuses_another_vault(tmp_path: Path) -> None:
    root, vault = build(tmp_path)
    instance = load_instance(root)
    run, _ = asyncio.run(_run(instance, dry_run=False))
    assert run.status == "ok", run.error
    written = (vault / NOTE).read_bytes()
    other = tmp_path / "other-vault"
    (other / "Daily").mkdir(parents=True)
    lock = Lockfile.model_validate(example_lock(other))

    def elsewhere(live: Any) -> McpSinks:
        registry = HostRegistry.from_directory(lock, root / "plugins")
        return McpSinks(registry, live.host)

    result = asyncio.run(_undo(instance, run.id, elsewhere))
    assert result.refused is not None
    assert result.refused.kind == "sink_mismatch"
    assert (vault / NOTE).read_bytes() == written
    assert list(other.rglob("*")) == [other / "Daily"]


def test_the_example_instance_is_valid(tmp_path: Path) -> None:
    instance = load_instance(EXAMPLE)
    assert sorted(instance.pipelines) == [PIPELINE]
    assert sorted(instance.lock.plugins) == sorted(BUILTIN)
    install(tmp_path / "plugins")
    registry = HostRegistry.from_directory(instance.lock, tmp_path / "plugins")
    assert registry.plugin_for_tool("fixture-source.serve").manifest.name == "fixture-source"
    assert registry.plugin_for_tool("file-sink.write_note").manifest.name == "file-sink"
    assert host_for_instance(instance, registry).timezone == "UTC"
