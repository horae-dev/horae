# SPDX-License-Identifier: Apache-2.0
"""Install a pipeline pack's pipelines into the instance (#133, schemas decision 71).

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

The files are written to a scratch directory in `pipelines/` and renamed into place, so the
pack's directory is absent or complete. A failure later in the add (the lock write) removes it.
"""

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from horae.config.documents import ProblemsError, parse_yaml, validate
from horae.contracts.pipeline import Pipeline
from horae.contracts.plugin import PluginManifest
from horae.contracts.types import PipelineName
from horae.marketplace.errors import InstallRefused
from horae.marketplace.install import INSTALL_RECORD

PIPELINES_DIR = "pipelines"
SUFFIX = ".yaml"
MAX_PIPELINE_BYTES = 1024 * 1024
SCRATCH_PREFIX = ".install-"
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


def _validated(plugin: str, name: str, raw: bytes) -> None:
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


def _stems(directory: Path) -> set[str]:
    try:
        return {p.stem for p in directory.iterdir() if p.suffix in _SUFFIXES}
    except OSError:
        return set()


def _taken(pipelines: Path) -> set[str]:
    """Names of the pipelines already in the instance (top level and pack directories)."""
    names = _stems(pipelines)
    try:
        for entry in pipelines.iterdir():
            info = _lstat(entry)
            if info is not None and stat.S_ISDIR(info.st_mode) and not entry.name.startswith("."):
                names |= _stems(entry)
    except OSError:
        pass
    return names


class PackPipelines:
    """The `Prepare` hook that checks a pack's pipelines, plus `write` and `remove`.

    `__call__` runs on the verified tree (nothing is written to the instance); `write` runs
    once the tree is in place; `remove` undoes `write`."""

    def __init__(self, instance: Path) -> None:
        self.instance = instance
        self.plugin: str | None = None
        self.files: dict[str, bytes] = {}
        self.written: Path | None = None

    @property
    def paths(self) -> list[str]:
        """The files to install, relative to the instance."""
        if self.plugin is None:
            return []
        return [pack_path(self.plugin, name) for name in sorted(self.files)]

    def __call__(self, tree: Path, manifest: PluginManifest) -> None:
        plugin = manifest.name
        declared = list(manifest.provides.pipelines)
        found = self._listing(plugin, tree)
        if set(declared) != set(found):
            raise InstallRefused(
                f"{plugin}: pipelines/ holds {sorted(found) or 'nothing'} but plugin.yaml "
                f"provides {sorted(declared) or 'no pipeline'}: they must be the same"
            )
        files = {name: _read(plugin, tree, name) for name in declared}
        for name, raw in files.items():
            _validated(plugin, name, raw)
        pipelines = self.instance / PIPELINES_DIR
        target = pipelines / plugin
        if _lstat(target) is not None:
            raise InstallRefused(
                f"{plugin}: {target} already exists and is not installed by this pack; "
                "refusing to overwrite it (move it away, or remove the pack first)"
            )
        clash = sorted(set(files) & _taken(pipelines))
        if clash:
            raise InstallRefused(
                f"{plugin}: the instance already defines pipeline(s) {', '.join(clash)}; "
                "refusing to overwrite or shadow them (rename or remove yours first)"
            )
        if not files:
            return  # not a pipeline pack: nothing to install
        self.plugin, self.files = plugin, files
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
        """Create `pipelines/<plugin>/` with the checked files, atomically."""
        if self.plugin is None:
            return
        pipelines = self.instance / PIPELINES_DIR
        target = pipelines / self.plugin
        try:
            pipelines.mkdir(parents=True, exist_ok=True)
            scratch = Path(tempfile.mkdtemp(dir=pipelines, prefix=SCRATCH_PREFIX))
            try:
                for name, raw in self.files.items():
                    (scratch / f"{name}{SUFFIX}").write_bytes(raw)
                if os.path.lexists(target):
                    raise InstallRefused(f"{self.plugin}: {target} appeared; not overwriting it")
                scratch.rename(target)
            except BaseException:
                shutil.rmtree(scratch, ignore_errors=True)
                raise
        except OSError as exc:
            raise InstallRefused(
                f"{self.plugin}: cannot write pipelines to {target}: {exc.strerror or exc}"
            ) from None
        self.written = target

    def remove(self) -> None:
        """Undo `write`: delete the directory this install created."""
        if self.written is not None:
            shutil.rmtree(self.written, ignore_errors=True)
            self.written = None
