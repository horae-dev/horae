# SPDX-License-Identifier: Apache-2.0
"""The dry-run diff: what applying a run's routed intents would change (#21).

`plan_changes(record, sinks)` calls each intent's sink **`preview`** (never `apply`) and
collects the results in a `ChangeSet`: a unified diff per note, the task changes, and the
notifications and HTTP calls that would be sent. Nothing is written anywhere.

**What is planned (ADR 0002, schemas decision 30).**

- A `failed` run is never applied, so it is not planned either: the change set is `refused`.
  So is an `undone` run, and a still running one cannot be planned (it has no record).
- An intent without a route (`applied_by`) must not be applied: it is reported
  (`unrouted`), never previewed. So is one whose entry's step did not run `ok`
  (`entry_skipped`), one whose tool has no sink (`no_sink`), and one whose preview fails
  (`PreviewError.kind`, or `host_error` for any other exception, without its message).
- A preview must describe the routed target and the right kind of change: a `FileChange` with
  the intent's `path` for `note.*`, a `TaskChange` with its fingerprint for `task.*`, a
  `Notification` for `notify.push` and an `HttpCall` with its `url` for `http.post`
  (`preview_invalid` otherwise).

**Diffs** are `difflib` unified diffs, deterministic (3 lines of context, no timestamps),
labelled `a/<path>` and `b/<path>` (`/dev/null` for a new note). A line without a final
newline is followed by `\\ No newline at end of file`; carriage returns are kept in the diff
text (`render_text` shows them as `\\r`). A side that is not UTF-8 or holds a NUL byte is
`binary`, one larger than `MAX_DIFF_BYTES` is `too_large`: neither gets a diff, both keep
their sizes and SHA-256. Each intent is previewed against the current target, so when
several intents change the same note, each diff is relative to the current note and
`overlaps` lists the others (their order of application is the journal's, #22).

**Notifications** carry a `dedupe_key`: the SHA-256 (canonical JSON) of the run's
`input_hash` (or, without one, its id) and the intent. Reusing an unchanged input re-proposes
identical notifications (decision 30), so the applier (#22) sends a key once
(`Store.record_notification`); given `notified`, `plan_changes` marks the keys already sent as
`duplicate`.
"""

import difflib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from horae.apply.sink import (
    FileChange,
    HttpCall,
    Notification,
    Preview,
    PreviewError,
    SinkCall,
    SinkRegistry,
    TaskChange,
)
from horae.contracts.intents import HttpPost, Intent, NotifyPush, TaskCreate, TaskUpdateStatus
from horae.contracts.run import RunRecord
from horae.engine.canonical import sha256_hex

MAX_DIFF_BYTES = 1024 * 1024
"""Sides larger than this (1 MiB) get no diff (`too_large`)."""

NO_NEWLINE = "\\ No newline at end of file\n"

_REFUSED_STATUSES = {"failed": "a failed run is never applied", "undone": "the run was undone"}


@dataclass(frozen=True)
class FileDiff:
    """The change to one note."""

    intent: int
    applied_by: str
    path: str
    status: Literal["create", "modify", "unchanged"]
    before_size: int | None
    after_size: int
    before_sha256: str | None
    after_sha256: str
    diff: str | None
    """The unified diff; `None` when unchanged, binary or too large."""
    binary: bool = False
    too_large: bool = False
    overlaps: tuple[int, ...] = ()
    """Other intents of the run that change the same note."""


@dataclass(frozen=True)
class TaskItem:
    intent: int
    applied_by: str
    change: TaskChange


@dataclass(frozen=True)
class NotificationItem:
    intent: int
    applied_by: str
    notification: Notification
    dedupe_key: str
    duplicate: bool | None = None
    """Already sent (`True`), not yet (`False`), or not checked (`None`)."""


@dataclass(frozen=True)
class HttpItem:
    intent: int
    applied_by: str
    call: HttpCall


@dataclass(frozen=True)
class Problem:
    """An intent that cannot be planned, and so would not be applied."""

    intent: int
    applied_by: str | None
    kind: str
    message: str


@dataclass(frozen=True)
class ChangeSet:
    """What applying a run would change."""

    run_id: str
    pipeline: str
    run_status: str
    dry_run: bool
    refused: str | None = None
    """Why nothing of the run is planned (e.g. it failed)."""
    files: tuple[FileDiff, ...] = ()
    tasks: tuple[TaskItem, ...] = ()
    notifications: tuple[NotificationItem, ...] = ()
    http_calls: tuple[HttpItem, ...] = ()
    problems: tuple[Problem, ...] = ()

    @property
    def empty(self) -> bool:
        """Nothing would change (problems aside)."""
        changed = [f for f in self.files if f.status != "unchanged"]
        return not (changed or self.tasks or self.notifications or self.http_calls)


