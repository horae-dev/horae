# SPDX-License-Identifier: Apache-2.0
"""The apply layer: the core plans and sinks apply routed intents (ADR 0002, #21/#22).

`horae.apply.sink` defines the `Sink` port (`read` / `apply`; the plugin host, #24,
implements it over MCP), `horae.apply.notes` the core's note conventions, `horae.apply.plan`
the dry-run diff (`plan_changes`, which only ever calls `read`), `horae.apply.render` its
text and JSON forms, `horae.apply.local` a built-in read-only sink over a local vault and
`horae.apply.testing` an in-memory reference sink. `horae.apply.apply` journals and applies
a planned run (#22), `horae.apply.undo` reverts one, and `LocalFileSink` (provisional, until
#24) writes notes to a local vault.
"""

from horae.apply.apply import ApplyFailure, ApplyRefused, ApplyResult, apply_changes
from horae.apply.local import LocalFileSink, LocalSink
from horae.apply.plan import (
    MAX_DIFF_BYTES,
    ChangeSet,
    FileDiff,
    HttpItem,
    NotificationItem,
    Problem,
    Refusal,
    TaskItem,
    dedupe_key,
    describe,
    entry_params,
    plan_changes,
    unified_diff,
)
from horae.apply.render import JSON_SCHEMA_VERSION, render_text, to_dict, to_json
from horae.apply.sink import (
    MAX_IDENTITY,
    MAX_NOTE_BYTES,
    Applied,
    FileChange,
    HttpCall,
    Notification,
    Preview,
    RestorableSink,
    Sink,
    SinkCall,
    SinkError,
    SinkRegistry,
    StaticSinks,
    TaskChange,
    error_kind,
    sink_identity,
)
from horae.apply.undo import (
    NoteUndo,
    NotUndoable,
    UndoProblem,
    UndoRefusal,
    UndoResult,
    undo_run,
)

__all__ = [
    "JSON_SCHEMA_VERSION",
    "MAX_DIFF_BYTES",
    "MAX_IDENTITY",
    "MAX_NOTE_BYTES",
    "Applied",
    "ApplyFailure",
    "ApplyRefused",
    "ApplyResult",
    "ChangeSet",
    "FileChange",
    "FileDiff",
    "HttpCall",
    "HttpItem",
    "LocalFileSink",
    "LocalSink",
    "NotUndoable",
    "NoteUndo",
    "Notification",
    "NotificationItem",
    "Preview",
    "Problem",
    "Refusal",
    "RestorableSink",
    "Sink",
    "SinkCall",
    "SinkError",
    "SinkRegistry",
    "StaticSinks",
    "TaskChange",
    "TaskItem",
    "UndoProblem",
    "UndoRefusal",
    "UndoResult",
    "apply_changes",
    "dedupe_key",
    "describe",
    "entry_params",
    "error_kind",
    "plan_changes",
    "render_text",
    "sink_identity",
    "to_dict",
    "to_json",
    "undo_run",
    "unified_diff",
]
