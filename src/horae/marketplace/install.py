# SPDX-License-Identifier: Apache-2.0
"""Install a plugin from git at a pinned commit (#44, schemas decision 62).

`PluginInstaller.install(name, version, repo, sha)` is the whole install: the marketplace
resolves a name to `(repo, sha)` first (`horae plugin add`). A direct install from a git URL
(#48) calls `install_commit(repo, sha)`: the same steps, but no tag is checked: the repository's
own branches and tags are fetched and the full SHA must be reachable from one of them (never
from a fork's objects a forge serves through the same URL); the name and version are those of
its `plugin.yaml`. A repository is
untrusted, so:

- **git** is the marketplace's hardened runner (`horae.marketplace.git`): `https` only, no
  user or system config, an allow-listed environment, a timeout, `--` before every URL, no
  hook (`core.hooksPath=/dev/null`), no LFS smudge;
- **fetch**: `git clone --bare --depth=1 --no-tags --no-recurse-submodules --branch v<version>
  -- <repo>`, then the pinned SHA and the tag `v<version>` are fetched again explicitly from
  the remote (a branch named like the tag cannot stand in for it);
- **verify**: `<sha>^{commit}` must be the pinned SHA, and `refs/tags/v<version>^{commit}`
  (an annotated tag is dereferenced) must be the same commit, else the install is refused;
- **limits**: at most `MAX_FETCH_BYTES` fetched, `MAX_FILES` entries and `MAX_TREE_BYTES` of
  file content in the tree; no path with a `.`, `..` or `.git` segment; no symlink that
  points outside the tree (absolute, or climbing out with `..`);
- **checkout** of the tree only (no `.git` in the install), with `core.symlinks=false` (a
  symlink is written as a small file holding its target), `core.protectHFS` and
  `core.protectNTFS`, `--no-recurse-submodules` (a submodule stays an empty directory). No
  filter runs: `.gitattributes` can name a filter, but only config defines one and the global
  and system configs are `/dev/null`;
- **manifest**: `plugin.yaml` at the commit must validate as `PluginManifest`, with the name
  and version that were resolved;
- **place**: everything is built in a scratch directory `plugins/.install-*` and the finished
  tree is renamed to `plugins/<name>` (same file system, atomic), so `plugins/<name>` is
  either absent or complete, even if the process is killed. The target is derived from the
  validated name only. A scratch directory left by a killed install is never read and is
  removed by a later install once it is older than `STALE_SCRATCH_S`.

An installed plugin is **not enabled**: the plugin host loads only the plugins of
`horae.lock` (`HostRegistry.from_directory`), and writing that entry with the approved
permissions is #46. Its dependencies are #45: `prepare` is the hook that runs on the verified
tree before it is moved into place, and `horae plugin add` passes
`horae.marketplace.environment.PluginEnvironment`, which builds the plugin's venv in
`<tree>/.venv` from its committed `uv.lock` (decision 69). So the venv belongs to the tree: an
upgrade (#47) installs a new tree, with a fresh venv, through this same atomic path. What was
installed is recorded in the install directory, `plugins/<name>/.horae-install.json`:
`{schema_version, name, version, repo, commit}`, the fields of a `horae.lock` entry but the
permissions (`Installed.lock_entry`).
"""

import errno
import json
import os
import posixpath
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from horae.config.documents import ProblemsError, read_yaml, validate
from horae.contracts.lockfile import GitRepoUrl, LockedPlugin
from horae.contracts.plugin import Permissions, PluginManifest
from horae.contracts.types import CommitSha, PluginName, SemVer
from horae.marketplace.errors import AlreadyInstalled, InstallError, InstallRefused
from horae.marketplace.git import GIT_TIMEOUT_S, HTTPS, GitError, GitTooLarge, run_git

MANIFEST_FILE = "plugin.yaml"
INSTALL_RECORD = ".horae-install.json"
"""In the install directory: what was installed (decision 62)."""
SCRATCH_PREFIX = ".install-"
STALE_SCRATCH_S = 3600
"""A scratch directory older than this is left by a killed install and may be removed."""
MAX_FETCH_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
MAX_TREE_BYTES = 100 * 1024 * 1024
MAX_LINK_BYTES = 4096
MAX_DEPTH = 32
MAX_PATH_CHARS = 1024
MAX_PROBLEMS = 3
MAX_DIRECT_REFS = 1000
"""A direct install (#48): at most this many branches and tags are fetched to prove the
commit is the repository's own."""
MAX_REACH_DEPTH = 1000
"""A direct install: how many commits of each branch and tag are fetched; a commit further
from every tip cannot be proven reachable and is refused."""
_REACH_PATTERNS = ("refs/heads/*", "refs/tags/*")
_TIP_REF = re.compile(r"^refs/(heads|tags)/[A-Za-z0-9._/-]{1,200}$")
_CHECKOUT_OPTIONS = ("core.symlinks=false", "core.protectHFS=true", "core.protectNTFS=true")

