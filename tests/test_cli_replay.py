# SPDX-License-Identifier: Apache-2.0
"""`horae replay <run-id>`: reads the instance and the store (read-only), and exits
0 identical, 1 configuration or store problem, 2 usage, 3 not comparable, 4 different."""

import asyncio
import datetime
import json
import re
import socket
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli
from horae.cli import app
from horae.config import STORE_FILE, load_instance
from horae.contracts import RunRecord
from horae.engine import LlmBackend, PreviousLlm, ToolResult, execute, make_context
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.store import Store

runner = CliRunner()
START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
RUN = "20261027T053000Z-flow-01"
COMMIT = "c" * 40
PERMISSIONS = {
    "src": ["notify.push"],
    "sink": ["note.create", "notify.push"],
    "skills": ["note.create", "notify.push"],
}
PROVIDES: dict[str, dict[str, list[str]]] = {
    "src": {"sources": ["src.read"], "transforms": ["src.shape"]},
    "sink": {"sinks": ["sink.write"], "notifiers": ["sink.notify"]},
    "skills": {"skills": ["skills/rank"]},
}
FLOW: dict[str, Any] = {
    "name": "flow",
    "steps": [
        {"collect": [{"src.read": {"day": "{{date}}"}}]},
        {"transform": ["src.shape"]},
        {
            "llm": {
                "skill": "skills/rank",
                "model": "default",
                "budget": {"usd": 1.0, "tokens_out": 99},
            }
        },
        {"act": [{"sink.write": {"path": "Notes/{{date}}.md", "mode": "create"}}]},
        {"notify": ["sink.notify"]},
    ],
}
CREATE = {"type": "note.create", "content": "hello"}
PUSH = {"type": "notify.push", "body": "1 note"}
KEYS = {
    "schema_version",
    "run_id",
    "pipeline",
    "outcome",
    "identical",
    "against_current",
    "definition",
    "pipeline_digest",
    "lock_digest",
    "config_digest",
    "error",
    "mismatches",
    "notes",
    "intents_diff",
    "summary",
}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["replay", *args])
    return result.exit_code, _plain(result.output)


def _json(*args: str) -> tuple[int, dict[str, Any]]:
    code, out = _invoke(*args, "--json")
    report = json.loads(out)
    assert set(report) == KEYS  # every key, also for errors
    return code, report


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here fails if anything opens a socket or starts a process."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("replay touched the network or started a process")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def _plugins(
    provides: dict[str, dict[str, list[str]]], permissions: dict[str, list[str]]
) -> FixturePlugins:
    plugins = FixturePlugins()
    for name, intents in permissions.items():
        manifest = {
            "name": name,
            "version": "0.1.0",
            "horae": ">=0.1",
            "provides": provides[name],
            "permissions": {"intents": intents},
        }
        plugins.add(manifest, commit=COMMIT)
    return plugins


def _flow_plugins() -> FixturePlugins:
    plugins = _plugins(PROVIDES, PERMISSIONS)
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data={"day": p["day"]}, intents=[PUSH])
    plugins.handlers["src.shape"] = lambda p, i: {"shaped": i}
    return plugins


def _write_instance(root: Path, pipeline: dict[str, Any], permissions: dict[str, Any]) -> None:
    (root / "pipelines").mkdir(parents=True)
    (root / "horae.yaml").write_text(
        "timezone: Europe/Paris\nmodels:\n  default: { backend: claude-cli, model: fixture-m }\n",
        encoding="utf-8",
    )
    name = pipeline["name"]
    (root / "pipelines" / f"{name}.yaml").write_text(yaml.safe_dump(pipeline), encoding="utf-8")
    lock = {
        "schema_version": 1,
        "plugins": {
            plugin: {
                "version": "0.1.0",
                "repo": f"https://github.com/horae-dev/plugin-{plugin}",
                "commit": COMMIT,
                "permissions": {"intents": intents},
            }
            for plugin, intents in permissions.items()
        },
    }
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")


def _run(
    root: Path,
    name: str,
    run_id: str,
    plugins: FixturePlugins,
    llm: LlmBackend | None,
    previous: PreviousLlm | None = None,
) -> RunRecord:
    """Run `name` as the runner does (models and digests of the instance) and save it."""
    loaded = load_instance(root)
    record = asyncio.run(
        execute(
            loaded.pipelines[name],
            run_id=run_id,
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            previous=previous,
            models={alias: spec.model for alias, spec in loaded.config.models.items()},
            lock_digest=loaded.lock_digest(),
            config_digest=loaded.config_digest(),
        )
    )
    with Store.open(loaded.store_path) as store:
        store.save_run(record)
    return record


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """An instance whose plugins are named after their namespaces, with one recorded run."""
    root = tmp_path / "instance"
    _write_instance(root, FLOW, PERMISSIONS)
    record = _run(
        root, "flow", RUN, _flow_plugins(), FakeLlm([llm_response({"ok": True}, intents=[CREATE])])
    )
    assert record.status == "ok", record.error
    return root


