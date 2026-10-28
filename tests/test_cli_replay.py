# SPDX-License-Identifier: Apache-2.0
"""`horae replay <run-id>`: reads the instance and the store, exit codes like `horae doctor`."""

import asyncio
import datetime
import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli
from horae.cli import app
from horae.contracts import Pipeline
from horae.engine import ToolResult, execute, make_context
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


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["replay", *args])
    return result.exit_code, _plain(result.output)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("replay touched the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _plugins() -> FixturePlugins:
    plugins = FixturePlugins()
    provides = {
        "src": {"sources": ["src.read"], "transforms": ["src.shape"]},
        "sink": {"sinks": ["sink.write"], "notifiers": ["sink.notify"]},
        "skills": {"skills": ["skills/rank"]},
    }
    for name, intents in PERMISSIONS.items():
        manifest = {
            "name": name,
            "version": "0.1.0",
            "horae": ">=0.1",
            "provides": provides[name],
            "permissions": {"intents": intents},
        }
        plugins.add(manifest, commit=COMMIT)
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data={"day": p["day"]}, intents=[PUSH])
    plugins.handlers["src.shape"] = lambda p, i: {"shaped": i}
    return plugins


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """An instance whose plugins are named after their namespaces, with one recorded run."""
    root = tmp_path / "instance"
    (root / "pipelines").mkdir(parents=True)
    (root / "horae.yaml").write_text(
        "timezone: Europe/Paris\nmodels:\n  default: { backend: claude-cli, model: fixture-m }\n",
        encoding="utf-8",
    )
    (root / "pipelines" / "flow.yaml").write_text(yaml.safe_dump(FLOW), encoding="utf-8")
    lock = {
        "schema_version": 1,
        "plugins": {
            name: {
                "version": "0.1.0",
                "repo": f"https://github.com/horae-dev/plugin-{name}",
                "commit": COMMIT,
                "permissions": {"intents": intents},
            }
            for name, intents in PERMISSIONS.items()
        },
    }
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    plugins = _plugins()
    record = asyncio.run(
        execute(
            Pipeline.model_validate(FLOW),
            run_id=RUN,
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=FakeLlm([llm_response({"ok": True}, intents=[CREATE])]),
            models={"default": "fixture-m"},
        )
    )
    assert record.status == "ok", record.error
    with Store.open(root / "state" / cli.STORE_FILE) as store:
        store.save_run(record)
    return root


def test_replay_identical(instance: Path) -> None:
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == cli.EXIT_OK == 0, out
    assert out.strip() == f"run {RUN} (flow): identical: the replay proposes the same intents"


def test_replay_json(instance: Path) -> None:
    code, out = _invoke(RUN, "-i", str(instance), "--json")
    assert code == 0
    report = json.loads(out)
    assert report["schema_version"] == 1
    assert report["outcome"] == "identical"
    assert report["identical"] is True
    assert report["intents_diff"] == ""
    assert report["summary"] == []
    assert report["pipeline_digest"]["changed"] is False


def _edit(instance: Path) -> None:
    flow = instance / "pipelines" / "flow.yaml"
    flow.write_text(
        flow.read_text(encoding="utf-8").replace("Notes/{{date}}.md", "Inbox/{{date}}.md"),
        encoding="utf-8",
    )


def test_changed_pipeline_and_against_current(instance: Path) -> None:
    _edit(instance)
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == cli.EXIT_PROBLEMS == 1
    assert "changed since the run" in out
    code, out = _invoke(RUN, "-i", str(instance), "--against-current")
    assert code == 1
    assert '+      "path": "Inbox/2026-10-27.md",' in out
    assert out.strip().endswith("different: the replay differs from the recorded run")
    code, out = _invoke(RUN, "-i", str(instance), "--against-current", "--json")
    assert json.loads(out)["outcome"] == "different"


def test_unknown_run_and_missing_store_are_usage_errors(instance: Path, tmp_path: Path) -> None:
    code, out = _invoke("no-such-run", "-i", str(instance))
    assert code == cli.EXIT_USAGE == 2
    assert "unknown run 'no-such-run'" in out
    missing = tmp_path / "missing.db"
    code, out = _invoke(RUN, "-i", str(instance), "--store", str(missing), "--json")
    assert code == 2
    assert json.loads(out) == {
        "schema_version": 1,
        "run_id": RUN,
        "outcome": "error",
        "identical": False,
        "error": f"no store at {missing}",
    }
    assert not missing.exists()  # replay never creates a store
    assert _invoke("--no-such-option")[0] == 2


def test_explicit_store(instance: Path, tmp_path: Path) -> None:
    moved = tmp_path / "copy.db"
    (instance / "state" / cli.STORE_FILE).rename(moved)
    assert _invoke(RUN, "-i", str(instance), "--store", str(moved))[0] == 0


def test_instance_problems(instance: Path) -> None:
    (instance / "horae.yaml").write_text("timezone: Mars/Base\n", encoding="utf-8")
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 1
    assert "horae.yaml" in out


def test_missing_pipeline(instance: Path) -> None:
    (instance / "pipelines" / "flow.yaml").unlink()
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 1
    assert "pipeline 'flow' is not in the instance" in out


def test_unresolved_namespace(instance: Path) -> None:
    flow = instance / "pipelines" / "flow.yaml"
    flow.write_text(
        flow.read_text(encoding="utf-8").replace("src.shape", "other.shape"), encoding="utf-8"
    )
    code, out = _invoke(RUN, "-i", str(instance))
    assert code == 1
    assert "other.shape" in out
    assert "#24" in out


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