_name: TypeAdapter[str] = TypeAdapter(PluginName)
_version: TypeAdapter[str] = TypeAdapter(SemVer)
_sha: TypeAdapter[str] = TypeAdapter(CommitSha)
_repo: TypeAdapter[str] = TypeAdapter(GitRepoUrl)

Prepare = Callable[[Path, PluginManifest], None]
"""Run on the verified tree before it is moved into place (#45: the plugin's dependencies).
An exception aborts the install and leaves nothing."""


@dataclass(frozen=True)
class Installed:
    """A plugin installed at a verified commit."""

    name: str
    version: str
    repo: str
    commit: str
    path: Path
    manifest: PluginManifest

    def record(self) -> dict[str, Any]:
        """The content of `INSTALL_RECORD`."""
        return {
            "schema_version": 1,
            "name": self.name,
            "version": self.version,
            "repo": self.repo,
            "commit": self.commit,
        }

    def lock_entry(self, permissions: Permissions) -> LockedPlugin:
        """The `horae.lock` entry of this plugin with the permissions the user approved
        (#46 writes it; until then the plugin host does not load the plugin). The manifest's
        `provides.personal_fields` are copied as they are: a declaration only adds protection,
        so it needs no approval (decision 68)."""
        return LockedPlugin(
            version=self.version,
            repo=self.repo,
            commit=self.commit,
            permissions=permissions,
            personal_fields=self.manifest.provides.personal_fields,
        )


def _refused_problems(exc: ProblemsError) -> str:
    shown = [p.message for p in exc.problems]
    more = f" (and {len(shown) - MAX_PROBLEMS} more)" if len(shown) > MAX_PROBLEMS else ""
    return "; ".join(shown[:MAX_PROBLEMS]) + more


def _bad_path(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) > MAX_DEPTH
        or len(path) > MAX_PATH_CHARS
        or "\0" in path
        or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts)
    )


def _escapes(path: str, target: str) -> bool:
    """Whether a symlink at `path` (relative to the tree) pointing to `target` leaves it."""
    if not target or target.startswith("/") or "\0" in target or "\\" in target:
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved == ".." or resolved.startswith("../")


