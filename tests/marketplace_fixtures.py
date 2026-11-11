# SPDX-License-Identifier: Apache-2.0
"""Local git repositories as marketplace indexes: no network in the tests."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from horae.marketplace import MarketplaceClient, PluginInstaller

_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def sha(n: int) -> str:
    return f"{n:040x}"


def entry(
    name: str,
    versions: dict[str, str] | list[str],
    *,
    repo: str | None = None,
    tier: str = "community",
) -> dict[str, Any]:
    if isinstance(versions, list):
        versions = {v: sha(i + 1) for i, v in enumerate(versions)}
    return {
        "name": name,
        "repo": repo or f"https://github.com/example/plugin-{name}",
        "versions": versions,
        "tier": tier,
    }


def publish(root: Path, plugins: list[dict[str, Any]] | str) -> Path:
    """Create or update the index repository at `root` with this `marketplace.json`."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
    text = plugins if isinstance(plugins, str) else json.dumps({"plugins": plugins}, indent=2)
    (root / "marketplace.json").write_text(text, encoding="utf-8")
    git(root, "add", "marketplace.json")
    git(root, "commit", "-q", "--allow-empty", "-m", "index")
    return root


class LocalClient(MarketplaceClient):
    """A client that maps the `https://` names to local repositories, over `file` only."""

    def __init__(self, cache_dir: Path, urls: list[str], remotes: dict[str, Path]) -> None:
        super().__init__(cache_dir, urls, allow_protocol="file", timeout_s=30)
        self.remotes = remotes

    def remote(self, url: str) -> str:
        return self.remotes[url].as_uri()


A = "https://example.org/a/marketplace"
B = "https://example.org/b/marketplace"


# --- plugin repositories (#44) --------------------------------------------------------------

PLUGIN_URL = "https://example.org/x/plugin-demo"


def plugin_manifest(name: str = "demo", version: str = "1.0.0", **changes: Any) -> str:
    data: dict[str, Any] = {
        "name": name,
        "version": version,
        "horae": ">=0",
        "provides": {"sources": [f"{name}.fetch"]},
        "permissions": {"network": ["api.example.org"]},
        "runtime": {"protocol": "mcp-stdio", "command": ["python3", "server.py"]},
        **changes,
    }
    return json.dumps(data, indent=2)


def plugin_repo(
    root: Path,
    *,
    name: str = "demo",
    version: str = "1.0.0",
    manifest: str | None = None,
    files: dict[str, str] | None = None,
    tag: str | None = "",
    annotated: bool = False,
) -> tuple[Path, str]:
    """A plugin repository at `root` with one commit tagged `v<version>` (unless `tag` is
    `None`; a non-empty `tag` names another tag). Returns the repository and the commit."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
    content = {"plugin.yaml": plugin_manifest(name, version), "server.py": "print('hi')\n"}
    if manifest is not None:
        content["plugin.yaml"] = manifest
    content.update(files or {})
    for path, text in content.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", f"release {version}")
    if tag is not None:
        tag_name = tag or f"v{version}"
        if annotated:
            git(root, "tag", "-f", "-a", tag_name, "-m", tag_name)
        else:
            git(root, "tag", "-f", tag_name)
    return root, git(root, "rev-parse", "HEAD")


class LocalInstaller(PluginInstaller):
    """An installer that maps the `https://` names to local repositories, over `file` only."""

    def __init__(self, plugins_dir: Path, remotes: dict[str, Path | str]) -> None:
        super().__init__(plugins_dir, allow_protocol="file", timeout_s=30)
        self.remotes = remotes

    def remote(self, url: str) -> str:
        where = self.remotes[url]
        return where.as_uri() if isinstance(where, Path) else where


def set_runtime(instance: Path, runtime: str, plugin: str | None = None) -> None:
    """Set `plugins.<plugin>.runtime` (or `plugin_defaults.runtime` when `plugin` is `None`) in
    the instance's `horae.yaml` (#191: a plugin that is not first-party needs `container`)."""
    path = instance / "horae.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if plugin is None:
        data.setdefault("plugin_defaults", {})["runtime"] = runtime
    else:
        plugins = data.get("plugins") or {}
        plugins.setdefault(plugin, {})["runtime"] = runtime
        data["plugins"] = plugins
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
