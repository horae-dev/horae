# SPDX-License-Identifier: Apache-2.0
"""The apply layer: the core plans and sinks apply routed intents (ADR 0002, #21/#22).

`horae.apply.sink` defines the `Sink` port (`read` / `apply`; the plugin host, #24,
implements it over MCP), `horae.apply.notes` the core's note conventions, `horae.apply.plan`
the dry-run diff (`plan_changes`, which only ever calls `read`), `horae.apply.render` its
text and JSON forms, `horae.apply.local` a built-in read-only sink over a local vault and
`horae.apply.testing` an in-memory reference sink.
"""

from horae.apply.local import LocalSink
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
    MAX_NOTE_BYTES,
    Applied,
    FileChange,
    HttpCall,
    Notification,
    Preview,
    Sink,
    SinkCall,
    SinkError,
    SinkRegistry,
    StaticSinks,
    TaskChange,
)

__all__ = [
    "JSON_SCHEMA_VERSION",
    "MAX_DIFF_BYTES",
    "MAX_NOTE_BYTES",
    "Applied",
    "ChangeSet",
    "FileChange",
    "FileDiff",
    "HttpCall",
    "HttpItem",
    "LocalSink",
    "Notification",
    "NotificationItem",
    "Preview",
    "Problem",
    "Refusal",
    "Sink",
    "SinkCall",
    "SinkError",
    "SinkRegistry",
    "StaticSinks",
    "TaskChange",
    "TaskItem",
    "dedupe_key",
    "describe",
    "entry_params",
    "plan_changes",
    "render_text",
    "to_dict",
    "to_json",
    "unified_diff",
]
