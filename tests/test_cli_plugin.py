# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search|resolve` (#43), over local fixture repositories (no network)."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.marketplace import MarketplaceClient
from marketplace_fixtures import A, B, LocalClient, entry, publish, sha

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


def _run(instance: Path, *args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", *args, "-i", str(instance)])
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
