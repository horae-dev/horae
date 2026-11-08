# SPDX-License-Identifier: Apache-2.0
"""Helpers for the SDK tests: the examples, the core's schemas, a lock and a host."""

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from horae.contracts import Lockfile, PluginManifest
from horae.plugins.host import HostRegistry, PluginHost
from horae.secrets import SecretResolver
from horae_sdk.tasks import SIGNIFIERS

SDK = Path(__file__).resolve().parents[1]
ROOT = SDK.parent
EXAMPLES = SDK / "examples"
HELLO = EXAMPLES / "hello" / "hello_plugin.py"
TASKS_FILE = EXAMPLES / "tasks_file" / "tasks_file_sink.py"
SCHEMAS = ROOT / "schemas"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

DUE, REMINDER, DONE, RECUR, CREATED, SCHEDULED, START = (
    "\U0001f4c5",
    "\u23f0",
    "\u2705",
    "\U0001f501",
    "\u2795",
    "\u23f3",
    "\U0001f6eb",
)
PRIORITIES = "\u23eb\U0001f53c\U0001f53d\U0001f53a\u23ec"
"""⏫ 🔼 🔽 🔺 ⏬"""

HOSTILE = (
    f"Pay {DUE} 2020-01-01 {REMINDER} 07:00 {DONE} 2020-01-02 {RECUR} every day "
    f"{CREATED} 2020-01-01 {SCHEDULED} 2020-01-01 {START} 2020-01-01 {PRIORITIES} "
    "**bold** _it_ `code` [link](https://example.org) [[Wiki]] ![[embed]] #tag "
    "==mark== %%hidden%% $x$ ^block <b>html</b> [due:: 2020-01-01] (start:: 2020-01-01) "
    "a|b ~~s~~ {x} &amp; back\\slash"
)
"""A title holding every signifier the threat model names, and Markdown."""

# A Tasks-like reader: the signifier fields it would parse from a line.
_FIELD = re.compile("(" + "|".join(map(re.escape, SIGNIFIERS)) + ")\ufe0f?")


def signifiers_in(text: str) -> list[str]:
    return _FIELD.findall(text)


def load_example(path: Path) -> ModuleType:
    """Import an example file as a module (named after the file)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def schema_errors(schema: str, document: Any) -> list[str]:
    """The core's generated JSON Schema `schemas/<schema>` errors for `document`."""
    data = json.loads((SCHEMAS / schema).read_text(encoding="utf-8"))
    validator = Draft202012Validator(data, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(document)]


def pinned(manifest: dict[str, Any], script: Path) -> dict[str, Any]:
    """`manifest` with its command pinned to this interpreter and `script` (absolute)."""
    return {
        **manifest,
        "runtime": {"protocol": "mcp-stdio", "command": [sys.executable, str(script)]},
    }


def lock_for(*manifests: dict[str, Any], write: list[str] | None = None) -> Lockfile:
    """A lock approving each manifest's permissions (plus `write` paths, the user's grant)."""
    plugins = {}
    for data in manifests:
        permissions = dict(data["permissions"])
        if write:
            permissions["paths"] = {"write": write}
        plugins[data["name"]] = {
            "version": data["version"],
            "repo": f"https://example.org/plugin-{data['name']}",
            "commit": COMMIT,
            "permissions": permissions,
        }
    return Lockfile.model_validate({"schema_version": 1, "plugins": plugins})


def host_for(root: Path, *manifests: dict[str, Any], write: list[str] | None = None) -> PluginHost:
    """The core's real plugin host over `manifests`."""
    models = {data["name"]: PluginManifest.model_validate(data) for data in manifests}
    registry = HostRegistry(lock_for(*manifests, write=write), models)
    secrets = root / "secrets"
    secrets.mkdir(exist_ok=True)
    return PluginHost(registry, secrets=SecretResolver({}, secrets), work_dir=root / "work")
