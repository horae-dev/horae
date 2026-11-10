# SPDX-License-Identifier: Apache-2.0
"""A plugin container's declared mounts (#51, schemas decision 78 A to C).

`plan_mounts` turns the plugin's **locked** `permissions.paths` into bind mounts, or refuses
the plugin (`runtime_unavailable`): nothing is mounted unless every declared path is safe.

- **Same path inside.** Each declared path is mounted at the same path in the container, so the
  paths the plugin already gets in `_meta["dev.horae/paths"]` (and any path a pipeline passes
  as a parameter) mean the same thing on both sides. Read-only unless declared in `write`
  (`write` implies read); `bind-recursive=disabled`, so a filesystem mounted below a declared
  path on the host is not carried in (and cannot stay writable under a read-only mount).
- **Nesting.** Mounts are ordered parent first, so a writable child of a read-only parent is
  writable and the rest of the parent read-only, and a read-only child of a writable parent
  stays read-only: each path behaves as declared.
- **Refusals.** A declared path must be canonical (its real path: no symbolic link in it, so
  the host cannot follow a link the plugin planted), exist, be a directory or a regular file,
  be carried by `--mount` (no `,`, `"` or control character), lie inside the instance
  directory or a `container.mount_roots` entry, and neither be nor contain nor lie inside a
  sensitive path (`MountPolicy.sensitive`: the instance's configuration, lock, state,
  plugins, pipelines, skills and secrets, the Docker socket, the user's credential
  directories, the system directories). `/tmp` and `/plugin` are the container's own.

`sweep` runs once the container is gone: in each writable mount, what the plugin created or
changed during its run (`st_ctime` since its start, which a plugin cannot set back) that the
host could trip over is undone and logged at WARNING: a symbolic link to a place the plugin
could not write itself (outside its writable paths, or in a read-only child of one) is
removed, a device, FIFO or socket is removed, a set-user-ID (or, on a file, set-group-ID) bit
is cleared. Every declared path must still be the same file (device and
inode) as when it was checked. Bind mounts cannot do this themselves: Docker's `--mount` takes
no `nosuid`/`nodev`/`nosymfollow`, so the mount has the host filesystem's own flags.
"""

import logging
import os
import re
import stat
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from horae.plugins.host.errors import PluginCallError

UNSAFE_MOUNT = re.compile(r'[,"\x00-\x1f\x7f]')
"""What `--mount` (a CSV field) cannot carry in a path."""
SYSTEM_PATHS = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/plugin",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/lib/containerd",
    "/var/lib/docker",
    "/var/lock",
    "/var/run",
)
"""Paths no declared path may be, contain or lie inside: the host's system and engine
directories, and the container's own (`/plugin`; `/proc`, `/dev`… are also the container's)."""
CONTAINER_ONLY = ("/tmp",)  # noqa: S108 (the container's tmpfs)
"""Paths a declared path may lie inside but neither be nor contain (the container's tmpfs)."""
HOME_SECRETS = (
    ".aws",
    ".azure",
    ".config",
    ".docker",
    ".git-credentials",
    ".gnupg",
    ".kube",
    ".netrc",
    ".pypirc",
    ".ssh",
)
"""Credential files and directories in Horae's home directory."""
INSTANCE_ENTRIES = (
    "horae.yaml",
    "horae.lock",
    "plugins",
    "pipelines",
    "skills",
    ".git",
)
"""Entries of the instance directory that steer Horae (besides its state and secrets)."""
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd")
SWEEP_LIMIT = 200_000
"""Entries `sweep` looks at, per plugin stop, before it gives up (and says so)."""
CTIME_SLACK_S = 2.0
"""Taken off the start time: filesystems that keep coarse timestamps."""
REPORTED = 50
"""Findings of one sweep logged one by one; the rest are counted."""
_SHOWN = 200
_log = logging.getLogger("horae.plugins.host.mounts")


