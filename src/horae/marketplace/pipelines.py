# SPDX-License-Identifier: Apache-2.0
"""Install a pipeline pack's pipelines into the instance (#133, schemas decision 72).

A plugin whose manifest lists `provides.pipelines` is a pipeline pack: its tree holds one
`pipelines/<name>.yaml` per listed pipeline. `horae plugin add` copies them to
`<instance>/pipelines/<plugin>/<name>.yaml`, a directory per pack that the instance loader
reads like `<instance>/pipelines/*.yaml` (`horae.config.loader`), so that removing or
upgrading a pack (#47) deletes exactly one directory. The paths are recorded in the plugin's
install record (`plugins/<plugin>/.horae-install.json`, key `pipelines`, relative to the
instance).

The pack's tree is untrusted, so before anything is written to the instance (this is a
`Prepare` hook, run on the verified tree) every file is checked:

- only `pipelines/<name>.yaml`, a regular file that is neither a symlink nor a directory,
  inside the tree (a path resolving outside it is refused);
- the set of files is exactly `provides.pipelines`, and the pipeline's `name` is the file's;
- it validates as a `Pipeline` (`horae.contracts.pipeline`) with the same safe YAML reader as
  the instance loader;
- no other pipeline of the instance has that name, and `pipelines/<plugin>/` does not exist:
  a user's file is never replaced. (The plugin is not installed at this point, so whatever is
  there is not ours; `remove` (#47) deletes the recorded files before a reinstall.)

The files are written to a scratch directory in `pipelines/` and renamed into place without
replacing anything (`rename_noreplace`: `renameat2(RENAME_NOREPLACE)`, or an exclusive `mkdir`
elsewhere; a directory that appears at the target meanwhile, even an empty one, is refused, #173),
so the pack's directory is absent or complete. A failure later in the add (the lock write)
removes it.

**Upgrade and remove (#47, decision 73).** `PackPipelines(instance, replacing=<the locked
digests>)` is the upgrade's hook: `pipelines/<plugin>/` may then exist, but only as a real
directory holding nothing but the old version's own files, each a regular file named like a
pipeline the lock approves for this plugin and still with its locked digest (an edited or
foreign file refuses the upgrade: it is never deleted). `write()` then moves the old directory
aside (`pipelines/.upgrade-old-*`), renames the new one in, and `remove()` puts it back;
`commit()` deletes the old copy. `delete_own(...)` is what `remove` and `commit` delete with:
only `<name>.yaml` regular files approved for the plugin with their locked digest, through
`lstat` (never a symlink, never following one), then the directory only if it is empty.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from horae.config.documents import ProblemsError, parse_yaml, validate
from horae.contracts.pipeline import LlmStep, Pipeline, call_parts
from horae.contracts.plugin import PluginManifest
from horae.contracts.types import PipelineName
from horae.marketplace.errors import InstallRefused
from horae.marketplace.install import INSTALL_RECORD

PIPELINES_DIR = "pipelines"
SUFFIX = ".yaml"
MAX_PIPELINE_BYTES = 1024 * 1024
SCRATCH_PREFIX = ".install-"
OLD_PREFIX = ".upgrade-old-"
"""An upgraded pack's previous directory, deleted once the upgrade is committed."""
_SUFFIXES = (".yaml", ".yml")
_name: TypeAdapter[str] = TypeAdapter(PipelineName)


def pack_path(plugin: str, pipeline: str) -> str:
    """Where a pack's pipeline lives, relative to the instance."""
    return f"{PIPELINES_DIR}/{plugin}/{pipeline}{SUFFIX}"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallRefused(f"cannot read {path.name}: {exc.strerror}") from None


