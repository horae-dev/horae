# SPDX-License-Identifier: Apache-2.0
"""The built-in fixture plugins (#25), their tools called in-process.

`tests/test_e2e.py` runs them as real subprocesses; here every branch of their tools is
exercised directly (same functions the MCP server registers), and file-sink's note cap is
checked against its real subprocess."""

import asyncio
import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

from horae.apply import SinkError
from horae.config.documents import read_yaml, validate
from horae.contracts import Lockfile, PluginManifest
from horae.plugins.builtin import (
    BUILTIN,
    VERSION,
    InstallError,
    install,
    manifest_data,
    plugin_dir,
)
from horae.plugins.builtin.file_sink import server as file_sink
from horae.plugins.builtin.fixture_source import server as fixture_source
from horae.plugins.host import HostRegistry, McpSinks, PluginHost
from horae.plugins.host.sink import MAX_MCP_NOTE_BYTES
from horae.secrets import SecretResolver

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


def _ctx(paths: Any = None) -> Any:
    """A tool context whose request carries `_meta["dev.horae/paths"]` (when given)."""
    meta = None if paths is None else {"dev.horae/paths": paths}
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def test_fixture_source_serves_the_packaged_fixture() -> None:
    result = fixture_source.serve(_ctx())
    assert result.structured_content is not None
    assert [item["id"] for item in result.structured_content["items"]] == ["fx-1", "fx-2"]
    assert result.meta is not None
    (intent,) = result.meta["dev.horae/intents"]
    assert intent["type"] == "note.replace_owned_block"
    assert "path" not in intent  # filled by the core from the act entry
    assert fixture_source.serve(_ctx()).structured_content == result.structured_content


def test_fixture_source_serves_a_given_file(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"data": {"n": 1}}), encoding="utf-8")
    granted = {"read": [str(tmp_path)], "write": []}
    data, intents = fixture_source.load(str(path), granted)
    assert (data, intents) == ({"n": 1}, [])
    served = fixture_source.serve(_ctx(granted), str(path))
    assert served.structured_content == {"n": 1}
    path.write_text("{}", encoding="utf-8")
    assert fixture_source.load(str(path), granted) == ({}, [])


@pytest.mark.parametrize(
    "paths",
    [None, {}, {"read": []}, {"read": "/"}, {"read": ["relative"]}, {"write": ["/"]}],
)
def test_fixture_source_serves_only_files_the_lock_grants(tmp_path: Path, paths: Any) -> None:
    path = tmp_path / "f.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ToolError, match=r"paths\.read"):
        fixture_source.load(str(path), paths)
    with pytest.raises(ToolError, match=r"paths\.read"):
        fixture_source.serve(_ctx(paths), str(path))


def test_fixture_source_resolves_links_and_reads_regular_files_only(tmp_path: Path) -> None:
    granted_dir = tmp_path / "granted"
    granted_dir.mkdir()
    granted = {"read": [str(granted_dir)]}
    secret = tmp_path / "secret.json"
    secret.write_text('{"data": {"secret": 1}}', encoding="utf-8")
    (granted_dir / "link.json").symlink_to(secret)
    with pytest.raises(ToolError, match=r"paths\.read"):  # its real path is outside
        fixture_source.load(str(granted_dir / "link.json"), granted)
    with pytest.raises(ToolError, match=r"paths\.read"):
        fixture_source.load(str(granted_dir / ".." / "secret.json"), granted)
    fifo = granted_dir / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ToolError, match="not a regular file"):  # and it never blocks
        fixture_source.load(str(fifo), granted)
    with pytest.raises(ToolError, match="not a regular file"):
        fixture_source.load(str(granted_dir), granted)


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
        fixture_source.load(str(path), {"read": [str(tmp_path)]})


def test_fixture_source_needs_an_absolute_path() -> None:
    with pytest.raises(ToolError, match="absolute"):
        fixture_source.load("relative.json", {"read": ["/"]})


# --- file-sink -------------------------------------------------------------------------------


def _paths(vault: Path) -> dict[str, Any]:
    return {"read": [], "write": [str(vault)]}


def _data(result: Any) -> Any:
    """A file-sink tool's result: its `structuredContent` (sent once, with a short text)."""
    assert isinstance(result, CallToolResult)
    (text,) = result.content
    assert isinstance(text, TextContent)
    assert len(text.text) < 64  # never a second copy of the payload
    return result.structured_content


def _run(coro: Any) -> Any:
    return _data(asyncio.run(coro))


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
    identity = _data(file_sink.identity(paths))["identity"]
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
    assert _data(file_sink.identity(paths))["error"]["kind"] == kind
    assert _run(file_sink.read(paths, NOTE))["error"]["kind"] == kind


