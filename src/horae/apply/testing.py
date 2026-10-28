# SPDX-License-Identifier: Apache-2.0
"""An in-memory reference `Sink`, for tests and examples.

`FixtureSink` keeps notes (`files`, path to bytes) and tasks (`tasks`, fingerprint to a task
line) in memory, previews with the conventions of `horae.apply.notes`, and records every call
(`previews`, `applied`). `apply` changes its own memory only and records what it sent
(`sent`): it never touches the filesystem or the network. Set `fail` to make the preview (and
apply) of some intent indices raise a given `PreviewError`.
"""

from dataclasses import dataclass, field, replace

from horae.apply.local import describe
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


@dataclass
class FixtureSink:
    """An in-memory sink for every intent type."""

    files: dict[str, bytes] = field(default_factory=dict)
    tasks: dict[str, str] = field(default_factory=dict)
    fail: dict[int, PreviewError] = field(default_factory=dict)
    previews: list[SinkCall] = field(default_factory=list)
    applied: list[SinkCall] = field(default_factory=list)
    sent: list[Notification | HttpCall] = field(default_factory=list)

    def _preview(self, call: SinkCall) -> Preview:
        if call.index in self.fail:
            raise self.fail[call.index]
        intent = call.intent
        if isinstance(intent, NoteIntent):
            before = self.files.get(intent.path)
            return FileChange(intent.path, before, note_after(intent, before))
        described = describe(intent)
        if isinstance(described, TaskChange):
            return replace(described, before=self.tasks.get(described.fingerprint))
        if described is None:  # pragma: no cover - the closed set
            raise PreviewError("not_supported", intent.type)
        return described

    async def preview(self, call: SinkCall) -> Preview:
        self.previews.append(call)
        return self._preview(call)

    async def apply(self, call: SinkCall) -> Applied:
        self.applied.append(call)
        change = self._preview(call)
        if isinstance(change, FileChange):
            self.files[change.path] = change.after
            return Applied(change.path, change.before, change.after)
        if isinstance(change, TaskChange):
            self.tasks[change.fingerprint] = change.after
            return Applied(change.fingerprint)
        self.sent.append(change)
        return Applied(change.url if isinstance(change, HttpCall) else call.tool)
