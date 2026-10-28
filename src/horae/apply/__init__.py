# SPDX-License-Identifier: Apache-2.0
"""The apply layer: sinks preview and apply routed intents (ADR 0002, #21/#22).

`horae.apply.sink` defines the `Sink` port (the plugin host, #24, implements it over MCP),
`horae.apply.plan` the dry-run diff (`plan_changes`, which only ever calls `preview`),
`horae.apply.render` its text and JSON forms, `horae.apply.local` a built-in read-only preview
sink over a local vault and `horae.apply.testing` an in-memory reference sink.
"""

from horae.apply.local import MAX_READ_BYTES, LocalSink
from horae.apply.plan import (
    MAX_DIFF_BYTES,
    ChangeSet,
    FileDiff,
    HttpItem,
    NotificationItem,
    Problem,
    TaskItem,
    dedupe_key,
    plan_changes,
    unified_diff,
)
from horae.apply.render import JSON_SCHEMA_VERSION, render_text, to_dict, to_json
from horae.apply.sink import (
    Applied,
    FileChange,
    HttpCall,
    Notification,
    Preview,
    PreviewError,
    Sink,
    SinkCall,
    SinkRegistry,
    StaticSinks,
    TaskChange,
)

__all__ = [
    "JSON_SCHEMA_VERSION",
    "MAX_DIFF_BYTES",
    "MAX_READ_BYTES",
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
    "PreviewError",
    "Problem",
    "Sink",
    "SinkCall",
    "SinkRegistry",
    "StaticSinks",
    "TaskChange",
    "TaskItem",
    "dedupe_key",
    "plan_changes",
    "render_text",
    "to_dict",
    "to_json",
    "unified_diff",
]