def _store(instance: Path) -> Path:
    return instance / "state" / STORE_FILE


def test_replay_identical(instance: Path) -> None:
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == cli.EXIT_OK == 0, out
    assert out.strip() == f"run {RUN} (flow): identical: the replay proposes the same intents"


def test_replay_json(instance: Path) -> None:
    code, report = _json(RUN, "-i", str(instance))
    assert code == 0
    assert report["schema_version"] == 1
    assert report["outcome"] == "identical"
    assert report["identical"] is True
    assert report["definition"] == "recorded"
    assert report["intents_diff"] == ""
    assert report["summary"] == []
    assert report["pipeline_digest"]["changed"] is False
    assert report["lock_digest"]["changed"] is False
    assert report["config_digest"]["changed"] is False


def _edit(instance: Path, old: str, new: str, file: str = "pipelines/flow.yaml") -> None:
    path = instance / file
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_changed_pipeline_and_against_current(instance: Path) -> None:
    _edit(instance, "Notes/{{date}}.md", "Inbox/{{date}}.md")
    # The record keeps the definition that ran: replayed as it ran, with a note.
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 0, out
    assert "note: pipeline flow changed since the run: replayed the recorded definition" in out
    code, out = _invoke(RUN, "-i", str(instance), "--against-current")
    assert code == cli.EXIT_DIFFERENT == 4
    assert '+      "path": "Inbox/2026-10-27.md",' in out
    assert out.strip().endswith("different: the replay differs from the recorded run")
    code, report = _json(RUN, "-i", str(instance), "--against-current")
    assert code == 4
    assert report["outcome"] == "different"
    assert report["definition"] == "current"


def test_missing_pipeline_file(instance: Path) -> None:
    (instance / "pipelines" / "flow.yaml").unlink()
    assert _invoke(RUN, "-i", str(instance))[0] == 0  # the recorded definition is enough
    code, report = _json(RUN, "-i", str(instance), "--against-current")
    assert code == cli.EXIT_NOT_COMPARABLE == 3
    assert report["error"] == "--against-current: pipeline flow is not in the instance"


def test_unknown_run_and_missing_store_are_refused(instance: Path, tmp_path: Path) -> None:
    # Exit 3 like `horae diff` and `horae undo` (decision 37; was 2 before #26).
    code, out = _invoke("no-such-run", "-i", str(instance))
    assert code == cli.EXIT_UNKNOWN_RUN == 3
    assert "unknown run 'no-such-run'" in out
    missing = tmp_path / "missing.db"
    code, report = _json(RUN, "-i", str(instance), "--store", str(missing))
    assert code == 3
    assert report["outcome"] == "error"
    assert report["identical"] is False
    assert report["error"] == f"unknown run {RUN!r}: no store at {missing}"
    assert report["pipeline_digest"] == {"recorded": None, "current": None, "changed": None}
    assert report["mismatches"] == []
    assert report["summary"] == []
    assert not missing.exists()  # replay never creates a store
    assert _invoke("--no-such-option")[0] == 2


def test_the_store_is_opened_read_only(instance: Path) -> None:
    store = _store(instance)
    for suffix in ("-wal", "-shm"):
        store.with_name(store.name + suffix).unlink(missing_ok=True)
    before = sorted(p.name for p in store.parent.iterdir())
    assert _invoke(RUN, "-i", str(instance))[0] == 0
    assert sorted(p.name for p in store.parent.iterdir()) == before


def test_store_problems_exit_1(instance: Path) -> None:
    store = _store(instance)
    with sqlite3.connect(store) as conn:
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    code, report = _json(RUN, "-i", str(instance))
    assert code == cli.EXIT_PROBLEMS == 1
    assert "older than this Horae" in report["error"]


def test_an_invalid_record_is_not_comparable(instance: Path) -> None:
    with sqlite3.connect(_store(instance)) as conn:
        conn.execute("UPDATE runs SET record = ? WHERE id = ?", ('{"schema_version": 2}', RUN))
    conn.close()
    code, report = _json(RUN, "-i", str(instance))
    assert code == 3
    assert report["error"] == f"run {RUN!r}: its record is not a valid v1 run record"


def test_explicit_store(instance: Path, tmp_path: Path) -> None:
    moved = tmp_path / "copy.db"
    _store(instance).rename(moved)
    assert _invoke(RUN, "-i", str(instance), "--store", str(moved))[0] == 0