@dataclass(frozen=True)
class MountPolicy:
    """Where a container plugin's declared paths may come from (decision 78 B)."""

    roots: tuple[str, ...]
    """The instance directory and `container.mount_roots`, as real paths."""
    sensitive: tuple[str, ...]
    """Paths a declared path may neither be, contain nor lie inside (lexical and real forms)."""

    @classmethod
    def for_instance(
        cls,
        root: Path,
        *,
        state_dir: Path,
        secrets_dir: Path,
        mount_roots: Iterable[str] = (),
        home: Path | None = None,
    ) -> "MountPolicy":
        """The policy of the instance at `root` (`home`: Horae's home, default `Path.home()`)."""
        try:
            home = Path.home() if home is None else home
        except RuntimeError:  # no home directory known
            home = None
        sensitive = [str(root / name) for name in INSTANCE_ENTRIES]
        sensitive += [str(state_dir), str(secrets_dir), *SYSTEM_PATHS, *DOCKER_SOCKETS]
        if home is not None:
            sensitive += [str(home / name) for name in HOME_SECRETS]
        roots = [str(root), *mount_roots]
        return cls(
            roots=_unique(os.path.realpath(path) for path in roots),
            sensitive=_unique(_both(sensitive)),
        )

    def with_sensitive(self, *paths: str) -> "MountPolicy":
        """This policy, with `paths` (e.g. the engine's socket) sensitive too."""
        return MountPolicy(self.roots, _unique([*self.sensitive, *_both(paths)]))


@dataclass(frozen=True)
class Mount:
    """One declared path, mounted at the same path in the container."""

    path: str
    writable: bool
    identity: tuple[int, int]
    """`st_dev` and `st_ino` when it was checked (`sweep` compares them)."""

    def option(self) -> str:
        """`docker run --mount`'s value."""
        value = f"type=bind,source={self.path},target={self.path},bind-recursive=disabled"
        return value if self.writable else f"{value},readonly"


def _both(paths: Iterable[str]) -> list[str]:
    """Each path's absolute lexical form and its real path."""
    out: list[str] = []
    for path in paths:
        absolute = os.path.normpath(os.path.abspath(path))
        out += [absolute, os.path.realpath(absolute)]
    return out


def _unique(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(paths)))