def test_file_sink_applies_note_intents_only_with_the_core_bytes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    push = {"type": "notify.push", "body": "hi"}
    assert _run(file_sink.apply(paths, push))["error"]["kind"] == "not_supported"
    assert _run(file_sink.apply(paths, _note()))["error"]["kind"] == "content_missing"
    bad = _run(file_sink.apply(paths, _note(), content="!!"))
    assert bad["error"]["kind"] == "output_invalid"
    assert list(tmp_path.iterdir()) == []


# --- install ---------------------------------------------------------------------------------


def test_install_writes_0644_manifests_atomically(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        installed = install(tmp_path / "plugins")
    finally:
        os.umask(old)
    for root in installed.values():
        assert stat.S_IMODE(os.stat(root / "plugin.yaml").st_mode) == 0o644
        assert [p.name for p in root.iterdir()] == ["plugin.yaml"]  # no temporary file left
    # A symbolic link where a manifest goes is replaced, never followed.
    outside = tmp_path / "outside.yaml"
    outside.write_text("mine\n", encoding="utf-8")
    manifest = installed["file-sink"] / "plugin.yaml"
    manifest.unlink()
    manifest.symlink_to(outside)
    install(tmp_path / "plugins")
    assert not manifest.is_symlink()
    assert outside.read_text(encoding="utf-8") == "mine\n"


def test_install_refuses_symlinked_directories(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    with pytest.raises(InstallError, match="symbolic link"):
        install(tmp_path / "linked")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "file-sink").symlink_to(real)
    with pytest.raises(InstallError, match="symbolic link"):
        install(plugins)
    assert list(real.iterdir()) == []


def test_install_needs_an_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "")
    with pytest.raises(InstallError, match=r"sys\.executable is empty"):
        install(tmp_path)
    with pytest.raises(InstallError, match=r"sys\.executable is empty"):
        manifest_data("file-sink")
    assert install(tmp_path, python="/opt/python/bin/python3")


# --- file-sink as a real subprocess: the MCP note cap ----------------------------------------


def _file_sink(tmp_path: Path, vault: Path) -> tuple[HostRegistry, PluginHost]:
    plugins = tmp_path / "plugins"
    install(plugins)
    permissions = manifest_data("file-sink")["permissions"] | {"paths": {"write": [str(vault)]}}
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                "file-sink": {
                    "version": VERSION,
                    "repo": "https://example.org/horae.git",
                    "commit": "0" * 40,
                    "permissions": permissions,
                }
            },
        }
    )
    manifest = validate(read_yaml(plugins / "file-sink" / "plugin.yaml"), PluginManifest)
    registry = HostRegistry(lock, {"file-sink": manifest}, {"file-sink": plugins / "file-sink"})
    secrets = SecretResolver({}, tmp_path / "secrets")
    return registry, PluginHost(registry, secrets=secrets, work_dir=tmp_path / "work")


def test_file_sink_notes_up_to_the_mcp_cap(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Daily").mkdir(parents=True)
    under, over = b"u" * MAX_MCP_NOTE_BYTES, b"o" * (MAX_MCP_NOTE_BYTES + 1)
    (vault / "Daily" / "under.md").write_bytes(under)
    (vault / "Daily" / "over.md").write_bytes(over)
    registry, plugin_host = _file_sink(tmp_path, vault)

    async def go() -> None:
        async with plugin_host as live:
            sink = McpSinks(registry, live).mcp_sink_for("file-sink.write_note")
            assert sink is not None
            assert await sink.read_target("Daily/under.md") == under
            with pytest.raises(SinkError) as caught:
                await sink.read_target("Daily/over.md")
            assert caught.value.kind == "note_too_large"  # an error result, not a kill
            assert await sink.read_target("Daily/under.md") == under
            with pytest.raises(SinkError) as caught:
                await sink.restore("Daily/under.md", over, expected_sha256=_sha(under))
            assert caught.value.kind == "note_too_large"  # refused before the call
            await sink.restore("Daily/under.md", b"small\n", expected_sha256=_sha(under))
            # One process for the whole block: the oversized read did not cost it.
            (run_dir,) = (tmp_path / "work").iterdir()
            assert sorted(p.name for p in run_dir.iterdir()) == ["file-sink.1"]

    asyncio.run(go())
    assert (vault / "Daily" / "under.md").read_bytes() == b"small\n"
    assert (vault / "Daily" / "over.md").read_bytes() == over