def test_instance_problems(instance: Path) -> None:
    (instance / "horae.yaml").write_text("timezone: Mars/Base\n", encoding="utf-8")
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 1
    assert "horae.yaml" in out


def test_unresolved_namespace(instance: Path) -> None:
    _edit(instance, "src.shape", "other.shape")
    assert _invoke(RUN, "-i", str(instance))[0] == 0  # the recorded definition resolves
    code, out = _invoke(RUN, "-i", str(instance), "--against-current")
    assert code == 3
    assert "other.shape: resolved by no plugin of horae.lock" in out
    code, report = _json(RUN, "-i", str(instance), "--against-current")
    assert report["pipeline"] == "flow"


def test_lock_and_model_drift(instance: Path) -> None:
    _edit(instance, "fixture-m", "other-m", "horae.yaml")
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 3
    assert "horae.yaml (models, plugins) changed since the run" in out
    # Replayed with the recorded model targets: the same cache key, the same run.
    code, out = _invoke(RUN, "-i", str(instance), "--against-current")
    assert code == 0, out
    assert "note: horae.yaml (models, plugins) changed since the run" in out
    _edit(instance, "version: 0.1.0", "version: 0.2.0", "horae.lock")
    code, report = _json(RUN, "-i", str(instance))
    assert code == 3
    assert report["error"].startswith("horae.lock changed since the run")
    assert report["lock_digest"]["changed"] is True
    code, report = _json(RUN, "-i", str(instance), "--against-current")
    assert code == 4  # the plugins are now 0.2.0
    assert {d["field"] for d in report["summary"]} == {"plugins", "input_hash"}  # cache key


def test_replay_of_a_reuse_run(instance: Path) -> None:
    store = _store(instance)
    with Store.open(store, read_only=True) as opened:
        previous = opened.last_accepted_llm("flow")
    assert previous is not None
    second = _run(
        instance, "flow", "20261027T053000Z-flow-02", _flow_plugins(), FakeLlm(), previous
    )
    assert second.llm[0].cached is True
    code, out = _invoke(second.id, "-i", str(instance))
    assert code == 0, out


BRIEF_PERMISSIONS = {
    "mcp-brief": [],
    "pack-daily-brief": ["notify.push"],
    "ntfy": ["notify.push"],
}
BRIEF_PROVIDES = {
    "mcp-brief": {"sources": ["coach.brief"]},
    "pack-daily-brief": {"skills": ["daily-brief/rank-and-word"]},
    "ntfy": {"notifiers": ["ntfy.digest"]},
}
BRIEF = {
    "name": "daily-brief",
    "steps": [
        {"collect": [{"coach.brief": {"endpoints": ["finance", "running"]}}]},
        {
            "llm": {
                "skill": "daily-brief/rank-and-word",
                "model": "default",
                "budget": {"usd": 0.05, "tokens_out": 2000},
            }
        },
        {"notify": [{"ntfy.digest": {"when": "07:30"}}]},
    ],
}


def test_plan_daily_brief_replays_with_plugins_not_named_after_their_namespace(
    tmp_path: Path,
) -> None:
    """PLAN.md §3.2: `coach.brief` from `mcp-brief`, the skill from `pack-daily-brief`."""
    root = tmp_path / "brief"
    _write_instance(root, BRIEF, BRIEF_PERMISSIONS)
    plugins = _plugins(BRIEF_PROVIDES, BRIEF_PERMISSIONS)
    plugins.handlers["coach.brief"] = lambda p, i: {e: "fine" for e in p["endpoints"]}
    llm = FakeLlm([llm_response({"items": []}, intents=[{"type": "notify.push", "body": "ok"}])])
    record = _run(root, "daily-brief", "20261027T053000Z-daily-brief-01", plugins, llm)
    assert record.status == "ok", record.error
    code, out = _invoke(record.id, "-i", str(root))
    assert code == 0, out


def test_internal_error_prints_only_the_type(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sentinel-secret-value")

    monkeypatch.setattr(cli, "replay", boom)
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == cli.EXIT_INTERNAL == 70
    assert "RuntimeError" in out
    assert "sentinel-secret-value" not in out


def test_help_documents_the_exit_codes() -> None:
    result = runner.invoke(app, ["replay", "--help"], terminal_width=200)
    text = " ".join(_plain(result.output).split())
    for code in ("0 identical", "1 configuration or store problem", "2 usage error"):
        assert code in text
    assert "3 unknown run (or no store) or not comparable" in text
    assert "4 different" in text
    assert "70 internal error" in text
