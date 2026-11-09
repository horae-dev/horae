# SPDX-License-Identifier: Apache-2.0
"""An in-memory reference `Sink`, for tests and examples.

`FixtureSink` keeps notes (`files`, path to bytes) and tasks (`tasks`, fingerprint to a task
line) in memory and records every call (`reads`, `applied`). `read` returns a note's bytes.
`apply` is a compare-and-swap on a note (`target_changed` when it is not as expected) and
applies the core's conventions (`horae.apply.notes`); it changes its own memory only and
records what it sent (`sent`, with each notification's `dedupe_key` in `dedupe_keys`): it
never touches the filesystem or the network. Set `fail` to make the read (and apply) of some
intent indices raise a given `SinkError`. It is a `RestorableSink` too: `read_target` and
`restore` (a compare-and-swap on `files`, recorded in `restored`) serve `horae undo` (#22);
with `status_details` it answers a `task.update_status` with details and serves
`restore_status` too (#139).
Its `identity` defaults to `"fixture"` (two fixtures with the same identity count as the same
place); `parent_exists` is `False` for a target whose directory is in `missing_dirs`.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from horae.apply.notes import NoteIntent, note_after
from horae.apply.plan import describe
from horae.apply.sink import Applied, HttpCall, Notification, SinkCall, SinkError, TaskChange


@dataclass
class FixtureSink:
    """An in-memory sink for every intent type."""

    files: dict[str, bytes] = field(default_factory=dict)
    tasks: dict[str, str] = field(default_factory=dict)
    fail: dict[int, SinkError] = field(default_factory=dict)
    reads: list[SinkCall] = field(default_factory=list)
    applied: list[SinkCall] = field(default_factory=list)
    sent: list[Notification | HttpCall] = field(default_factory=list)
    dedupe_keys: list[str | None] = field(default_factory=list)
    restored: list[tuple[str, bytes | None]] = field(default_factory=list)
    restore_forced: list[bool] = field(default_factory=list)
    """The `force` of every `restore` call."""
    task_status: dict[str, str] = field(default_factory=dict)
    """Fingerprint to the status of a task, kept when `restore_status` is served (#139)."""
    status_details: bool = False
    """Answer a `task.update_status` with the details `restore_status` needs (#139)."""
    status_restored: list[tuple[dict[str, Any], bool]] = field(default_factory=list)
    """Every `restore_status` call: the details and `force`."""
    status_changed: dict[str, bool] = field(default_factory=dict)
    """Fingerprints whose task was edited since its status change: `restore_status` refuses
    them (`target_changed`) unless forced."""
    identity: str | None = "fixture"
    missing_dirs: set[str] = field(default_factory=set)

    async def parent_exists(self, target: str) -> bool:
        return target.rpartition("/")[0] not in self.missing_dirs

    async def read_target(self, target: str) -> bytes | None:
        return self.files.get(target)

    async def restore(
        self,
        target: str,
        content: bytes | None,
        *,
        expected_sha256: str | None,
        force: bool = False,
    ) -> None:
        self.restore_forced.append(force)
        current = self.files.get(target)
        found = None if current is None else hashlib.sha256(current).hexdigest()
        if found != expected_sha256:
            raise SinkError("target_changed", f"{target} changed since it was applied")
        self.restored.append((target, content))
        if content is None:
            self.files.pop(target, None)
        else:
            self.files[target] = content

    async def restore_status(self, details: dict[str, Any], *, force: bool = False) -> bool:
        """Undo a status change `apply` answered (`details["task"]`, `before_status`)."""
        self.status_restored.append((dict(details), force))
        task = details.get("task")
        if not isinstance(task, str) or not isinstance(details.get("before_status"), str):
            raise SinkError("not_restorable", "the change did not record what it replaced")
        if self.status_changed.get(task) and not force:
            raise SinkError("target_changed", f"task {task} changed since")
        if self.tasks_status_of(task) == details["before_status"]:
            return False
        self.task_status[task] = details["before_status"]
        return True

    def tasks_status_of(self, task: str) -> str | None:
        return self.task_status.get(task)

    async def read(self, call: SinkCall) -> bytes | None:
        self.reads.append(call)
        if call.index in self.fail:
            raise self.fail[call.index]
        intent = call.intent
        if not isinstance(intent, NoteIntent):
            raise SinkError("not_supported", f"no {intent.type} target to read")
        return self.files.get(intent.path)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        self.applied.append(call)
        if call.index in self.fail:
            raise self.fail[call.index]
        intent = call.intent
        if isinstance(intent, NoteIntent):
            before = self.files.get(intent.path)
            found = None if before is None else hashlib.sha256(before).hexdigest()
            if found != expected_before_sha256:
                raise SinkError("target_changed", f"{intent.path} changed since it was planned")
            after = note_after(intent, before)
            self.files[intent.path] = after
            return Applied(intent.path, before, after)
        change = describe(intent)
        if isinstance(change, TaskChange):
            self.tasks[change.fingerprint] = change.after
            if self.status_details and change.action == "update_status":
                # What a sink that can undo a status change answers (#139).
                fingerprint = change.fingerprint
                prior = self.task_status.get(fingerprint, "todo")
                self.task_status[fingerprint] = str(getattr(intent, "status", ""))
                details = {"task": fingerprint, "before_status": prior, "changed": True}
                return Applied(fingerprint, details=details)
            return Applied(change.fingerprint)
        assert change is not None  # noqa: S101 - notes are handled above
        self.sent.append(change)
        if isinstance(change, HttpCall):
            return Applied(change.url)
        self.dedupe_keys.append(call.dedupe_key)
        return Applied(call.tool)
