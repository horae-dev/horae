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
inode) as when it was checked. What the sweep cannot check (a listing or `lstat` that fails, an
entry on another device that is not a host mount point, more than `NEW_LIMIT` new or
`WALK_LIMIT` entries, a replaced declared path) **quarantines** the plugin: a file in
`<state_dir>/quarantine/` refuses its next start until the operator removes it. Bind mounts
cannot do this themselves: Docker's `--mount` takes
no `nosuid`/`nodev`/`nosymfollow`, so the mount has the host filesystem's own flags.
"""

import logging
import os
import re
import stat
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
NEW_LIMIT = 100_000
"""Entries created or changed during the run that `sweep` checks; past it the plugin is
quarantined (fail closed)."""
WALK_LIMIT = 2_000_000
"""Entries `sweep` walks in all; past it the plugin is quarantined (fail closed)."""
QUARANTINE_DIR = "quarantine"
"""Under the state directory: one file per quarantined plugin, which the operator removes."""
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
    quarantine: str | None = None
    """The directory of quarantine files (`<state_dir>/quarantine`), `None` in tests."""

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
            quarantine=str(state_dir / QUARANTINE_DIR),
        )

    def with_sensitive(self, *paths: str) -> "MountPolicy":
        """This policy, with `paths` (e.g. the engine's socket) sensitive too."""
        sensitive = _unique([*self.sensitive, *_both(paths)])
        return MountPolicy(self.roots, sensitive, self.quarantine)


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
    # The same by file identity: another case on a case-insensitive volume, or an alias.
    own = {(status.st_dev, status.st_ino), *_ancestors(plugin, path)}
    for other in policy.sensitive:
        try:
            found = os.stat(other)
        except OSError:
            continue  # absent: nothing there to reach
        if (found.st_dev, found.st_ino) in own or (status.st_dev, status.st_ino) in _chain(other):
            raise _refuse(plugin, path, f"it overlaps {other}, which no plugin may see")
    for other in CONTAINER_ONLY:
        if within(other, path):
            raise _refuse(plugin, path, f"it would hide the container's own {other}")
    return status.st_dev, status.st_ino


def plan_mounts(
    plugin: str,
    read: Sequence[str],
    write: Sequence[str],
    policy: MountPolicy | None,
    shared: Sequence[tuple[str, str]] = (),
) -> tuple[Mount, ...]:
    """The bind mounts of the locked `read` and `write` paths, parents first, or
    `runtime_unavailable` (fail closed: one refused path refuses the plugin). Without a policy
    (a host built without an instance), any declared path is refused. `shared` lists the
    writable paths of the **other** container plugins (`(plugin, path)`): a declared path
    strictly inside one is refused, since that plugin could swap a directory of it for a link
    while this one starts (decision 78 B)."""
    access = dict.fromkeys(read, False) | dict.fromkeys(write, True)
    if not access:
        return ()
    if policy is None:
        first = sorted(access)[0]
        raise _refuse(plugin, first, "this host has no instance to check it against")
    writers = _writer_identities(plugin, shared, sorted(access)[0])
    planned: list[Mount] = []
    for path, writable in access.items():
        identity = check_path(plugin, path, policy)
        for ancestor in _ancestors(plugin, path):
            for other, written, written_identity in writers:
                if ancestor == written_identity:
                    raise _refuse(
                        plugin, path, f"it lies inside {written!r}, which plugin {other} may write"
                    )
        planned.append(Mount(path, writable, identity))
    return tuple(sorted(planned, key=lambda mount: (mount.path.count("/"), mount.path)))


def _writer_identities(
    plugin: str, shared: Sequence[tuple[str, str]], first: str
) -> list[tuple[str, str, tuple[int, int]]]:
    """Each other plugin's writable path with its file identity (device and inode, after
    `realpath`), so that an alias of it (another case on a case-insensitive volume, a link, a
    bind mount) is recognised. One that cannot be read refuses the plugin (fail closed)."""
    found = []
    for other, written in shared:
        if other == plugin:
            continue
        try:
            status = os.stat(os.path.realpath(written))
        except OSError as error:
            why = f"{written!r}, which plugin {other} may write, cannot be read ({error.strerror})"
            raise _refuse(plugin, first, why) from None
        found.append((other, written, (status.st_dev, status.st_ino)))
    return found


def _ancestors(plugin: str, path: str) -> list[tuple[int, int]]:
    """The file identities of `path`'s strict ancestors, up to `/`."""
    identities = []
    current = os.path.dirname(path)
    while True:
        try:
            status = os.lstat(current)
        except OSError as error:
            raise _refuse(plugin, path, f"{current!r} cannot be read ({error.strerror})") from None
        identities.append((status.st_dev, status.st_ino))
        parent = os.path.dirname(current)
        if parent == current:
            return identities
        current = parent


def _chain(path: str) -> set[tuple[int, int]]:
    """The file identities of `path`'s real path and its ancestors (unreadable ones left out)."""
    identities = set()
    current = os.path.realpath(path)
    while True:
        try:
            status = os.stat(current)
            identities.add((status.st_dev, status.st_ino))
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return identities
        current = parent


def recheck(plugin: str, planned: Sequence[Mount], policy: MountPolicy | None) -> None:
    """Check `planned` again, right before the engine mounts them: each path still passes
    `check_path` and is the same file (device and inode); else `runtime_unavailable`."""
    if policy is None:
        return
    for mount in planned:
        if check_path(plugin, mount.path, policy) != mount.identity:
            raise _refuse(plugin, mount.path, "it was replaced since it was checked")


def mounts_problem(planned: Sequence[Mount], inspected: object, plugin_mount: str) -> str | None:
    """Why a started container's mounts (`docker inspect`'s `.Mounts`) are not exactly the
    plugin directory and `planned`, each from its own path and with its access, or `None`."""
    if not isinstance(inspected, list):
        return "its mounts cannot be read"
    seen: dict[str, tuple[object, object]] = {}
    for item in inspected:
        if not isinstance(item, dict) or item.get("Type") != "bind":
            return "it has a mount that is not a bind mount"
        seen[str(item.get("Destination"))] = (item.get("Source"), item.get("RW"))
    wanted = {mount.path: (mount.path, mount.writable) for mount in planned}
    if set(seen) != set(wanted) | {plugin_mount}:
        return "its mounts are not the declared ones"
    for path, expected in wanted.items():
        if seen[path] != expected:
            return f"{path!r} is not mounted from itself with its declared access"
    return None


def _quarantine_file(policy: MountPolicy | None, plugin: str) -> Path | None:
    if policy is None or policy.quarantine is None:
        return None
    return Path(policy.quarantine) / plugin


def check_quarantine(plugin: str, policy: MountPolicy | None) -> None:
    """`runtime_unavailable` while `plugin` is quarantined (`quarantine`)."""
    marker = _quarantine_file(policy, plugin)
    if marker is not None and os.path.lexists(marker):
        raise PluginCallError(
            "runtime_unavailable",
            f"plugin {plugin} (runtime container) is quarantined: the check of its writable "
            f"paths after its last run failed (reasons in {marker}). Check those paths, then "
            "remove that file",
        )


def quarantine(plugin: str, policy: MountPolicy | None, reasons: Sequence[str]) -> bool:
    """Record that `plugin` must not start again until the operator clears it; whether the
    file could be written (the caller also keeps it in memory)."""
    marker = _quarantine_file(policy, plugin)
    if marker is None:
        return False
    text = "".join(f"{reason}\n" for reason in reasons[:REPORTED])
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
    except OSError:
        return False
    _log.warning("plugin %s (runtime container): quarantined (%s)", plugin, marker)
    return True


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


@dataclass
class Sweep:
    """What `sweep` found and did."""

    findings: list[str] = field(default_factory=list)
    """Everything, logged at WARNING."""
    failures: list[str] = field(default_factory=list)
    """What it could not check or undo: the plugin is then quarantined (fail closed)."""

    def fail(self, message: str) -> None:
        self.findings.append(message)
        self.failures.append(message)


def mount_points() -> frozenset[str]:
    """The host's mount points (`/proc/self/mountinfo`), empty where there is none."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as info:
            lines = info.read().splitlines()
    except OSError:
        return frozenset()
    points = set()
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    return frozenset(points)


def sweep(
    plugin: str, mounts: Sequence[Mount], since: float, points: frozenset[str] | None = None
) -> Sweep:
    """Check `mounts` once the plugin's container is gone (see the module docstring). Never
    raises: what it cannot check is a failure (`Sweep.failures`), never skipped quietly."""
    result = Sweep()
    points = mount_points() if points is None else points
    cutoff = since - CTIME_SLACK_S
    for mount in mounts:
        try:
            status = os.lstat(mount.path)
            same = (status.st_dev, status.st_ino) == mount.identity
        except OSError:
            same = False
        if not same:
            result.fail(f"declared path {_shown(mount.path)} was replaced or removed")
        elif mount.writable and status.st_ctime >= cutoff:
            _check(result, mount.path, status, mounts)  # the mount itself: its bits
    writable = [mount.path for mount in mounts if mount.writable]
    tops = [path for path in writable if not any(p != path and within(path, p) for p in writable)]
    walked = new = 0
    for top in tops:
        try:
            status = os.lstat(top)
        except OSError:
            continue  # reported above
        if not stat.S_ISDIR(status.st_mode):
            continue  # a file: checked above
        device, stack = status.st_dev, [top]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as error:
                result.fail(f"could not list {_shown(directory)} ({error.strerror})")
                continue
            for entry in entries:
                walked += 1
                if walked > WALK_LIMIT:
                    result.fail(f"more than {WALK_LIMIT} entries: not every one was checked")
                    return _report(plugin, result)
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError as error:
                    result.fail(f"could not check {_shown(entry.path)} ({error.strerror})")
                    continue
                if status.st_dev != device:
                    if entry.path not in points:
                        result.fail(f"{_shown(entry.path)} is on another device (not a mount)")
                    continue  # a host mount point: not carried into the container
                if stat.S_ISDIR(status.st_mode):
                    stack.append(entry.path)
                if status.st_ctime < cutoff or not writable_at(entry.path, mounts):
                    continue  # older than the run, or in a read-only child: not its own
                new += 1
                if new > NEW_LIMIT:
                    result.fail(f"more than {NEW_LIMIT} new entries: not every one was checked")
                    return _report(plugin, result)
                _check(result, entry.path, status, mounts)
    return _report(plugin, result)


def _check(result: Sweep, path: str, status: os.stat_result, mounts: Sequence[Mount]) -> None:
    try:
        done = _fix(path, status, mounts)
    except OSError as error:
        result.fail(f"could not undo {_shown(path)} ({error.strerror})")
        return
    if done is not None:
        result.findings.append(done)


def _report(plugin: str, result: Sweep) -> Sweep:
    findings = result.findings
    for finding in findings[:REPORTED]:
        _log.warning("plugin %s (runtime container): %s", plugin, finding)
    if len(findings) > REPORTED:
        more = len(findings) - REPORTED
        _log.warning("plugin %s (runtime container): %d more such findings", plugin, more)
    return result


def now() -> float:
    """The wall clock `sweep` compares `st_ctime` with."""
    return time.time()


__all__ = [
    "CONTAINER_ONLY",
    "SYSTEM_PATHS",
    "UNSAFE_MOUNT",
    "Mount",
    "MountPolicy",
    "Sweep",
    "check_path",
    "check_quarantine",
    "mounts_problem",
    "plan_mounts",
    "quarantine",
    "recheck",
    "sweep",
    "within",
]
