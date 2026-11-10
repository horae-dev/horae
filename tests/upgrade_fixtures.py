# SPDX-License-Identifier: Apache-2.0
"""A marketplace whose `demo` plugin gets new releases during a test (#47): local git
repositories only, synthetic hosts, no network."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.contracts.plugin import PluginManifest
from horae.marketplace import MarketplaceClient, approval
from horae.marketplace.install import INSTALL_RECORD
from marketplace_fixtures import (
    PLUGIN_URL,
    A,
    LocalClient,
    LocalInstaller,
    entry,
    plugin_manifest,
    plugin_repo,
    publish,
)

runner = CliRunner()

PERMISSIONS: dict[str, Any] = {
    "network": ["api.example.org"],
    "network_local": ["192.168.1.10:8123"],
    "secrets": ["DEMO_TOKEN"],
    "optional_secrets": ["DEMO_EXTRA"],
    "intents": ["note.create"],
    "paths": {"read": ["/srv/inbox"], "write": ["/srv/outbox"]},
}
PERSONAL = {"demo.fetch": ["children[].first_name", "children[].school"]}


def manifest(version: str, permissions: dict[str, Any] | None = None, **changes: Any) -> str:
    provides = changes.pop("provides", {"sources": ["demo.fetch"], "personal_fields": PERSONAL})
    return plugin_manifest(
        version=version,
        provides=provides,
        permissions=PERMISSIONS if permissions is None else permissions,
        **changes,
    )


@dataclass
class Market:
    """The plugin repository and the index; `release` adds a version."""

    root: Path
    index_dir: Path
    versions: dict[str, str] = field(default_factory=dict)
    tier: str = "first-party"

    def release(
        self, version: str, text: str | None = None, files: dict[str, str] | None = None
    ) -> str:
        _, sha = plugin_repo(
            self.root, version=version, manifest=text or manifest(version), files=files
        )
        self.versions[version] = sha
        self.publish()
        return sha

    def publish(self) -> None:
        publish(
            self.index_dir,
            [entry("demo", dict(self.versions), repo=PLUGIN_URL, tier=self.tier)],
        )


def fake_venv(tree: Path, plugin: PluginManifest) -> None:
    """Stands in for `PluginEnvironment`: a venv the host recognises, marked with the version."""
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "pyvenv.cfg").write_text(f"version = {plugin.version}\n", encoding="utf-8")
    (tree / ".venv" / "bin" / "python").write_text("", encoding="utf-8")


@pytest.fixture
def market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Market:
    """`demo` 1.0.0 is published (first-party, by the default index for the test)."""
    built = Market(tmp_path / "plugin-demo", tmp_path / "index")
    built.release("1.0.0")

    def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
        return LocalClient(cache_dir, urls, {A: built.index_dir})

    monkeypatch.setattr(cli_plugin, "make_client", make_client)
    monkeypatch.setattr(
        cli_plugin, "make_installer", lambda where: LocalInstaller(where, {PLUGIN_URL: built.root})
    )
    monkeypatch.setattr(cli_plugin, "interactive", lambda: True)
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({A}))
    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: fake_venv)
    return built


@pytest.fixture
def inst(instance_dir: Path) -> Path:
    """The fixture instance with the marketplace `A` and without its `github` plugin (so
    `audit` starts clean)."""
    config = instance_dir / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    text = text.replace("plugins:\n  github: { call_timeout_s: 20 }\n", "")
    config.write_text(f"{text}\nmarketplaces: [{A}]\n", encoding="utf-8")
    (instance_dir / "horae.lock").write_text("schema_version: 1\nplugins: {}\n", encoding="utf-8")
    return instance_dir


def cli(instance: Path, *args: str, answer: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", *args, "-i", str(instance)], input=answer)
    return result.exit_code, result.stdout, result.stderr


def add(instance: Path, ref: str = "demo@1.0.0", *args: str) -> None:
    code, out, _ = cli(instance, "add", ref, *args, answer="y\n")
    assert code == 0, out


def lock(instance: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((instance / "horae.lock").read_text(encoding="utf-8"))
    return data


def record(instance: Path, name: str = "demo") -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (instance / "plugins" / name / INSTALL_RECORD).read_text(encoding="utf-8")
    )
    return data


def as_json(out: str) -> Any:
    """The JSON document of stdout (CliRunner echoes a typed answer there; a terminal does not)."""
    return json.loads(out[out.index("{") :])


def hidden(instance: Path) -> list[str]:
    """What an upgrade or remove left in `plugins/` and `pipelines/` (hidden entries)."""
    return sorted(
        f"{where}/{p.name}"
        for where in ("plugins", "pipelines")
        if (instance / where).is_dir()
        for p in (instance / where).iterdir()
        if p.name.startswith(".")
    )


Snapshot = Callable[[], dict[str, bytes]]


def snapshot(instance: Path) -> dict[str, bytes]:
    """Every file of `horae.lock`, `plugins/` and `pipelines/`, by path."""
    files = {"horae.lock": (instance / "horae.lock").read_bytes()}
    for where in ("plugins", "pipelines"):
        base = instance / where
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files[str(path.relative_to(instance))] = path.read_bytes()
    return files