def _read(plugin: str, tree: Path, name: str) -> bytes:
    """The bytes of `pipelines/<name>.yaml` of the tree, once it is known to be a plain file."""
    relative = f"{PIPELINES_DIR}/{name}{SUFFIX}"
    path = tree / PIPELINES_DIR / f"{name}{SUFFIX}"
    info = _lstat(path)
    if info is None:
        raise InstallRefused(f"{plugin}: provides pipeline {name!r} but has no {relative}")
    if not stat.S_ISREG(info.st_mode):
        raise InstallRefused(f"{plugin}: {relative} is not a regular file (no symlink)")
    if info.st_size > MAX_PIPELINE_BYTES:
        raise InstallRefused(f"{plugin}: {relative} is larger than {MAX_PIPELINE_BYTES} bytes")
    try:
        if not path.resolve(strict=True).is_relative_to(tree.resolve(strict=True)):
            raise InstallRefused(f"{plugin}: {relative} resolves outside the plugin")
        raw = path.read_bytes()
    except OSError as exc:
        raise InstallRefused(f"{plugin}: cannot read {relative}: {exc.strerror}") from None
    return raw


def _validated(plugin: str, name: str, raw: bytes) -> Pipeline:
    relative = f"{PIPELINES_DIR}/{name}{SUFFIX}"
    try:
        doc = parse_yaml(raw.decode("utf-8"), Path(relative))
        pipeline = validate(doc, Pipeline)
    except UnicodeDecodeError:
        raise InstallRefused(f"{plugin}: {relative} is not valid UTF-8") from None
    except ProblemsError as exc:
        shown = "; ".join(str(p) for p in exc.problems[:3])
        raise InstallRefused(f"{plugin}: {relative} is not a valid pipeline: {shown}") from None
    if pipeline.name != name:
        raise InstallRefused(
            f"{plugin}: {relative} defines pipeline {pipeline.name!r}, not {name!r}"
        )
    return pipeline


def digest(raw: bytes) -> str:
    """The digest the lock records for a pipeline file."""
    return hashlib.sha256(raw).hexdigest()


def tools_and_skills(pipeline: Pipeline) -> tuple[list[str], list[str]]:
    """The tool refs and the skills a pipeline calls, sorted, without duplicates."""
    tools: set[str] = set()
    skills: set[str] = set()
    for step in pipeline.steps:
        if isinstance(step, LlmStep):
            skills.add(step.llm.skill)
            continue
        calls = getattr(step, next(iter(type(step).model_fields)))
        tools.update(call_parts(call)[0] for call in calls)
    return sorted(tools), sorted(skills)


MAX_PARAMS_SHOWN = 160


def _calls(pipeline: Pipeline) -> list[str]:
    """One line per step element, with parameters: `act obsidian.write_note {...}`; an `llm`
    step shows its skill, model and budget."""
    lines: list[str] = []
    for step in pipeline.steps:
        if isinstance(step, LlmStep):
            spec = step.llm
            lines.append(
                f"llm {spec.skill} model={spec.model} "
                f"budget=${spec.budget.usd}/{spec.budget.tokens_out} tokens out"
            )
            continue
        kind = next(iter(type(step).model_fields))
        for call in getattr(step, kind):
            ref, params, _ = call_parts(call)
            shown = json.dumps(params, sort_keys=True, default=str, ensure_ascii=True)
            if len(shown) > MAX_PARAMS_SHOWN:
                shown = shown[: MAX_PARAMS_SHOWN - 3] + "..."
            lines.append(f"{kind} {ref}" + (f" {shown}" if params else ""))
    return lines


def _summary(plugin: str, pipeline: Pipeline) -> list[str]:
    trigger = pipeline.trigger
    if trigger is None:
        when = "manual (horae run) or external trigger only"
    else:
        zone = f" ({trigger.timezone})" if trigger.timezone else ""
        when = f"scheduled, cron {trigger.cron}{zone}"
    tools, skills = tools_and_skills(pipeline)
    lines = [f"{pipeline.name}: {when}"]
    lines.append(f"    tools: {', '.join(tools) or 'none'}")
    if skills:
        lines.append(f"    skills: {', '.join(skills)}")
    lines.append("    steps:")
    lines.extend(f"      {call}" for call in _calls(pipeline))
    foreign = sorted({t.split(".", 1)[0] for t in tools} - {plugin})
    foreign += sorted({s.split("/", 1)[0] for s in skills} - {plugin} - set(foreign))
    if foreign:
        lines.append(
            f"    warning: calls plugins other than {plugin} ({', '.join(foreign)}); "
            "they run with those plugins' own approved permissions"
        )
    return lines