def _lines(text: str) -> list[str]:
    """`text` split after each `\\n` only (not `str.splitlines`: a `\\r` stays in its line)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def unified_diff(path: str, before: bytes | None, after: bytes) -> str:
    """The unified diff of note `path` from `before` (`None`: no note) to `after` (text)."""
    old, new = _text(before), _text(after)
    if old is None or new is None:
        raise ValueError("binary content has no text diff")
    out: list[str] = []
    for line in difflib.unified_diff(
        _lines(old),
        _lines(new),
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    ):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n" + NO_NEWLINE)
    return "".join(out)


def _file_diff(index: int, tool: str, change: FileChange) -> FileDiff:
    before, after = change.before, change.after
    status: Literal["create", "modify", "unchanged"]
    status = "create" if before is None else "unchanged" if before == after else "modify"
    binary = _text(before) is None or _text(after) is None
    too_large = not binary and max(len(before or b""), len(after)) > MAX_DIFF_BYTES
    diff = None
    if status != "unchanged" and not binary and not too_large:
        diff = unified_diff(change.path, before, after)
    return FileDiff(
        intent=index,
        applied_by=tool,
        path=change.path,
        status=status,
        before_size=None if before is None else len(before),
        after_size=len(after),
        before_sha256=None if before is None else hashlib.sha256(before).hexdigest(),
        after_sha256=hashlib.sha256(after).hexdigest(),
        diff=diff,
        binary=binary and status != "unchanged",
        too_large=too_large and status != "unchanged",
    )


def dedupe_key(record: RunRecord, intent: Intent) -> str:
    """The deduplication key of a notification proposed by `record`."""
    scope = {"input_hash": record.input_hash} if record.input_hash else {"run": record.id}
    return sha256_hex({**scope, "intent": intent.model_dump(mode="json")})


def _is_bytes(value: object) -> bool:
    """A runtime check: a sink over MCP may return anything."""
    return isinstance(value, bytes)


def _expected(intent: Intent, preview: Preview) -> bool:
    """`preview` is the right kind of change, for the routed target."""
    if isinstance(intent, TaskCreate | TaskUpdateStatus):
        fingerprint = intent.fingerprint if isinstance(intent, TaskCreate) else intent.task
        return isinstance(preview, TaskChange) and preview.fingerprint == fingerprint
    if isinstance(intent, NotifyPush):
        return isinstance(preview, Notification)
    if isinstance(intent, HttpPost):
        return isinstance(preview, HttpCall) and preview.url == intent.url
    return (
        isinstance(preview, FileChange)
        and preview.path == intent.path
        and _is_bytes(preview.after)
        and (preview.before is None or _is_bytes(preview.before))
    )


async def plan_changes(
    record: RunRecord,
    sinks: SinkRegistry,
    *,
    notified: Callable[[str], bool] | None = None,
) -> ChangeSet:
    """Preview every routed intent of `record` with its sink; never applies anything.

    `notified(key)` says whether a notification's `dedupe_key` was already sent."""
    base = ChangeSet(record.id, record.pipeline, record.status, record.dry_run)
    if record.status in _REFUSED_STATUSES:
        return replace(base, refused=_REFUSED_STATUSES[record.status])
    files: list[FileDiff] = []
    tasks: list[TaskItem] = []
    notes: list[NotificationItem] = []
    calls: list[HttpItem] = []
    problems: list[Problem] = []
    for index, proposed in enumerate(record.intents):
        tool, step, entry = proposed.applied_by, proposed.applied_by_step, proposed.applied_by_call
        if tool is None or step is None or entry is None:
            problems.append(Problem(index, None, "unrouted", "the intent has no sink"))
            continue
        if record.steps[step].status != "ok":
            message = f"step {step} did not run ({record.steps[step].status})"
            problems.append(Problem(index, tool, "entry_skipped", message))
            continue
        sink = sinks.sink_for(tool)
        if sink is None:
            problems.append(Problem(index, tool, "no_sink", f"no sink serves {tool}"))
            continue
        output = record.steps[step].output
        params = {}
        if isinstance(output, list) and entry < len(output) and isinstance(output[entry], dict):
            raw = output[entry].get("params")
            params = dict(raw) if isinstance(raw, dict) else {}
        call = SinkCall(record.id, index, tool, params, proposed.intent, record.input_hash)
        try:
            preview = await sink.preview(call)
        except PreviewError as error:
            problems.append(Problem(index, tool, str(error.kind), str(error.message)))
            continue
        except Exception:
            problems.append(Problem(index, tool, "host_error", "the preview failed"))
            continue
        if not _expected(proposed.intent, preview):
            message = f"the preview does not describe this {proposed.intent.type}"
            problems.append(Problem(index, tool, "preview_invalid", message))
        elif isinstance(preview, FileChange):
            files.append(_file_diff(index, tool, preview))
        elif isinstance(preview, TaskChange):
            tasks.append(TaskItem(index, tool, preview))
        elif isinstance(preview, Notification):
            key = dedupe_key(record, proposed.intent)
            duplicate = None if notified is None else notified(key)
            notes.append(NotificationItem(index, tool, preview, key, duplicate))
        else:
            calls.append(HttpItem(index, tool, preview))
    for position, diff in enumerate(files):
        others = tuple(f.intent for f in files if f.path == diff.path and f is not diff)
        if others:
            files[position] = replace(diff, overlaps=others)
    return replace(
        base,
        files=tuple(files),
        tasks=tuple(tasks),
        notifications=tuple(notes),
        http_calls=tuple(calls),
        problems=tuple(problems),
    )