class PluginInstaller:
    """Installs plugins into an instance's plugin directory (`<instance>/plugins`).

    `allow_protocol` is the one git transport to allow: `https`, never configurable from
    `horae.yaml`; only tests pass `file` (with `remote`) to clone local fixture repositories.
    """

    def __init__(
        self,
        plugins_dir: Path,
        *,
        allow_protocol: str = HTTPS,
        timeout_s: float = GIT_TIMEOUT_S,
    ) -> None:
        self.plugins_dir = plugins_dir
        self.allow_protocol = allow_protocol
        self.timeout_s = timeout_s

    def remote(self, url: str) -> str:
        """The address git is given for `url`: `url` itself. A seam for tests."""
        return url

    # --- the install ---------------------------------------------------------------------

    def install(
        self, name: str, version: str, repo: str, sha: str, *, prepare: Prepare | None = None
    ) -> Installed:
        """Fetch `repo`, verify that `sha` and the tag `v<version>` are the same commit, and
        install its tree as `plugins/<name>`. `InstallRefused` when verification fails,
        `AlreadyInstalled` when the name is taken, `InstallError` when git fails; in every
        case nothing is left in `plugins/`."""
        try:
            name = _name.validate_python(name)
            version = _version.validate_python(version)
            sha = _sha.validate_python(sha)
        except ValidationError:
            raise InstallRefused(
                f"{name!r}@{version!r} at {sha!r}: not a plugin name, version and full commit SHA"
            ) from None
        self._check_repo(repo)
        target = self.plugins_dir / name
        if os.path.lexists(target):
            raise AlreadyInstalled(f"plugin {name!r} is already installed in {target}")
        return self._install(repo, sha, (name, version), prepare)

    def install_commit(self, repo: str, sha: str, *, prepare: Prepare | None = None) -> Installed:
        """A direct install from a git URL (#48): fetch the branches and tags of `repo`, require
        the commit `sha` to be reachable from one of them (no tag is compared: the full SHA is
        the pin) and install its tree as
        `plugins/<name>`, the name and version being those of its `plugin.yaml`. The same
        limits and checks as `install`; the name is checked against `plugins/` once the
        manifest is read, before `prepare` runs."""
        try:
            sha = _sha.validate_python(sha)
        except ValidationError:
            raise InstallRefused(f"{sha!r} is not a full commit SHA") from None
        self._check_repo(repo)
        return self._install(repo, sha, None, prepare)

    def _check_repo(self, repo: str) -> None:
        if self.allow_protocol == HTTPS:
            try:
                _repo.validate_python(repo)
            except ValidationError:
                raise InstallRefused(f"{repo!r} is not an https git URL") from None

    def _install(
        self,
        repo: str,
        sha: str,
        expected: tuple[str, str] | None,
        prepare: Prepare | None,
    ) -> Installed:
        """The install of `sha` from `repo`: at the tag of `expected = (name, version)`, or,
        when `expected` is `None`, a direct install of the commit alone."""
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            self._sweep()
            scratch = Path(tempfile.mkdtemp(dir=self.plugins_dir, prefix=SCRATCH_PREFIX))
        except OSError as exc:
            raise InstallError(f"cannot write {self.plugins_dir}: {exc.strerror}") from None
        try:
            git_dir, tree = scratch / "repo.git", scratch / "tree"
            if expected is None:
                self._fetch_commit(scratch, git_dir, repo, sha)
            else:
                self._fetch(scratch, git_dir, repo, expected[1], sha)
                self._verify(git_dir, expected[1], sha)
            self._check_tree(git_dir, sha)
            tree.mkdir()
            self._checkout(scratch, git_dir, tree, sha)
            self._check_checkout(tree)
            manifest = self._manifest(tree, expected)
            name, version = manifest.name, manifest.version
            target = self.plugins_dir / name
            if expected is None and os.path.lexists(target):
                raise AlreadyInstalled(f"plugin {name!r} is already installed in {target}")
            installed = Installed(name, version, repo, sha, target, manifest)
            (tree / INSTALL_RECORD).write_text(
                json.dumps(installed.record(), indent=2) + "\n", encoding="utf-8"
            )
            if prepare is not None:
                prepare(tree, manifest)
            if os.path.lexists(target):
                raise AlreadyInstalled(f"plugin {name!r} is already installed in {target}")
            self._place(tree, target, name)
        except OSError as exc:
            what = repo if expected is None else expected[0]
            raise InstallError(f"cannot install {what}: {exc.strerror or exc}") from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return installed

    def _place(self, tree: Path, target: Path, name: str) -> None:
        """Rename `tree` to `target`, which must not exist. `rename` onto a non-empty directory
        fails, so an install that lost a race never replaces the winner's (a plugin directory
        always holds `plugin.yaml`)."""
        try:
            tree.rename(target)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise AlreadyInstalled(
                    f"plugin {name!r} is already installed in {target}"
                ) from None
            raise

    # --- steps ---------------------------------------------------------------------------

    def _git(
        self, cwd: Path, *args: str, options: tuple[str, ...] = (), watch: Path | None = None
    ) -> str:
        if watch is None:
            return run_git(
                cwd, *args, allow=self.allow_protocol, timeout_s=self.timeout_s, options=options
            )
        # A download is killed once the scratch directory holds more than the budget.
        return run_git(
            cwd,
            *args,
            allow=self.allow_protocol,
            timeout_s=self.timeout_s,
            options=options,
            watch=watch,
            max_bytes=MAX_FETCH_BYTES,
        )

    def _fetch(self, scratch: Path, git_dir: Path, repo: str, version: str, sha: str) -> None:
        url, tag = self.remote(repo), f"v{version}"
        try:
            self._git(
                scratch,
                "clone",
                "--bare",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "--single-branch",
                "--no-recurse-submodules",
                "--branch",
                tag,
                "--",
                url,
                str(git_dir),
                watch=scratch,
            )
        except GitTooLarge:
            raise InstallRefused(f"{repo} is more than {MAX_FETCH_BYTES} bytes") from None
        except GitError as exc:
            raise InstallError(f"cannot clone {repo} at tag {tag}: {exc}") from None
        try:
            self._git(
                git_dir,
                "fetch",
                "--quiet",
                "--force",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--",
                url,
                sha,
                f"+refs/tags/{tag}:refs/tags/{tag}",
                watch=scratch,
            )
        except GitTooLarge:
            raise InstallRefused(f"{repo} at {sha} is more than {MAX_FETCH_BYTES} bytes") from None
        except GitError as exc:
            raise InstallError(
                f"cannot fetch commit {sha} and tag {tag} from {repo}: {exc}"
            ) from None
        fetched = self._fetched_bytes(git_dir)
        if fetched > MAX_FETCH_BYTES:
            raise InstallRefused(f"{repo} at {sha} is {fetched} bytes, more than {MAX_FETCH_BYTES}")

    def _fetch_commit(self, scratch: Path, git_dir: Path, repo: str, sha: str) -> None:
        """A direct install (#48): fetch the repository's own branches and tags, never the
        commit by its id, and require `sha` to be reachable from one of them.

        A forge may serve, through a repository's URL, objects that exist only in a fork (or
        in a pull request's `refs/pull/*`): fetching `sha` directly would then install a
        fork's code under the trusted repository's name. So `git ls-remote` lists the tips of
        `refs/heads/*` and `refs/tags/*` (at most `MAX_DIRECT_REFS`), those refs alone are
        fetched (shallow, `MAX_REACH_DEPTH` commits, the same byte budget and hardening), and
        `merge-base --is-ancestor <sha> <tip>` must hold for one of them. Anything that cannot
        be proven reachable is refused."""
        url = self.remote(repo)
        try:
            self._git(scratch, "init", "--bare", "--quiet", str(git_dir))
        except GitError as exc:
            raise InstallError(f"cannot create a repository: {exc}") from None
        try:
            listing = self._git(scratch, "ls-remote", "--refs", "--", url, *_REACH_PATTERNS)
        except GitError as exc:
            raise InstallError(f"cannot list the branches and tags of {repo}: {exc}") from None
        refs = sorted(
            {
                ref
                for line in listing.splitlines()
                for _oid, _, ref in [line.partition("\t")]
                if _TIP_REF.match(ref)
            }
        )
        if not refs:
            raise InstallRefused(f"{repo} has no branch or tag to prove that {sha} is its own")
        if len(refs) > MAX_DIRECT_REFS:
            raise InstallRefused(
                f"{repo} has more than {MAX_DIRECT_REFS} branches and tags: cannot check "
                f"that {sha} is reachable from them"
            )
        try:
            self._git(
                git_dir,
                "fetch",
                "--quiet",
                "--force",
                f"--depth={MAX_REACH_DEPTH}",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--",
                url,
                *(f"+{ref}:{ref}" for ref in refs),
                watch=scratch,
            )
        except GitTooLarge:
            raise InstallRefused(
                f"the branches and tags of {repo} are more than {MAX_FETCH_BYTES} bytes"
            ) from None
        except GitError as exc:
            raise InstallError(f"cannot fetch the branches and tags of {repo}: {exc}") from None
        fetched = self._fetched_bytes(git_dir)
        if fetched > MAX_FETCH_BYTES:
            raise InstallRefused(f"{repo} is {fetched} bytes, more than {MAX_FETCH_BYTES}")
        if self._commit_of(git_dir, sha) != sha or not any(
            self._is_ancestor(git_dir, sha, ref) for ref in refs
        ):
            raise InstallRefused(
                f"commit {sha} is not reachable from a branch or tag of {repo} (within "
                f"{MAX_REACH_DEPTH} commits of its tip): refusing (a commit of a fork or a "
                "pull request is not the repository's own)"
            )

    def _is_ancestor(self, git_dir: Path, sha: str, ref: str) -> bool:
        try:
            self._git(git_dir, "merge-base", "--is-ancestor", sha, f"{ref}^{{commit}}")
        except GitError:
            return False
        return True

    def _fetched_bytes(self, git_dir: Path) -> int:
        try:
            out = self._git(git_dir, "count-objects", "-v")
        except GitError as exc:
            raise InstallError(str(exc)) from None
        pairs = (line.partition(": ") for line in out.splitlines())
        sizes = {key: value for key, _, value in pairs}
        try:
            return (int(sizes.get("size", "0")) + int(sizes.get("size-pack", "0"))) * 1024
        except ValueError:
            raise InstallError("git count-objects printed an unexpected size") from None

    def _commit_of(self, git_dir: Path, rev: str) -> str | None:
        try:
            return self._git(git_dir, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        except GitError:
            return None

    def _verify(self, git_dir: Path, version: str, sha: str) -> None:
        tag = f"v{version}"
        if self._commit_of(git_dir, sha) != sha:
            raise InstallRefused(f"commit {sha} is not a commit of the repository")
        tagged = self._commit_of(git_dir, f"refs/tags/{tag}")
        if tagged != sha:
            raise InstallRefused(
                f"tag {tag} points to {tagged or 'no commit'}, but {sha} is pinned: refusing "
                "(a moved tag, or a tampered index)"
            )

    def _check_tree(self, git_dir: Path, sha: str) -> None:
        try:
            listing = self._git(git_dir, "ls-tree", "-r", "-l", "-z", "--full-tree", sha)
        except GitError as exc:
            raise InstallRefused(f"cannot list the tree of {sha}: {exc}") from None
        entries = [e for e in listing.split("\0") if e]
        if len(entries) > MAX_FILES:
            raise InstallRefused(f"the tree has more than {MAX_FILES} files")
        total = 0
        for entry in entries:
            meta, _, path = entry.partition("\t")
            fields = meta.split()
            if len(fields) != 4:
                raise InstallRefused("git ls-tree printed an unexpected entry")
            mode, kind, oid, size = fields
            if _bad_path(path):
                raise InstallRefused(f"the tree has an invalid path: {path!r}")
            if path == INSTALL_RECORD:
                raise InstallRefused(f"the tree has a file named {INSTALL_RECORD}")
            if kind != "blob" or mode == "160000":
                # A gitlink (submodule) is never fetched; refused rather than checked out empty,
                # so what is installed is exactly what the commit holds.
                raise InstallRefused(f"the tree has a submodule (gitlink): {path!r}")
            total += int(size)
            if total > MAX_TREE_BYTES:
                raise InstallRefused(f"the tree is larger than {MAX_TREE_BYTES} bytes")
            if mode == "120000" and path.startswith("pipelines/"):
                # A pack's pipelines are copied into the instance (#133): never a link.
                raise InstallRefused(f"the symlink {path!r} is in pipelines/: not allowed")
            if mode == "120000":
                self._check_link(git_dir, path, oid, int(size))

    def _check_link(self, git_dir: Path, path: str, oid: str, size: int) -> None:
        if size > MAX_LINK_BYTES:
            raise InstallRefused(f"the symlink {path!r} has a target longer than {MAX_LINK_BYTES}")
        try:
            target = self._git(git_dir, "cat-file", "blob", oid)
        except GitError as exc:
            raise InstallRefused(f"cannot read the symlink {path!r}: {exc}") from None
        if _escapes(path, target):
            raise InstallRefused(f"the symlink {path!r} points outside the plugin: {target!r}")

    def _checkout(self, scratch: Path, git_dir: Path, tree: Path, sha: str) -> None:
        options: list[str] = []
        for setting in _CHECKOUT_OPTIONS:
            options += ["-c", setting]
        options += ["--git-dir", str(git_dir), "--work-tree", str(tree)]
        try:
            self._git(
                scratch,
                "checkout",
                "--quiet",
                "--force",
                "--no-recurse-submodules",
                sha,
                "--",
                ".",
                options=tuple(options),
            )
        except GitError as exc:
            raise InstallError(f"cannot check out {sha}: {exc}") from None

    def _check_checkout(self, tree: Path) -> None:
        """No symlink in what was written (`core.symlinks=false` already ensures it)."""
        for root, dirs, files in os.walk(tree):
            for entry in (*dirs, *files):
                if os.path.islink(os.path.join(root, entry)):
                    raise InstallRefused(f"the checkout holds a symlink: {entry!r}")

    def _manifest(self, tree: Path, expected: tuple[str, str] | None) -> PluginManifest:
        path = tree / MANIFEST_FILE
        if not path.is_file():
            raise InstallRefused(f"the plugin has no {MANIFEST_FILE}")
        try:
            manifest = validate(read_yaml(path), PluginManifest)
        except ProblemsError as exc:
            raise InstallRefused(f"{MANIFEST_FILE} is invalid: {_refused_problems(exc)}") from None
        if expected is not None and (manifest.name, manifest.version) != expected:
            raise InstallRefused(
                f"{MANIFEST_FILE} declares {manifest.name}@{manifest.version}, "
                f"but {expected[0]}@{expected[1]} was asked"
            )
        return manifest

    def _sweep(self) -> None:
        """Remove the scratch directories of killed installs (older than an hour)."""
        cutoff = time.time() - STALE_SCRATCH_S
        for entry in self.plugins_dir.glob(f"{SCRATCH_PREFIX}*"):
            try:
                if not entry.is_symlink() and entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