def _stems(directory: Path) -> set[str]:
    try:
        return {p.stem for p in directory.iterdir() if p.suffix in _SUFFIXES}
    except OSError:
        return set()


def _taken(pipelines: Path, own: str | None = None) -> set[str]:
    """Names of the pipelines already in the instance (top level and pack directories), but
    those of the pack directory `own` (an upgrade replaces them)."""
    names = _stems(pipelines)
    try:
        for entry in pipelines.iterdir():
            info = _lstat(entry)
            if entry.name == own:
                continue
            if info is not None and stat.S_ISDIR(info.st_mode) and not entry.name.startswith("."):
                names |= _stems(entry)
    except OSError:
        pass
    return names


class PackPipelines:
    """The `Prepare` hook that checks a pack's pipelines, plus `write` and `remove`.

    `__call__` runs on the verified tree (nothing is written to the instance); `write` runs
    once the tree is in place; `remove` undoes `write`."""

    def __init__(
        self, instance: Path, replacing: dict[str, str] | None = None, *, upgrade: bool = False
    ) -> None:
        self.instance = instance
        self.replacing = replacing
        """An upgrade of a pack: the pipelines the lock approves for the installed version
        (name -> digest); `pipelines/<plugin>/` may hold exactly those."""
        self.upgrade = upgrade or replacing is not None
        """An upgrade: a version that installs no pipeline leaves `pipelines/<plugin>/`
        alone (an add keeps refusing it, decision 72 D)."""
        self.plugin: str | None = None
        self.files: dict[str, bytes] = {}
        self.parsed: dict[str, Pipeline] = {}
        self.written: Path | None = None
        self.target: Path | None = None
        """`pipelines/<plugin>/`, once checked."""
        self.old: Path | None = None
        """An upgrade: the old directory, moved aside by `write`."""

    @property
    def paths(self) -> list[str]:
        """The files to install, relative to the instance."""
        if self.plugin is None:
            return []
        return [pack_path(self.plugin, name) for name in sorted(self.files)]

    @property
    def digests(self) -> dict[str, str]:
        """Pipeline name -> digest of its file, for the lock entry."""
        return {name: digest(raw) for name, raw in sorted(self.files.items())}

    def approval_lines(self) -> list[str]:
        """What the approval prompt shows for each pipeline: name, trigger, tools, skills."""
        lines: list[str] = []
        for name in sorted(self.parsed):
            lines.extend(_summary(self.plugin or "", self.parsed[name]))
        return lines

    def __call__(self, tree: Path, manifest: PluginManifest) -> None:
        plugin = manifest.name
        if plugin == PIPELINES_DIR:
            raise InstallRefused(
                f"{plugin!r} is a reserved plugin name (the instance's pipelines/)"
            )
        declared = list(manifest.provides.pipelines)
        found = self._listing(plugin, tree)
        if set(declared) != set(found):
            raise InstallRefused(
                f"{plugin}: pipelines/ holds {sorted(found) or 'nothing'} but plugin.yaml "
                f"provides {sorted(declared) or 'no pipeline'}: they must be the same"
            )
        files = {name: _read(plugin, tree, name) for name in declared}
        parsed = {name: _validated(plugin, name, raw) for name, raw in files.items()}
        pipelines = self.instance / PIPELINES_DIR
        target = pipelines / plugin
        self.target = target
        if self.replacing is not None:
            check_own(plugin, target, self.replacing)
        elif _lstat(target) is not None and (files or not self.upgrade):
            raise InstallRefused(
                f"{plugin}: {target} already exists and is not installed by this pack; "
                "refusing to overwrite it (move it away, or remove the pack first)"
            )
        own = plugin if self.replacing is not None else None
        clash = sorted(set(files) & _taken(pipelines, own))
        if clash:
            raise InstallRefused(
                f"{plugin}: the instance already defines pipeline(s) {', '.join(clash)}; "
                "refusing to overwrite or shadow them (rename or remove yours first)"
            )
        if not files:
            return  # not a pipeline pack: nothing to install
        self.plugin, self.files, self.parsed = plugin, files, parsed
        self._record(tree)

    def _listing(self, plugin: str, tree: Path) -> set[str]:
        """The pipeline names of `<tree>/pipelines/`: plain `<name>.yaml` entries only."""
        directory = tree / PIPELINES_DIR
        info = _lstat(directory)
        if info is None:
            return set()
        if not stat.S_ISDIR(info.st_mode):
            raise InstallRefused(f"{plugin}: pipelines is not a directory")
        names: set[str] = set()
        for entry in sorted(directory.iterdir()):
            stem = entry.name.removesuffix(SUFFIX)
            kind = _lstat(entry)
            if entry.name.endswith(SUFFIX) and kind is not None and stat.S_ISREG(kind.st_mode):
                try:
                    _name.validate_python(stem)
                except ValueError:
                    raise InstallRefused(
                        f"{plugin}: pipelines/{entry.name} is not a valid pipeline file name"
                    ) from None
                names.add(stem)
            elif entry.name.endswith(_SUFFIXES) or (kind and not stat.S_ISREG(kind.st_mode)):
                raise InstallRefused(
                    f"{plugin}: pipelines/{entry.name} is not a plain pipelines/<name>.yaml file "
                    "(no symlink, directory or .yml)"
                )
        return names

    def _record(self, tree: Path) -> None:
        """Add the installed paths to the install record of the tree."""
        path = tree / INSTALL_RECORD
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pipelines"] = self.paths
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def write(self) -> None:
        """Create `pipelines/<plugin>/` with the checked files, atomically. In an upgrade the
        old directory, if any, is first moved aside (`OLD_PREFIX`); `remove` undoes both."""
        target = self.target
        if target is None or (self.plugin is None and self.replacing is None):
            return
        pipelines = target.parent
        try:
            if self.replacing is not None and os.path.lexists(target):
                aside = pipelines / f"{OLD_PREFIX}{target.name}-{secrets.token_hex(8)}"
                rename_noreplace(target, aside)
                self.old = aside
            if self.plugin is None:
                return  # the new version installs no pipeline: the old ones are only removed
            pipelines.mkdir(parents=True, exist_ok=True)
            scratch = Path(tempfile.mkdtemp(dir=pipelines, prefix=SCRATCH_PREFIX))
            try:
                for name, raw in self.files.items():
                    (scratch / f"{name}{SUFFIX}").write_bytes(raw)
                try:
                    rename_noreplace(scratch, target)
                except FileExistsError:
                    raise InstallRefused(
                        f"{self.plugin}: {target} appeared; not overwriting it"
                    ) from None
            except BaseException:
                shutil.rmtree(scratch, ignore_errors=True)
                raise
        except OSError as exc:
            self.remove()
            raise InstallRefused(
                f"{target.name}: cannot write pipelines to {target}: {exc.strerror or exc}"
            ) from None
        except BaseException:
            self.remove()
            raise
        self.written = target

    def remove(self) -> None:
        """Undo `write`: delete the directory this install created, and put an upgraded
        pack's old directory back."""
        if self.written is not None:
            shutil.rmtree(self.written, ignore_errors=True)
            self.written = None
        if self.old is not None and self.target is not None:
            try:
                rename_noreplace(self.old, self.target)
            except OSError:
                return  # left aside: `horae plugin audit` reports it
            self.old = None

    def commit(self) -> list[str]:
        """An upgrade is done: delete the old directory moved aside (its own files only).
        Returns what was kept (empty when everything was deleted)."""
        if self.old is None or self.replacing is None:
            return []
        kept = delete_own(self.old, self.replacing)
        self.old = None
        return kept


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _open_dir(directory: Path) -> int | None:
    """A descriptor of `directory` (never through a symlink), or `None` when it is absent;
    `OSError` when it is a symlink or not a directory."""
    try:
        return os.open(directory, _DIR_FLAGS)
    except FileNotFoundError:
        return None


