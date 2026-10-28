# SPDX-License-Identifier: Apache-2.0
"""First-party fixture plugins shipped with the core (PLAN.md M1, #25): examples and tests.

- `fixture-source` (`horae.plugins.builtin.fixture_source`): serves deterministic synthetic
  data from a JSON fixture file, and proposes the intents the file lists;
- `file-sink` (`horae.plugins.builtin.file_sink`): applies `note.*` intents to a local vault,
  the core computing every note's bytes (`horae.plugins.host.sink`).

Each is a stdio MCP server built with the `mcp` SDK, with its `plugin.yaml` and `README.md`
next to it. The packaged manifest runs `python3 -m <module>`; the plugin host looks a bare
command up on the child's fixed `PATH` (`/usr/local/bin:/usr/bin:/bin`), where `python3` does
not have Horae installed. Until M4 installs plugins into their own environments (#44, #46),
`install(directory)` writes each manifest to `<directory>/<name>/plugin.yaml` with the
command pinned to an interpreter that has Horae (default: the running one), the layout
`HostRegistry.from_directory` reads.
"""

import sys
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
BUILTIN: dict[str, str] = {
    "fixture-source": "horae.plugins.builtin.fixture_source",
    "file-sink": "horae.plugins.builtin.file_sink",
}
"""Plugin name to module (`python -m <module>` runs its server)."""
VERSION = "0.1.0"
"""The version of both plugins (pinned in `horae.lock`)."""


def plugin_dir(name: str) -> Path:
    """The packaged directory of plugin `name` (its `plugin.yaml`, `README.md`, server)."""
    return PACKAGE_DIR / BUILTIN[name].rsplit(".", 1)[1]


def manifest_data(name: str, *, python: str | None = None) -> dict[str, Any]:
    """Plugin `name`'s packaged manifest, its command run by `python` (default: this
    interpreter, `sys.executable`)."""
    data: dict[str, Any] = yaml.safe_load(
        (plugin_dir(name) / "plugin.yaml").read_text(encoding="utf-8")
    )
    data["runtime"]["command"] = [python or sys.executable, "-m", BUILTIN[name]]
    return data


def install(directory: Path, *, python: str | None = None) -> dict[str, Path]:
    """Write `<directory>/<name>/plugin.yaml` for every built-in plugin (see the module
    docstring); returns each plugin's directory."""
    installed: dict[str, Path] = {}
    for name in sorted(BUILTIN):
        root = directory / name
        root.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(manifest_data(name, python=python), sort_keys=False)
        (root / "plugin.yaml").write_text(text, encoding="utf-8")
        installed[name] = root
    return installed


__all__ = ["BUILTIN", "PACKAGE_DIR", "VERSION", "install", "manifest_data", "plugin_dir"]
