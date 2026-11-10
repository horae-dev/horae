# SPDX-License-Identifier: Apache-2.0
"""The permission diff of `horae plugin upgrade` and the swap of an installed tree (#47,
schemas decision 73).

- `permission_diff(old, new)`: what changes between the `horae.lock` entry that is approved
  (`old`) and the one the upgrade asks for (`new`), item by item, each marked `added`,
  `removed` or `changed` and whether it **widens** what the plugin may do. Widening: a new
  host, local endpoint, secret, intent or path (a path moved from read to write is a new write
  path); a new or changed pack pipeline (a changed digest is new code the core runs); a
  personal field dropped (less protection, decision 68); a new repository or source. Not
  widening: anything removed, a secret moved between required and optional.
- `user_grants(locked)`: the hosts and local endpoints the user recorded in the lock entry
  with `--allow-host` / `--allow-local`; an upgrade keeps them (they are already approved).
- `Swap`: the renames that put an upgrade in place (old tree aside, new tree in), undone in
  reverse order on failure, so the old tree is left exactly where it was.
"""

import os
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from horae.contracts.lockfile import LockedPlugin
from horae.contracts.plugin import FREE_TEXT_FIELD, Permissions

OLD_PREFIX = ".upgrade-old-"
"""In `plugins/`: the tree an upgrade replaced, deleted once the upgrade is committed. A killed
upgrade may leave it as the only copy, so no install sweeps it (`horae plugin audit` reports
it)."""
REMOVED_PREFIX = ".remove-"
"""In `plugins/`: a tree `horae plugin remove` moved aside before deleting it."""

ChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True)
class Change:
    """One item of a permission diff."""

    field: str
    """`repo`, `source`, `network`, `network_local`, `secrets`, `intents`, `paths.read`,
    `paths.write`, `personal_fields` or `pipelines`."""
    item: str
    change: ChangeKind
    widens: bool

    def as_json(self) -> dict[str, object]:
        return {
            "field": self.field,
            "item": self.item,
            "change": self.change,
            "widens": self.widens,
        }


def _sets(field: str, old: Sequence[str], new: Sequence[str]) -> list[Change]:
    before, after = set(old), set(new)
    return [
        *(Change(field, item, "added", True) for item in sorted(after - before)),
        *(Change(field, item, "removed", False) for item in sorted(before - after)),
    ]


def _secrets(old: Permissions, new: Permissions) -> list[Change]:
    changes = _sets("secrets", old.all_secrets, new.all_secrets)
    for name in sorted(set(old.all_secrets) & set(new.all_secrets)):
        was, now = name in old.secrets, name in new.secrets
        if was != now:
            how = "now required" if now else "now optional"
            changes.append(Change("secrets", f"{name} ({how})", "changed", False))
    return changes


def _personal_item(ref: str, path: str) -> str:
    """One personal field as the diff shows it: a scan-only one (`free_text:`, #165) says it
    is not de-identified as a value, so turning a field scan-only reads as a field dropped."""
    if path.startswith(FREE_TEXT_FIELD):
        bare = path.removeprefix(FREE_TEXT_FIELD)
        return f"{ref}: {bare} (scan-only free text, not de-identified as a value)"
    return f"{ref}: {path}"


def _personal(old: dict[str, list[str]], new: dict[str, list[str]]) -> list[Change]:
    """A personal field dropped lowers the protection: it widens (decision 68)."""
    before = {_personal_item(ref, path) for ref, paths in old.items() for path in paths}
    after = {_personal_item(ref, path) for ref, paths in new.items() for path in paths}
    return [
        *(Change("personal_fields", item, "added", False) for item in sorted(after - before)),
        *(Change("personal_fields", item, "removed", True) for item in sorted(before - after)),
    ]


def _pipelines(old: dict[str, str], new: dict[str, str]) -> list[Change]:
    changes = _sets("pipelines", list(old), list(new))
    changes += [
        Change("pipelines", name, "changed", True)
        for name in sorted(set(old) & set(new))
        if old[name] != new[name]
    ]
    return changes