_LINUX = sys.platform.startswith("linux")
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _renameat2() -> Any:
    """`renameat2(2)` from libc (Linux only), or `None`."""
    if not _LINUX:
        return None
    try:
        fn = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return fn


def rename_noreplace(src: Path, dst: Path) -> None:
    """Rename the directory `src` to `dst`, which must not exist: `FileExistsError` if it does,
    even when it is an empty directory created a moment ago (a plain `rename` replaces that).

    On Linux this is `renameat2(RENAME_NOREPLACE)`, atomic. Elsewhere (macOS, or a file
    system without it) `dst` is created with an exclusive `mkdir` and the entries of `src` are
    moved into it through descriptors of both directories, then `src` is removed: on a
    failure `dst` is deleted again and `src` is left to the caller."""
    fn = _renameat2()
    if fn is not None:
        if fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), str(dst))
        if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(err, os.strerror(err), str(dst))
    os.mkdir(dst)  # exclusive: EEXIST for a directory, a file or a symlink
    try:
        src_fd = os.open(src, _DIR_FLAGS)
        try:
            dst_fd = os.open(dst, _DIR_FLAGS)
            try:
                for name in os.listdir(src_fd):
                    os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)
        os.rmdir(src)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def check_own(plugin: str, directory: Path, approved: dict[str, str]) -> None:
    """`InstallRefused` unless `directory` is absent, or a real directory whose every entry is
    a regular `<name>.yaml` file with `name` in `approved` and the digest the lock approved."""
    try:
        fd = _open_dir(directory)
    except OSError:
        raise InstallRefused(
            f"{plugin}: {directory} is not a directory (or is a symlink): refusing"
        ) from None
    if fd is None:
        return
    try:
        for name in sorted(os.listdir(fd)):
            if not _is_own(fd, name, approved):
                raise InstallRefused(
                    f"{plugin}: {directory / name} was not installed by this pack, or was "
                    "changed after approval; move it away (or restore it) first: it is never "
                    "deleted"
                )
    except OSError as exc:
        raise InstallRefused(f"{plugin}: cannot read {directory}: {exc.strerror}") from None
    finally:
        os.close(fd)


