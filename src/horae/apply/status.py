# SPDX-License-Identifier: Apache-2.0
"""What the journal keeps of a `task.update_status` so `horae undo` can revert it (#139,
schemas decision 59).

Two blobs, both JSON: the task index entry the change replaces (`task_snapshot`, the
entry's `before`; none for a task the index does not know) and the sink's `Applied.details`
(`status_details`, the entry's `after`), which the sink gets back untouched to put the task
back. Only a change whose answer says `changed: true` is journalled as undoable: a sink that
changed nothing (a recurring task, a task already in that state) has nothing to revert, and
a sink that says nothing keeps the behaviour of before (not undoable).
"""

import json
from typing import Any

from horae.contracts.intents import Intent, TaskUpdateStatus
from horae.store import Store, StoreError, TaskEntry

MAX_STATUS_DETAILS_BYTES = 64 * 1024
"""The largest `Applied.details` kept for an undo; a larger answer is not journalled (the
change is then not undoable, as before)."""


def task_snapshot(store: Store, fingerprint: str) -> bytes | None:
    """The task index entry for `fingerprint` as JSON, or `None` (unknown, or unreadable)."""
    try:
        entry = store.get_task(fingerprint)
    except (StoreError, ValueError):
        return None
    return None if entry is None else entry.model_dump_json().encode("utf-8")


def load_snapshot(data: bytes | None) -> TaskEntry | None:
    """The `TaskEntry` of `task_snapshot`, or `None` (none was kept, or it does not parse)."""
    if data is None:
        return None
    try:
        return TaskEntry.model_validate_json(data)
    except ValueError:
        return None


def status_details(intent: Intent, result: object) -> bytes | None:
    """The sink's answer to a `task.update_status` as JSON, when it makes the change
    undoable: it says `changed: true` and fits `MAX_STATUS_DETAILS_BYTES`."""
    if not isinstance(intent, TaskUpdateStatus):
        return None
    details: Any = getattr(result, "details", None)
    if not isinstance(details, dict) or details.get("changed") is not True:
        return None
    try:
        data = json.dumps(details, allow_nan=False, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return None
    encoded = data.encode("utf-8", "surrogatepass")
    return encoded if len(encoded) <= MAX_STATUS_DETAILS_BYTES else None
