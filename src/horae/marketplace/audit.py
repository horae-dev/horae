# SPDX-License-Identifier: Apache-2.0
"""What `horae plugin upgrade`, `remove` and `audit` read from disk (#47, schemas decision 73).

- `read_record(root)`: the install record `<root>/.horae-install.json` (decision 62 F): a
  regular file (never through a symlink) of at most 64 KiB holding a JSON object, or `None`
  when there is none (a built-in plugin, or one copied by hand). `RecordInvalid` otherwise.
- `pipeline_drift(plugin, approved, directory)`: how `pipelines/<plugin>/` differs from the
  digests `horae.lock` approves: a missing file, a changed one, an entry nobody approved.
- `leftovers(plugins, pipelines)`: what an interrupted upgrade or remove left behind.
"""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horae.marketplace.install import INSTALL_RECORD, STAGED_PREFIX
from horae.marketplace.pipelines import MAX_PIPELINE_BYTES, OLD_PREFIX, SUFFIX, digest
from horae.marketplace.upgrade import OLD_PREFIX as OLD_TREE_PREFIX
from horae.marketplace.upgrade import REMOVED_PREFIX

MAX_RECORD_BYTES = 64 * 1024
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_RECORD_KEYS = ("name", "version", "repo", "commit")


class RecordInvalid(Exception):
    """The install record exists but cannot be used (the message says why)."""


@dataclass(frozen=True)
class Drift:
    """One difference between `horae.lock` and what is installed."""

    kind: str
    message: str

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}


def runtime_drift(name: str, first_party: bool, runtime: str) -> Drift | None:
    """`runtime_not_container` (#191, decision 79 F) for a plugin that is not first-party whose
    effective runtime is not `container`: the plugin host refuses to start it. Shared by
    `horae plugin audit` and `horae doctor`; `None` when the plugin is fine."""
    if first_party or runtime == "container":
        return None
    return Drift(
        "runtime_not_container",
        f"{name}: not first-party, but horae.yaml runs it with `runtime: {runtime}`; the "
        f"plugin host refuses to start it: set `plugins.{name}.runtime: container`",
    )


def _read_small(path: Path, limit: int) -> bytes | None:
    """The bytes of the regular file `path` (opened without following a symlink), `None` when
    it is absent; `OSError` when it is a symlink, not a regular file, or larger than `limit`."""
    try:
        fd = os.open(path, _FILE_FLAGS)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError(f"{path.name} is not a regular file of at most {limit} bytes")
        return handle.read(limit + 1)


def read_record(root: Path) -> dict[str, Any] | None:
    """The install record of the plugin installed in `root`, or `None` when it has none."""
    try:
        raw = _read_small(root / INSTALL_RECORD, MAX_RECORD_BYTES)
    except OSError as exc:
        raise RecordInvalid(f"{INSTALL_RECORD} cannot be read: {exc.strerror or exc}") from None
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        raise RecordInvalid(f"{INSTALL_RECORD} is not valid JSON") from None
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), str) for key in _RECORD_KEYS
    ):
        raise RecordInvalid(f"{INSTALL_RECORD} is not an install record")
    return record


def record_mismatch(record: dict[str, Any], name: str, version: str, repo: str, commit: str) -> str:
    """What differs between `record` and the lock entry (`""` when nothing does)."""
    expected = {"name": name, "version": version, "repo": repo, "commit": commit}
    differ = [key for key in _RECORD_KEYS if record.get(key) != expected[key]]
    return ", ".join(
        f"{key} {record.get(key)!r} installed, {expected[key]!r} locked" for key in differ
    )


def pipeline_drift(plugin: str, approved: dict[str, str], directory: Path) -> list[Drift]:
    """How `directory` (`pipelines/<plugin>/`) differs from the `approved` digests."""
    found: list[Drift] = []
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return [
            Drift("pipeline_missing", f"{plugin}: pipeline {name} is approved but missing")
            for name in sorted(approved)
        ]
    except OSError:
        return [Drift("pipeline_changed", f"{plugin}: {directory} is not a real directory")]
    try:
        names = set(os.listdir(fd))
    except OSError:
        names = set()
    finally:
        os.close(fd)
    for name in sorted(approved):
        file = directory / f"{name}{SUFFIX}"
        try:
            raw = _read_small(file, MAX_PIPELINE_BYTES)
        except OSError:
            found.append(Drift("pipeline_changed", f"{plugin}: {file} is not a regular file"))
            continue
        if raw is None:
            found.append(Drift("pipeline_missing", f"{plugin}: {file} is missing"))
        elif digest(raw) != approved[name]:
            found.append(
                Drift("pipeline_changed", f"{plugin}: {file} differs from the approved digest")
            )
    expected = {f"{name}{SUFFIX}" for name in approved}
    found.extend(
        Drift("pipeline_unapproved", f"{plugin}: {directory / entry} is not approved by horae.lock")
        for entry in sorted(names - expected)
    )
    return found


def leftovers(plugins: Path, pipelines: Path) -> list[Drift]:
    """Directories an interrupted upgrade or remove left in `plugins/` and `pipelines/`."""
    found: list[Drift] = []
    for directory, prefixes in (
        (plugins, (OLD_TREE_PREFIX, REMOVED_PREFIX, STAGED_PREFIX)),
        (pipelines, (OLD_PREFIX,)),
    ):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        found.extend(
            Drift(
                "leftover",
                f"{entry} was left by an interrupted upgrade or remove; check it, then move it "
                "back or delete it",
            )
            for entry in entries
            if entry.name.startswith(prefixes)
        )
    return found


__all__ = [
    "Drift",
    "RecordInvalid",
    "leftovers",
    "pipeline_drift",
    "read_record",
    "record_mismatch",
    "runtime_drift",
]
