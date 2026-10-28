# SPDX-License-Identifier: Apache-2.0
"""A built-in, **read-only** sink for previews, until the plugin host (#24) provides real ones.

`LocalSink` previews every intent type without calling any plugin:

- `note.*` against a local vault directory (`horae.apply.notes` for the conventions): it reads
  the target note, nothing else. The path is resolved inside the vault (`outside_vault` if a
  symlink leads out, `not_a_file` for anything but a regular file), and notes over
  `MAX_READ_BYTES` are refused (`note_too_large`). Without a vault, `vault_unset`.
- `task.*`, `notify.push` and `http.post` are described from the intent alone (the current
  state of a task is not known here: `TaskChange.before` is `None`).

It never applies anything: `apply` raises `PreviewError("not_supported")`.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from horae.apply.notes import NoteIntent, note_after
from horae.apply.sink import (
    Applied,
    FileChange,
    HttpCall,
    Notification,
    Preview,
    PreviewError,
    SinkCall,
    TaskChange,
)
from horae.contracts.intents import (
    HttpPost,
    Intent,
    NotifyPush,
    TaskCreate,
    TaskUpdateStatus,
)

MAX_READ_BYTES = 16 * 1024 * 1024
"""The largest note `LocalSink` reads (16 MiB)."""


def describe(intent: Intent) -> Preview | None:
    """The preview of a task, notification or HTTP intent, from the intent alone; `None` for
    note intents."""
    if isinstance(intent, TaskCreate):
        line = f"- [ ] {intent.title}"
        if intent.due is not None:
            line += f" (due {intent.due}{'' if intent.time is None else ' ' + intent.time})"
        return TaskChange(intent.fingerprint, "create", None, line)
    if isinstance(intent, TaskUpdateStatus):
        after = intent.status if intent.until is None else f"{intent.status} until {intent.until}"
        return TaskChange(intent.task, "update_status", None, after)
    if isinstance(intent, NotifyPush):
        return Notification(intent.title, intent.body, intent.link)
    if isinstance(intent, HttpPost):
        return HttpCall(intent.url, intent.body)
    return None


def read_note(vault: Path, path: str) -> bytes | None:
    """The bytes of note `path` in `vault` (`None` if it does not exist), read-only."""
    root = os.path.realpath(vault)
    target = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath([root, target]) != root:
        raise PreviewError("outside_vault", f"{path} resolves outside the vault")
    try:
        info = os.stat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise PreviewError("not_a_file", f"{path} is not a regular file")
    if info.st_size > MAX_READ_BYTES:
        raise PreviewError("note_too_large", f"{path} is larger than {MAX_READ_BYTES} bytes")
    with open(target, "rb") as handle:
        data = handle.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise PreviewError("note_too_large", f"{path} is larger than {MAX_READ_BYTES} bytes")
    return data


@dataclass(frozen=True)
class LocalSink:
    """Read-only previews over a local vault directory (`None`: notes cannot be previewed)."""

    vault: Path | None = None

    async def preview(self, call: SinkCall) -> Preview:
        described = describe(call.intent)
        if described is not None:
            return described
        intent = call.intent
        if not isinstance(intent, NoteIntent):  # pragma: no cover - the closed set
            raise PreviewError("not_supported", f"cannot preview {intent.type}")
        if self.vault is None:
            raise PreviewError("vault_unset", "no vault directory to preview notes against")
        before = read_note(self.vault, intent.path)
        return FileChange(intent.path, before, note_after(intent, before))

    async def apply(self, call: SinkCall) -> Applied:
        raise PreviewError("not_supported", "the built-in local sink only previews")
