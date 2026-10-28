# SPDX-License-Identifier: Apache-2.0
"""Helpers for the plugin host tests: the probe and raw fixture plugins, a lock, a host."""

import sys
from pathlib import Path
from typing import Any

import yaml

from horae.contracts import Lockfile, PluginManifest
from horae.plugins.host import HostRegistry, PluginHost
from horae.secrets import SecretResolver

PLUGINS = Path(__file__).resolve().parent / "fixtures" / "plugins"
PROBE = PLUGINS / "probe"
RAW_SERVER = PLUGINS / "raw" / "raw_server.py"

PROBE_TOKEN = "sentinel-value-for-probe"  # noqa: S105 (synthetic)
OTHER_SECRET = "sentinel-value-for-other"  # noqa: S105 (synthetic, not declared by probe)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def probe_manifest(**changes: Any) -> dict[str, Any]:
    """`tests/fixtures/plugins/probe/plugin.yaml`, run with this interpreter."""
    data: dict[str, Any] = yaml.safe_load((PROBE / "plugin.yaml").read_text(encoding="utf-8"))
    data["runtime"]["command"] = [sys.executable, str(PROBE / "server.py")]
    data.update(changes)
    return data


RAW_TOOLS = [
    "raw.ok",
    "raw.junk",
    "raw.big",
    "raw.rpc_error",
    "raw.ask",
    "raw.init",
    "raw.progress",
    "raw.log",
    "raw.bad_result",
    "raw.bad_intents",
    "raw.two_blocks",
    "raw.not_json",
    "raw.null",
    "raw.close",
    "raw.unlisted",
]
"""The tools of `tests/fixtures/plugins/raw/raw_server.py`, plus one it does not list."""


def raw_manifest(mode: str = "normal", **permissions: Any) -> dict[str, Any]:
    return {
        "name": "raw",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": RAW_TOOLS, "transforms": ["raw.ok"]},
        "permissions": permissions,
        "runtime": {"protocol": "mcp-stdio", "command": [sys.executable, str(RAW_SERVER), mode]},
    }


def lock_for(*manifests: dict[str, Any]) -> Lockfile:
    plugins = {
        data["name"]: {
            "version": data["version"],
            "repo": f"https://example.org/plugin-{data['name']}",
            "commit": COMMIT,
            "permissions": data["permissions"],
        }
        for data in manifests
    }
    return Lockfile.model_validate({"schema_version": 1, "plugins": plugins})


def registry(*manifests: dict[str, Any]) -> HostRegistry:
    models = {data["name"]: PluginManifest.model_validate(data) for data in manifests}
    return HostRegistry(lock_for(*manifests), models)


def secrets_dir(root: Path, **values: str) -> Path:
    directory = root / "secrets"
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (directory / name).write_text(value + "\n", encoding="utf-8")
    return directory


def host(
    root: Path,
    *manifests: dict[str, Any],
    resolver: SecretResolver | None = None,
    **kwargs: Any,
) -> PluginHost:
    """A host over `manifests` (default: the probe), with `PROBE_TOKEN` resolvable."""
    if resolver is None:
        resolver = SecretResolver({}, secrets_dir(root, PROBE_TOKEN=PROBE_TOKEN))
    return PluginHost(
        registry(*(manifests or (probe_manifest(),))),
        secrets=resolver,
        work_dir=root / "work",
        **kwargs,
    )
