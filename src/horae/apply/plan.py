# SPDX-License-Identifier: Apache-2.0
"""The dry-run diff: what applying a run's routed intents would change (#21).

`plan_changes(record, sinks)` builds the change of every routed intent and collects them in a
`ChangeSet`: a unified diff per note, the task changes, and the notifications and HTTP calls
that would be sent. Nothing is written anywhere, and no sink is ever asked to apply.

**The core builds every preview** (schemas decision 32, D1). For `note.*` it asks the sink
only for the note's current bytes (`Sink.read`) and computes the new bytes itself
(`horae.apply.notes`); tasks, notifications and HTTP calls are described from the intent
alone, without calling the sink. So a plugin cannot show one change and make another. What a
sink returns is checked at runtime (bytes or `None`, at most `MAX_NOTE_BYTES`, else
`preview_invalid` / `note_too_large`), since over MCP (#24) it may return anything.

**What is planned (ADR 0002, schemas decisions 30 and 32).**

- A `failed` run is never applied, so it is not planned either: the change set is `refused`
  (`run_failed`). So is an `undone` run (`run_undone`) and one whose changes were already
  applied (`already_applied`: its journal, #22, says what changed). A still running run has
  no record. A `degraded` run is planned: what it proposed is applied.
- An intent without a route (`applied_by`) must not be applied: it is reported
  (`unrouted`). So is one whose entry's step did not run `ok` (`entry_skipped`), one whose
  tool has no sink (`no_sink`), and one whose note cannot be read or changed
  (`SinkError.kind`, or `host_error` for any other exception, without its message).

**Diffs** are `difflib` unified diffs, deterministic (3 lines of context, no timestamps),
labelled `a/<path>` and `b/<path>` (`/dev/null` for a new note). A line without a final
newline is followed by `\\ No newline at end of file`; carriage returns are kept in the diff
text (`render_text` shows them as `\\r`). A side that is not UTF-8 or holds a NUL byte is
`binary`, one larger than `MAX_DIFF_BYTES` is `too_large`: neither gets a diff, both keep
their sizes and SHA-256.

**Several intents on one note are chained**, in run order: the first is planned against the
note as read, each later one against the bytes the previous one leaves (`FileDiff.follows`
names it), which is the order the journal (#22) applies them in. `before_sha256` is then
exactly what that `apply` must find (`expected_before_sha256`, the compare-and-swap); an
intent that cannot be planned does not change the bytes the next one sees.

**Notifications** carry a `dedupe_key` (`dedupe_key`). Reusing an unchanged input re-proposes
identical notifications (decision 30), so the applier (#22) sends a key once
(`Store.record_notification`); given `notified`, `plan_changes` marks the keys already sent as
`duplicate`. A duplicate is information, not a problem.
"""

import asyncio
import copy
import difflib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from horae.apply.notes import NoteIntent, note_after
from horae.apply.sink import (
    MAX_NOTE_BYTES,
    HttpCall,
    Notification,
    SinkCall,
    SinkError,
    SinkRegistry,
    TaskChange,
    error_kind,
)
from horae.contracts.intents import Intent, NotifyPush, TaskCreate, TaskUpdateStatus
from horae.contracts.run import ProposedIntent, RunRecord
from horae.engine.canonical import sha256_hex

MAX_DIFF_BYTES = 1024 * 1024
"""Sides larger than this (1 MiB) get no diff (`too_large`)."""

NO_NEWLINE = "\\ No newline at end of file\n"


@dataclass(frozen=True)
class Refusal:
    """Why nothing of a run is planned."""

    kind: Literal["run_failed", "run_undone", "already_applied"]
    message: str


_REFUSED_STATUSES = {
    "failed": Refusal("run_failed", "a failed run is never applied"),
    "undone": Refusal("run_undone", "the run was undone"),
}


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
    """What `apply` must find (`expected_before_sha256`); `None`: no note."""
    after_sha256: str
    diff: str | None
    """The unified diff; `None` when unchanged, binary or too large."""
    binary: bool = False
    too_large: bool = False
    follows: int | None = None
    """The earlier intent of the run changing the same note: this one is planned against the
    bytes it leaves."""


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
    refused: Refusal | None = None
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_diff(
    index: int, tool: str, path: str, before: bytes | None, after: bytes, follows: int | None
) -> FileDiff:
    status: Literal["create", "modify", "unchanged"]
    status = "create" if before is None else "unchanged" if before == after else "modify"
    binary = _text(before) is None or _text(after) is None
    too_large = not binary and max(len(before or b""), len(after)) > MAX_DIFF_BYTES
    diff = None
    if status != "unchanged" and not binary and not too_large:
        diff = unified_diff(path, before, after)
    return FileDiff(
        intent=index,
        applied_by=tool,
        path=path,
        status=status,
        before_size=None if before is None else len(before),
        after_size=len(after),
        before_sha256=None if before is None else _sha(before),
        after_sha256=_sha(after),
        diff=diff,
        binary=binary and status != "unchanged",
        too_large=too_large and status != "unchanged",
        follows=follows,
    )


def entry_params(record: RunRecord, proposed: ProposedIntent) -> dict[str, Any]:
    """The trusted parameters of the entry routing `proposed`, as its step recorded them
    (empty when it did not), as a private copy."""
    step, entry = proposed.applied_by_step, proposed.applied_by_call
    if step is None or entry is None:
        return {}
    output = record.steps[step].output
    if isinstance(output, list) and entry < len(output) and isinstance(output[entry], dict):
        raw = output[entry].get("params")
        if isinstance(raw, dict):
            return copy.deepcopy(raw)
    return {}