def within(path: str, parent: str) -> bool:
    """Whether `path` is `parent` or lies inside it (both normalised absolute paths)."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _refuse(plugin: str, path: str, why: str) -> PluginCallError:
    shown = repr(path[:_SHOWN])
    return PluginCallError(
        "runtime_unavailable",
        f"plugin {plugin} (runtime container): declared path {shown} cannot be mounted: {why}",
    )


def check_path(plugin: str, path: str, policy: MountPolicy) -> tuple[int, int]:
    """`path`'s device and inode if it may be mounted, else `runtime_unavailable` (decision 78
    B, in this order: shape, symbolic links, type, roots, sensitive paths)."""
    if UNSAFE_MOUNT.search(path):
        raise _refuse(plugin, path, 'it holds a ",", a quote or a control character')
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _refuse(plugin, path, "it is not a normalised absolute path")
    real = os.path.realpath(path)
    if real != path:
        raise _refuse(plugin, path, f"it is or goes through a symbolic link (real path {real!r})")
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        raise _refuse(plugin, path, "it does not exist") from None
    except OSError as error:
        raise _refuse(plugin, path, f"it cannot be read ({error.strerror})") from None
    if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
        raise _refuse(plugin, path, "it is not a directory or a regular file")
    if not any(within(path, root) for root in policy.roots):
        raise _refuse(
            plugin, path, "it is outside the instance directory and `container.mount_roots`"
        )
    for other in policy.sensitive:
        if within(path, other) or within(other, path):
            raise _refuse(plugin, path, f"it overlaps {other}, which no plugin may see")
    for other in CONTAINER_ONLY:
        if within(other, path):
            raise _refuse(plugin, path, f"it would hide the container's own {other}")
    return status.st_dev, status.st_ino


def plan_mounts(
    plugin: str, read: Sequence[str], write: Sequence[str], policy: MountPolicy | None
) -> tuple[Mount, ...]:
    """The bind mounts of the locked `read` and `write` paths, parents first, or
    `runtime_unavailable` (fail closed: one refused path refuses the plugin). Without a policy
    (a host built without an instance), any declared path is refused."""
    access = dict.fromkeys(read, False) | dict.fromkeys(write, True)
    if not access:
        return ()
    if policy is None:
        first = sorted(access)[0]
        raise _refuse(plugin, first, "this host has no instance to check it against")
    mounts = [
        Mount(path, writable, check_path(plugin, path, policy)) for path, writable in access.items()
    ]
    return tuple(sorted(mounts, key=lambda mount: (mount.path.count("/"), mount.path)))


# --- after the run ------------------------------------------------------------------------------


def writable_at(path: str, mounts: Sequence[Mount]) -> bool:
    """Whether the plugin could write `path` itself: the deepest of its mounts holding it is
    writable (a read-only child of a writable parent is not)."""
    holding = [mount for mount in mounts if within(path, mount.path)]
    if not holding:
        return False
    return max(holding, key=lambda mount: len(mount.path)).writable


def _escapes(link: str, target: str, mounts: Sequence[Mount]) -> bool:
    """Whether a symbolic link at `link` to `target` names a place the plugin could not write
    (lexically, as a host process following it would start): following it, the host would act
    outside what the plugin was granted."""
    joined = target if os.path.isabs(target) else os.path.join(os.path.dirname(link), target)
    return not writable_at(os.path.normpath(joined), mounts)


def _shown(path: str) -> str:
    return repr(path[:_SHOWN])


def _fix(path: str, status: os.stat_result, mounts: Sequence[Mount]) -> str | None:
    """Undo what the plugin left at `path` that the host could trip over; what was done."""
    mode = status.st_mode
    if stat.S_ISLNK(mode):
        target = os.readlink(path)
        if _escapes(path, target, mounts):
            os.unlink(path)
            return f"removed symbolic link {_shown(path)} -> {_shown(target)} (not writable)"
        return None
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        os.unlink(path)
        return f"removed special file {_shown(path)}"
    bits = stat.S_ISUID | (stat.S_ISGID if stat.S_ISREG(mode) else 0)
    if mode & bits:
        os.chmod(path, stat.S_IMODE(mode) & ~bits)  # a regular file or a directory (lstat)
        return f"cleared the set-user/group-ID bit of {_shown(path)}"
    return None


def sweep(plugin: str, mounts: Sequence[Mount], since: float) -> list[str]:
    """Check `mounts` once the plugin's container is gone (see the module docstring); returns
    what was found and done, each also logged at WARNING. Never raises."""
    findings: list[str] = []
    for mount in mounts:
        try:
            status = os.lstat(mount.path)
            same = (status.st_dev, status.st_ino) == mount.identity
        except OSError:
            same = False
        if not same:
            findings.append(f"declared path {_shown(mount.path)} was replaced or removed")
    writable = [mount.path for mount in mounts if mount.writable]
    tops = [path for path in writable if not any(p != path and within(path, p) for p in writable)]
    cutoff = since - CTIME_SLACK_S
    seen = 0
    for top in tops:
        try:
            device = os.lstat(top).st_dev
        except OSError:
            continue
        stack = [top]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen > SWEEP_LIMIT:
                    findings.append(f"more than {SWEEP_LIMIT} entries: not every one was checked")
                    return _report(plugin, findings)
                try:
                    status = entry.stat(follow_symlinks=False)
                    if status.st_dev != device:
                        continue  # another filesystem: not part of the mount
                    if stat.S_ISDIR(status.st_mode):
                        stack.append(entry.path)
                    if status.st_ctime < cutoff or not writable_at(entry.path, mounts):
                        continue  # older than the run, or in a read-only child: not its own
                    done = _fix(entry.path, status, mounts)
                except OSError as error:
                    done = f"could not check {_shown(entry.path)} ({error.strerror})"
                if done is not None:
                    findings.append(done)
    return _report(plugin, findings)


def _report(plugin: str, findings: list[str]) -> list[str]:
    for finding in findings[:REPORTED]:
        _log.warning("plugin %s (runtime container): %s", plugin, finding)
    if len(findings) > REPORTED:
        more = len(findings) - REPORTED
        _log.warning("plugin %s (runtime container): %d more such findings", plugin, more)
    return findings


def now() -> float:
    """The wall clock `sweep` compares `st_ctime` with."""
    return time.time()


__all__ = [
    "CONTAINER_ONLY",
    "SYSTEM_PATHS",
    "UNSAFE_MOUNT",
    "Mount",
    "MountPolicy",
    "check_path",
    "plan_mounts",
    "sweep",
    "within",
]