def _is_own(dir_fd: int, name: str, approved: dict[str, str]) -> bool:
    """Whether entry `name` of the directory `dir_fd` is a regular file `<pipeline>.yaml` the
    lock approves, with its digest (opened with `O_NOFOLLOW`: a symlink is never one)."""
    pipeline = name.removesuffix(SUFFIX)
    if not name.endswith(SUFFIX) or pipeline not in approved:
        return False
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False  # a symlink (ELOOP), a vanished or unreadable entry
    with os.fdopen(fd, "rb") as handle:
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PIPELINE_BYTES:
                return False
            raw = handle.read(MAX_PIPELINE_BYTES + 1)
        except OSError:
            return False
    return digest(raw) == approved[pipeline]


def delete_own(directory: Path, approved: dict[str, str]) -> list[str]:
    """Delete from `directory` the files that are a pack's own (`_is_own`: approved name,
    locked digest, regular file), through a descriptor of the directory opened without
    following a symlink, then `directory` itself if it is left empty. Returns what was kept:
    every other entry, or the directory when it is not empty, not a real directory, or
    cannot be read."""
    try:
        fd = _open_dir(directory)
    except OSError:
        return [str(directory)]
    if fd is None:
        return []
    kept: list[str] = []
    try:
        names = sorted(os.listdir(fd))
        for name in names:
            if _is_own(fd, name, approved):
                try:
                    os.unlink(name, dir_fd=fd)
                    continue
                except OSError:
                    pass
            kept.append(str(directory / name))
    except OSError:
        return [str(directory)]
    finally:
        os.close(fd)
    try:
        directory.rmdir()  # never follows a symlink: a link is not a directory to rmdir
    except OSError:
        kept.append(str(directory))
    return kept