def _identity(record: RunRecord, proposed: ProposedIntent) -> dict[str, Any]:
    return {
        "applied_by": proposed.applied_by,
        "params": entry_params(record, proposed),
        "intent": proposed.intent.model_dump(mode="json"),
    }


def dedupe_key(record: RunRecord, index: int) -> str:
    """The deduplication key of the notification at `index` in `record.intents`.

    The SHA-256 (canonical JSON) of the pipeline, the routing entry (`applied_by` and its
    parameters), the run's `input_hash` (or, without one, its id), the intent, and its
    occurrence: how many identical (entry, intent) pairs come before it in the run. So a
    reused input re-proposes the same keys, while two identical notifications in one run get
    two keys (both are sent)."""
    mine = _identity(record, record.intents[index])
    occurrence = sum(1 for p in record.intents[:index] if _identity(record, p) == mine)
    scope = {"input_hash": record.input_hash} if record.input_hash else {"run_id": record.id}
    return sha256_hex({"pipeline": record.pipeline, **scope, **mine, "occurrence": occurrence})


def describe(intent: Intent) -> TaskChange | Notification | HttpCall | None:
    """The change of a task, notification or HTTP intent, from the intent alone (`None` for a
    note intent). The current state of a task is not known: `TaskChange.before` is `None`."""
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
    if isinstance(intent, NoteIntent):
        return None
    return HttpCall(intent.url, copy.deepcopy(intent.body))


def _too_large(path: str) -> SinkError:
    return SinkError("note_too_large", f"{path} is larger than {MAX_NOTE_BYTES} bytes")


async def plan_changes(
    record: RunRecord,
    sinks: SinkRegistry,
    *,
    notified: Callable[[str], bool] | None = None,
    call_timeout_s: float | None = None,
) -> ChangeSet:
    """Plan every routed intent of `record`; never applies anything.

    Only `Sink.read` is called, for `note.*` intents, each bounded by `call_timeout_s`
    (`None`: unbounded; past it the intent is a `timeout` problem). `notified(key)` says
    whether a notification's `dedupe_key` was already sent."""
    base = ChangeSet(record.id, record.pipeline, record.status, record.dry_run)
    if record.status in _REFUSED_STATUSES:
        return replace(base, refused=_REFUSED_STATUSES[record.status])
    if record.changes:
        message = "the run's changes were already applied (its journal, #22, lists them)"
        return replace(base, refused=Refusal("already_applied", message))
    files: list[FileDiff] = []
    tasks: list[TaskItem] = []
    notes: list[NotificationItem] = []
    calls: list[HttpItem] = []
    problems: list[Problem] = []
    pending: dict[tuple[int, str], tuple[bytes, int]] = {}
    """The bytes each (sink, note) is left with by the last planned intent, and that intent."""
    for index, proposed in enumerate(record.intents):
        tool, step = proposed.applied_by, proposed.applied_by_step
        if tool is None or step is None or proposed.applied_by_call is None:
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
        intent = proposed.intent
        described = describe(intent)
        if isinstance(described, TaskChange):
            tasks.append(TaskItem(index, tool, described))
            continue
        if isinstance(described, Notification):
            key = dedupe_key(record, index)
            duplicate = None if notified is None else notified(key)
            notes.append(NotificationItem(index, tool, described, key, duplicate))
            continue
        if isinstance(described, HttpCall):
            calls.append(HttpItem(index, tool, described))
            continue
        assert isinstance(intent, NoteIntent)  # noqa: S101 - `describe` covers the rest
        slot = (id(sink), intent.path)
        follows: int | None = None
        try:
            if slot in pending:
                before: bytes | None
                before, follows = pending[slot]
            else:
                call = SinkCall(
                    record.id,
                    index,
                    tool,
                    entry_params(record, proposed),
                    intent.model_copy(deep=True),
                    record.input_hash,
                )
                try:
                    async with asyncio.timeout(call_timeout_s):
                        before = await sink.read(call)
                except SinkError:
                    raise
                except TimeoutError:
                    message = f"the sink read took longer than {call_timeout_s:g} s"
                    problems.append(Problem(index, tool, "timeout", message))
                    continue
                except Exception:
                    problems.append(Problem(index, tool, "host_error", "the sink read failed"))
                    continue
                if before is not None and type(before) is not bytes:
                    message = "the sink returned something other than bytes for the note"
                    problems.append(Problem(index, tool, "preview_invalid", message))
                    continue
                if before is not None and len(before) > MAX_NOTE_BYTES:
                    raise _too_large(intent.path)
            after = note_after(intent, before)
            if len(after) > MAX_NOTE_BYTES:
                raise _too_large(intent.path)
        except SinkError as error:
            problems.append(Problem(index, tool, error_kind(error.kind), str(error.message)))
            continue
        pending[slot] = (after, index)
        files.append(_file_diff(index, tool, intent.path, before, after, follows))
    return replace(
        base,
        files=tuple(files),
        tasks=tuple(tasks),
        notifications=tuple(notes),
        http_calls=tuple(calls),
        problems=tuple(problems),
    )
