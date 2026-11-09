# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search|resolve` (#43) and `horae plugin add` (#44, with its venv: #45), over
local fixture repositories (no network)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.contracts import PluginManifest
from horae.marketplace import MarketplaceClient, PluginEnvironment, PluginInstaller, approval
from marketplace_fixtures import (
    PLUGIN_URL,
    A,
    B,
    LocalClient,
    LocalInstaller,
    entry,
    plugin_repo,
    publish,
    sha,
)

runner = CliRunner()


@pytest.fixture
def indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repos = {
        A: publish(
            tmp_path / "a",
            [
                entry("github", {"0.1.0": sha(1), "0.2.0": sha(2)}, tier="first-party"),
                entry("tool", ["1.0.0"], repo="https://github.com/a/tool"),
            ],
        ),
        B: publish(
            tmp_path / "b",
            [entry("tool", ["2.0.0"], repo="https://github.com/b/tool"), entry("ntfy", ["0.1.0"])],
        ),
    }

    def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
        return LocalClient(cache_dir, urls, repos)

    monkeypatch.setattr(cli_plugin, "make_client", make_client)
    return repos


def _configure(instance: Path) -> Path:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(f"{text}\nmarketplaces: [{A}, {B}]\n", encoding="utf-8")
    return instance


def _run(instance: Path, *args: str, answer: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", *args, "-i", str(instance)], input=answer)
    return result.exit_code, result.stdout, result.stderr


def test_resolve_prints_the_pinned_commit(instance_dir: Path, indexes: dict[str, Path]) -> None:
    _configure(instance_dir)
    code, out, _ = _run(instance_dir, "resolve", "github")
    assert code == 0
    assert "github@0.2.0" in out
    assert sha(2) in out
    assert "first-party" in out
    assert A in out
    code, out, _ = _run(instance_dir, "resolve", "github@0.1.0", "--json")
    assert code == 0
    assert json.loads(out) == {
        "schema_version": 1,
        "name": "github",
        "version": "0.1.0",
        "repo": "https://github.com/example/plugin-github",
        "sha": sha(1),
        "tier": "first-party",
        "index": A,
        "warnings": [],
    }


def test_a_conflict_is_exit_3_and_pins_resolve_it(
    instance_dir: Path, indexes: dict[str, Path]
) -> None:
    _configure(instance_dir)
    code, _, err = _run(instance_dir, "resolve", "tool")
    assert code == 3
    assert A in err
    assert B in err
    assert "marketplace_pins" in err
    code, out, _ = _run(instance_dir, "resolve", "tool", "--json")
    assert code == 3
    assert json.loads(out)["error"]["kind"] == "conflict"
    config = instance_dir / "horae.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"marketplace_pins: {{tool: {B}}}\n", encoding="utf-8"
    )
    code, out, _ = _run(instance_dir, "resolve", "tool")
    assert code == 0
    assert "tool@2.0.0" in out
    assert B in out


def test_unknown_names_versions_and_references(
    instance_dir: Path, indexes: dict[str, Path]
) -> None:
    _configure(instance_dir)
    assert _run(instance_dir, "resolve", "nope")[0] == 3
    assert _run(instance_dir, "resolve", "github@9.9.9")[0] == 3
    assert _run(instance_dir, "resolve", "Bad Ref")[0] == 2


def test_search_lists_every_index_in_priority_order(
    instance_dir: Path, indexes: dict[str, Path]
) -> None:
    _configure(instance_dir)
    code, out, _ = _run(instance_dir, "search")
    assert code == 0
    assert [line.split()[0] for line in out.splitlines()] == ["github", "tool", "ntfy", "tool"]
    code, out, _ = _run(instance_dir, "search", "ntf", "--json")
    assert [p["name"] for p in json.loads(out)["plugins"]] == ["ntfy"]
    assert "no plugin found" in _run(instance_dir, "search", "zzz")[1]


def test_offline_uses_the_cache_and_a_lost_network_warns(
    instance_dir: Path, indexes: dict[str, Path]
) -> None:
    _configure(instance_dir)
    assert _run(instance_dir, "search")[0] == 0  # fills the cache
    code, out, err = _run(instance_dir, "resolve", "github", "--offline")
    assert code == 0
    assert sha(2) in out
    assert "warning" not in err
    shutil.rmtree(indexes[A])  # the network is gone
    code, out, err = _run(instance_dir, "resolve", "github")
    assert code == 0
    assert sha(2) in out
    assert "warning: cannot fetch marketplace" in err
    assert A in err
    code, out, _ = _run(instance_dir, "resolve", "github", "--json")
    assert json.loads(out)["warnings"]


def test_no_cache_is_a_clear_error(instance_dir: Path, indexes: dict[str, Path]) -> None:
    _configure(instance_dir)
    code, _, err = _run(instance_dir, "resolve", "github", "--offline")
    assert code == 1
    assert "no cached copy" in err
    shutil.rmtree(indexes[A])
    code, out, _ = _run(instance_dir, "search", "--json")
    assert code == 1
    assert json.loads(out)["error"]["kind"] == "no_cache"


def test_the_commands_use_the_https_client_by_default(tmp_path: Path) -> None:
    client = cli_plugin.make_client(tmp_path, [A])
    assert type(client) is MarketplaceClient
    assert client.allow_protocol == "https"


