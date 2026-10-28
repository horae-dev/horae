# SPDX-License-Identifier: Apache-2.0
"""An in-memory reference `Sink`, for tests and examples.

`FixtureSink` keeps notes (`files`, path to bytes) and tasks (`tasks`, fingerprint to a task
line) in memory and records every call (`reads`, `applied`). `read` returns a note's bytes.
`apply` is a compare-and-swap on a note (`target_changed` when it is not as expected) and
applies the core's conventions (`horae.apply.notes`); it changes its own memory only and
records what it sent (`sent`, with each notification's `dedupe_key` in `dedupe_keys`): it
never touches the filesystem or the network. Set `fail` to make the read (and apply) of some
intent indices raise a given `SinkError`. It is a `RestorableSink` too: `read_target` and
`restore` (a compare-and-swap on `files`, recorded in `restored`) serve `horae undo` (#22).
Its `identity` defaults to `"fixture"` (two fixtures with the same identity count as the same
place); `parent_exists` is `False` for a target whose directory is in `missing_dirs`.
"""

import hashlib
from dataclasses import dataclass, field

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
    identity: str | None = "fixture"
    missing_dirs: set[str] = field(default_factory=set)

    async def parent_exists(self, target: str) -> bool:
        return target.rpartition("/")[0] not in self.missing_dirs

    async def read_target(self, target: str) -> bytes | None:
        return self.files.get(target)

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        current = self.files.get(target)
        found = None if current is None else hashlib.sha256(current).hexdigest()
        if found != expected_sha256:
            raise SinkError("target_changed", f"{target} changed since it was applied")
        self.restored.append((target, content))
        if content is None:
            self.files.pop(target, None)
        else:
            self.files[target] = content

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
            return Applied(change.fingerprint)
        assert change is not None  # noqa: S101 - notes are handled above
        self.sent.append(change)
        if isinstance(change, HttpCall):
            return Applied(change.url)
        self.dedupe_keys.append(call.dedupe_key)
        return Applied(call.tool)