def permission_diff(old: LockedPlugin, new: LockedPlugin) -> list[Change]:
    """Every difference between the approved entry `old` and the requested one `new`, but the
    version and commit (shown apart)."""
    changes: list[Change] = []
    if old.repo != new.repo:
        changes.append(Change("repo", f"{old.repo} -> {new.repo}", "changed", True))
    if old.source != new.source:
        before, after = old.source or "marketplace", new.source or "marketplace"
        changes.append(Change("source", f"{before} -> {after}", "changed", True))
    have, want = old.permissions, new.permissions
    changes += _sets("network", have.network, want.network)
    changes += _sets("network_local", have.network_local, want.network_local)
    changes += _secrets(have, want)
    changes += _sets("intents", have.intents, want.intents)
    changes += _sets("paths.read", have.paths.read, want.paths.read)
    changes += _sets("paths.write", have.paths.write, want.paths.write)
    changes += _personal(old.personal_fields, new.personal_fields)
    changes += _pipelines(old.pipelines, new.pipelines)
    return changes


def widens(changes: Sequence[Change]) -> bool:
    return any(change.widens for change in changes)


_LABELS = {
    "repo": "repository",
    "source": "source",
    "network": "network (https hosts)",
    "network_local": "local network (plain http allowed)",
    "secrets": "secrets",
    "intents": "intents",
    "paths.read": "paths (read)",
    "paths.write": "paths (read and write)",
    "personal_fields": "personal fields",
    "pipelines": "pipelines",
}
_MARKS: dict[ChangeKind, str] = {"added": "+", "removed": "-", "changed": "~"}


def describe_diff(changes: Sequence[Change]) -> list[str]:
    """The diff as text, one line per item, `+` added, `-` removed, `~` changed, and
    `(needs approval)` on what widens."""
    if not changes:
        return ["  permissions: unchanged"]
    lines: list[str] = []
    for field in _LABELS:
        items = [change for change in changes if change.field == field]
        if not items:
            continue
        lines.append(f"  {_LABELS[field]}:")
        lines.extend(
            f"    {_MARKS[c.change]} {c.item}{'  (new: needs approval)' if c.widens else ''}"
            for c in items
        )
    return lines


def user_grants(locked: LockedPlugin) -> tuple[list[str], list[str]]:
    """The hosts and local endpoints the user recorded when `locked` was approved
    (`LockedPlugin.user_grants`), still granted by it. Nothing for an entry without a record:
    what the user added is never inferred from the installed manifest, which is not verified."""
    if locked.user_grants is None:
        return [], []
    return (
        sorted(set(locked.user_grants.network) & set(locked.permissions.network)),
        sorted(set(locked.user_grants.network_local) & set(locked.permissions.network_local)),
    )


class Swap:
    """Renames done in order and undone in reverse on `rollback`."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def move(self, source: Path, target: Path) -> None:
        """Rename `source` to `target` (which must not exist), remembered for `rollback`."""
        if os.path.lexists(target):
            raise FileExistsError(f"{target} exists")
        os.rename(source, target)
        self._undo.append(lambda: os.rename(target, source))

    def aside(self, path: Path, prefix: str) -> Path:
        """Rename `path` to a new hidden sibling named `<prefix><name>-<random>`."""
        target = path.parent / f"{prefix}{path.name}-{secrets.token_hex(8)}"
        self.move(path, target)
        return target

    def also_undo(self, undo: Callable[[], None]) -> None:
        """Run `undo` too on `rollback` (in reverse order with the renames)."""
        self._undo.append(undo)

    def rollback(self) -> list[str]:
        """Undo everything, newest first. Returns what could not be undone (messages)."""
        failed: list[str] = []
        while self._undo:
            undo = self._undo.pop()
            try:
                undo()
            except OSError as exc:
                failed.append(str(exc.strerror or exc))
        return failed


def delete_tree(path: Path) -> bool:
    """Delete a tree Horae installed (no symlink is followed: `shutil.rmtree` removes a link
    itself, never what it points to). Whether it is gone."""
    if os.path.islink(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


__all__ = [
    "OLD_PREFIX",
    "REMOVED_PREFIX",
    "Change",
    "Swap",
    "delete_tree",
    "describe_diff",
    "permission_diff",
    "user_grants",
    "widens",
]
