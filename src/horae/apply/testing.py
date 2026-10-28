# SPDX-License-Identifier: Apache-2.0
"""An in-memory reference `Sink`, for tests and examples.

`FixtureSink` keeps notes (`files`, path to bytes) and tasks (`tasks`, fingerprint to a task
line) in memory and records every call (`reads`, `applied`). `read` returns a note's bytes.
`apply` is a compare-and-swap on a note (`target_changed` when it is not as expected) and
applies the core's conventions (`horae.apply.notes`); it changes its own memory only and
records what it sent (`sent`, with each notification's `dedupe_key` in `dedupe_keys`): it
never touches the filesystem or the network. Set `fail` to make the read (and apply) of some
intent indices raise a given `SinkError`.
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