def test_http_marketplaces_in_the_config_are_refused(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + "\nmarketplaces: ['file:///tmp/x']\n",
        encoding="utf-8",
    )
    code, _, err = _run(instance_dir, "search")
    assert code == 1
    assert "marketplaces" in err


# --- plugin add (#44) ----------------------------------------------------------------------------


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An index listing `demo` (the good release and one whose tag was moved) and its repo."""
    repo, good = plugin_repo(tmp_path / "plugin-demo")
    _, moved = plugin_repo(repo, version="2.0.0")
    plugin_repo(repo, version="2.0.0", files={"server.py": "print('evil')\n"})
    index = publish(
        tmp_path / "index",
        [entry("demo", {"1.0.0": good, "2.0.0": moved}, repo=PLUGIN_URL, tier="first-party")],
    )

    def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
        return LocalClient(cache_dir, urls, {A: index})

    monkeypatch.setattr(cli_plugin, "make_client", make_client)
    monkeypatch.setattr(
        cli_plugin, "make_installer", lambda where: LocalInstaller(where, {PLUGIN_URL: repo})
    )
    monkeypatch.setattr(cli_plugin, "interactive", lambda: True)  # approval: tests answer
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({A}))
    prepared: dict[str, str] = {}

    def make_environment(cache_dir: Path) -> Any:
        # The demo plugin has no dependencies to install: record what `add` asked for.
        def prepare(tree: Path, manifest: PluginManifest) -> None:
            prepared.update(cache=str(cache_dir), tree=tree.name, plugin=manifest.name)

        return prepare

    monkeypatch.setattr(cli_plugin, "make_environment", make_environment)
    return {"good": good, "moved": moved, "prepared": prepared}


def _configure_a(instance: Path) -> None:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(f"{text}\nmarketplaces: [{A}]\n", encoding="utf-8")


def test_add_installs_into_the_plugin_dir_and_says_what_is_left(
    instance_dir: Path, demo: dict[str, Any]
) -> None:
    _configure_a(instance_dir)
    code, out, _ = _run(instance_dir, "add", "demo@1.0.0", answer="y\n")
    assert code == 0, out
    assert "installed demo@1.0.0 into plugins/demo" in out
    assert demo["good"] in out
    assert "approved: horae.lock pins it" in out
    assert "dependencies are installed in its own venv" in out
    assert (instance_dir / "plugins" / "demo" / "plugin.yaml").is_file()
    code, out, _ = _run(instance_dir, "add", "demo@1.0.0", "--json", answer="y\n")
    assert code == 3
    assert json.loads(out)["error"]["kind"] == "already_installed"


def test_add_json_reports_the_install_record(instance_dir: Path, demo: dict[str, Any]) -> None:
    _configure_a(instance_dir)
    code, out, _ = _run(instance_dir, "add", "demo@1.0.0", "--json", answer="y\n")
    assert code == 0
    report = json.loads(out[out.index("{") :])  # CliRunner echoes the typed answer
    assert report["commit"] == demo["good"]
    assert report["path"] == "plugins/demo"
    assert report["approved"] is True
    assert report["enabled"] is False
    assert report["pending"] == []
    # Its venv was built in the verified tree, with uv's cache in the state directory (#45).
    assert demo["prepared"] == {
        "cache": str(instance_dir / "state" / "uv-cache"),
        "tree": "tree",
        "plugin": "demo",
    }


def test_add_refuses_a_plugin_without_a_lock_file_and_leaves_nothing(
    instance_dir: Path, demo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_plugin, "make_environment", PluginEnvironment)
    _configure_a(instance_dir)
    # Approved first (decision 70), then the venv step refuses the tree (decision 69).
    code, out, err = _run(instance_dir, "add", "demo@1.0.0", "--json", answer="y\n")
    assert code == 3, (out, err)
    error = json.loads(out[out.index("{") :])["error"]  # the runner echoes the typed "y"
    assert error["kind"] == "install_refused"
    assert "no pyproject.toml" in error["message"]
    assert not (instance_dir / "horae.lock").read_text(encoding="utf-8").count("demo:")
    assert not list((instance_dir / "plugins").iterdir())


def test_add_builds_a_venv_by_default(tmp_path: Path) -> None:
    environment = cli_plugin.make_environment(tmp_path / "uv-cache")
    assert isinstance(environment, PluginEnvironment)
    assert environment.cache_dir == tmp_path / "uv-cache"
    assert environment.index_url is None


def test_add_refuses_a_moved_tag_with_exit_3(instance_dir: Path, demo: dict[str, Any]) -> None:
    _configure_a(instance_dir)
    code, out, _ = _run(instance_dir, "add", "demo", "--json")  # 2.0.0, the moved tag
    assert code == 3
    error = json.loads(out)["error"]
    assert error["kind"] == "install_refused"
    assert "tag v2.0.0" in error["message"]
    assert not (instance_dir / "plugins").exists() or not list((instance_dir / "plugins").iterdir())


def test_add_uses_the_https_installer_by_default(tmp_path: Path) -> None:
    installer = cli_plugin.make_installer(tmp_path)
    assert type(installer) is PluginInstaller
    assert installer.allow_protocol == "https"
