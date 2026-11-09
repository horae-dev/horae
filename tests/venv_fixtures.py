# SPDX-License-Identifier: Apache-2.0
"""Plugins with their own dependencies (#45), and a local package index: no network.

`local_index(root)` writes a PEP 503 index of hand-made wheels of `tinydep` (1.0.0 and 2.0.0,
pure Python, `tinydep.VERSION`), served as a `file://` URL. `venv_plugin_repo` writes a plugin
that depends on one `tinydep` version, builds with `uv_build` (built into uv: nothing to
fetch), and locks it against that index with uv itself. Its server is a hand-written MCP stdio
server (the venv holds no MCP SDK), whose one tool `<name>.info` reports the `tinydep` it
imported, its interpreter and its environment.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from marketplace_fixtures import plugin_repo

TINYDEP_VERSIONS = ("1.0.0", "2.0.0")
UV = shutil.which("uv")

_SERVER = """\
import json
import os
import sys

import tinydep


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        version = message["params"]["protocolVersion"]
        info = {"name": "NAME", "version": "1"}
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": info}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "tools/list":
        tools = [{"name": "NAME.info", "inputSchema": {"type": "object"}}]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
    elif method == "tools/call":
        data = {
            "tinydep": tinydep.VERSION,
            "prefix": sys.prefix,
            "executable": sys.executable,
            "env": dict(os.environ),
        }
        result = {"content": [], "structuredContent": data}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif request_id is not None and method is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
"""


def _wheel(directory: Path, version: str) -> Path:
    path = directory / f"tinydep-{version}-py3-none-any.whl"
    info = f"tinydep-{version}.dist-info"
    files = {
        "tinydep/__init__.py": f"VERSION = {version!r}\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: tinydep\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    record = []
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            data = text.encode()
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{name},sha256={digest.decode()},{len(data)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return path


def local_index(root: Path) -> str:
    """A PEP 503 index of `tinydep` at `root`; returns its `file://` URL."""
    project = root / "tinydep"
    project.mkdir(parents=True)
    links = []
    for version in TINYDEP_VERSIONS:
        wheel = _wheel(project, version)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        links.append(f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>')
    (project / "index.html").write_text(f"<html><body>{''.join(links)}</body></html>")
    (root / "index.html").write_text('<html><body><a href="tinydep/">tinydep</a></body></html>')
    return root.as_uri()


def _uv_env(cache: Path) -> dict[str, str]:
    keep = {"PATH", "HOME", "TMPDIR", "LANG"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update(UV_NO_CONFIG="1", UV_CACHE_DIR=str(cache), UV_PYTHON_DOWNLOADS="never")
    return env


def lock(project: Path, index_url: str, cache: Path) -> None:
    """`uv lock` the project at `project` against the local index (offline)."""
    assert UV is not None
    subprocess.run(  # noqa: S603
        [UV, "lock", "--offline", "--default-index", index_url],
        cwd=project,
        env=_uv_env(cache),
        capture_output=True,
        check=True,
    )


def pyproject(
    name: str,
    version: str,
    dependencies: list[str],
    *,
    build: str | None = None,
    requires_python: str = ">=3.12",
) -> str:
    deps = ", ".join(json.dumps(d) for d in dependencies)
    build = build or '[build-system]\nrequires = ["uv_build>=0.7,<1"]\nbuild-backend = "uv_build"\n'
    return (
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        f'requires-python = "{requires_python}"\ndependencies = [{deps}]\n\n{build}'
    )


def venv_manifest(name: str, version: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "horae": ">=0",
        "provides": {"sources": [f"{name}.info"]},
        "permissions": {},
        "runtime": {"protocol": "mcp-stdio", "command": ["python", "-m", name]},
    }


def venv_plugin_files(
    name: str, version: str, tinydep: str, *, build: str | None = None
) -> dict[str, str]:
    """The files of a plugin `name` that depends on `tinydep==<tinydep>` (not locked yet)."""
    return {
        "plugin.yaml": json.dumps(venv_manifest(name, version), indent=2),
        "pyproject.toml": pyproject(name, version, [f"tinydep=={tinydep}"], build=build),
        f"src/{name}/__init__.py": "",
        f"src/{name}/__main__.py": _SERVER.replace("NAME", name),
    }


def venv_plugin_repo(
    root: Path,
    name: str,
    version: str,
    tinydep: str,
    *,
    index_url: str,
    cache: Path,
    files: dict[str, str] | None = None,
    locked: bool = True,
) -> tuple[Path, str]:
    """A plugin repository at `root`, locked against the index unless `locked` is false;
    `files` are written after the lock (to break it)."""
    content = venv_plugin_files(name, version, tinydep)
    root.mkdir(parents=True, exist_ok=True)
    for path, text in content.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    if locked:
        lock(root, index_url, cache)
    content["uv.lock"] = (root / "uv.lock").read_text() if locked else ""
    if not locked:
        del content["uv.lock"]
    content.update(files or {})
    return plugin_repo(
        root, name=name, version=version, manifest=content.pop("plugin.yaml"), files=content
    )
