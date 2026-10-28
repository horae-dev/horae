# SPDX-License-Identifier: Apache-2.0
"""The built-in fixture plugins (#25), their tools called in-process.

`tests/test_e2e.py` runs them as real subprocesses; here every branch of their tools is
exercised directly (same functions the MCP server registers)."""

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError

from horae.contracts import PluginManifest
from horae.plugins.builtin import BUILTIN, VERSION, install, manifest_data, plugin_dir
from horae.plugins.builtin.file_sink import server as file_sink
from horae.plugins.builtin.fixture_source import server as fixture_source

NOTE = "Daily/x.assistant.md"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- packaging -------------------------------------------------------------------------------


def test_manifests_and_install(tmp_path: Path) -> None:
    installed = install(tmp_path, python="/opt/python/bin/python3")
    assert sorted(installed) == sorted(BUILTIN)
    for name, module in BUILTIN.items():
        packaged = yaml.safe_load((plugin_dir(name) / "plugin.yaml").read_text(encoding="utf-8"))
        assert packaged["runtime"]["command"] == ["python3", "-m", module]
        assert (plugin_dir(name) / "README.md").is_file()
        data = yaml.safe_load((installed[name] / "plugin.yaml").read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(data)
        assert (manifest.name, manifest.version) == (name, VERSION)
        assert manifest.runtime is not None
        assert data["runtime"]["command"] == ["/opt/python/bin/python3", "-m", module]
        assert manifest.permissions.paths.write == []  # the vault is the user's, in the lock
    assert manifest_data("file-sink")["runtime"]["command"][0] == sys.executable


# --- fixture-source --------------------------------------------------------------------------


def test_fixture_source_serves_the_packaged_fixture() -> None:
    result = fixture_source.serve()
    assert result.structured_content is not None
    assert [item["id"] for item in result.structured_content["items"]] == ["fx-1", "fx-2"]
    assert result.meta is not None
    (intent,) = result.meta["dev.horae/intents"]
    assert intent["type"] == "note.replace_owned_block"
    assert "path" not in intent  # filled by the core from the act entry
    assert fixture_source.serve().structured_content == result.structured_content


def test_fixture_source_serves_a_given_file(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"data": {"n": 1}}), encoding="utf-8")
    data, intents = fixture_source.load(str(path))
    assert (data, intents) == ({"n": 1}, [])
    path.write_text("{}", encoding="utf-8")
    assert fixture_source.load(str(path)) == ({}, [])


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot be read"),
        (b"x" * (fixture_source.MAX_FIXTURE_BYTES + 1), "larger than"),
        (b"{nope", "not JSON"),
        (b"[]", "only `data` and `intents`"),
        (b'{"other": 1}', "only `data` and `intents`"),
        (b'{"data": []}', "must be an object"),
        (b'{"intents": {}}', "must be an object"),
    ],
)
def test_fixture_source_refuses_bad_files(
    tmp_path: Path, content: bytes | None, message: str
) -> None:
    path = tmp_path / "f.json"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ToolError, match=message):
        fixture_source.load(str(path))


def test_fixture_source_needs_an_absolute_path() -> None:
    with pytest.raises(ToolError, match="absolute"):
        fixture_source.load("relative.json")


# --- file-sink -------------------------------------------------------------------------------


def _paths(vault: Path) -> dict[str, Any]:
    return {"read": [], "write": [str(vault)]}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _note(content: str = "x") -> dict[str, Any]:
    return {"type": "note.create", "path": NOTE, "content": content}


def test_file_sink_writes_reads_and_restores(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = _paths(vault)
    assert _run(file_sink.read(paths, NOTE)) == {"content": None}
    assert _run(file_sink.parent_exists(paths, NOTE)) == {"exists": False}
    result = _run(file_sink.apply(paths, _note(), content=_b64(b"x\n")))
    assert result == {"target": NOTE, "after_sha256": _sha(b"x\n")}
    assert (vault / NOTE).read_bytes() == b"x\n"
    assert _run(file_sink.read(paths, NOTE)) == {"content": _b64(b"x\n")}
    assert _run(file_sink.parent_exists(paths, NOTE)) == {"exists": True}
    stale = _run(file_sink.apply(paths, _note(), content=_b64(b"y\n")))
    assert stale["error"]["kind"] == "target_changed"  # it exists: the swap is refused
    assert (vault / NOTE).read_bytes() == b"x\n"
    assert _run(file_sink.restore(paths, NOTE, None, _sha(b"x\n"))) == {}
    assert not (vault / NOTE).exists()
    identity = file_sink.identity(paths)["identity"]
    info = os.stat(vault)
    assert identity == f"local:{info.st_dev}:{info.st_ino}:{os.path.realpath(vault)}"


def test_file_sink_keeps_the_built_in_sinks_safety_rules(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Daily").mkdir(parents=True)
    outside = tmp_path / "outside.assistant.md"
    outside.write_bytes(b"secret\n")
    (vault / NOTE).symlink_to(outside)
    paths = _paths(vault)
    assert _run(file_sink.read(paths, NOTE))["error"]["kind"] == "symlink"
    written = _run(file_sink.apply(paths, _note(), _b64(b"x\n"), _sha(b"secret\n")))
    assert written["error"]["kind"] == "symlink"
    assert outside.read_bytes() == b"secret\n"
    for bad in ("../x.md", "/etc/x.md", ".obsidian/x.md", "a//b.md", "a\\b.md"):
        assert _run(file_sink.read(paths, bad))["error"]["kind"] == "path_invalid"
        assert _run(file_sink.parent_exists(paths, bad))["error"]["kind"] == "path_invalid"
        restored = _run(file_sink.restore(paths, bad, None, None))
        assert restored["error"]["kind"] == "path_invalid"


@pytest.mark.parametrize(
    ("paths", "kind"),
    [
        ({}, "vault_unset"),
        ({"write": []}, "vault_unset"),
        ({"write": ["/a", "/b"]}, "config_invalid"),
        ({"write": ["relative"]}, "config_invalid"),
        ({"write": [3]}, "config_invalid"),
        ({"write": "/a"}, "config_invalid"),
    ],
)
def test_file_sink_needs_one_approved_vault(paths: dict[str, Any], kind: str) -> None:
    assert file_sink.identity(paths)["error"]["kind"] == kind
    assert _run(file_sink.read(paths, NOTE))["error"]["kind"] == kind


def test_file_sink_applies_note_intents_only_with_the_core_bytes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    push = {"type": "notify.push", "body": "hi"}
    assert _run(file_sink.apply(paths, push))["error"]["kind"] == "not_supported"
    assert _run(file_sink.apply(paths, _note()))["error"]["kind"] == "content_missing"
    bad = _run(file_sink.apply(paths, _note(), content="!!"))
    assert bad["error"]["kind"] == "output_invalid"
    assert list(tmp_path.iterdir()) == []
