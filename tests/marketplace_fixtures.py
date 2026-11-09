# SPDX-License-Identifier: Apache-2.0
"""Local git repositories as marketplace indexes: no network in the tests."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from horae.marketplace import MarketplaceClient

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
