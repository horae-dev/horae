# SPDX-License-Identifier: Apache-2.0
"""The apply phase: journal, then apply, a run's planned changes (#22, schemas decision 34).

`apply_changes(record, changeset, sinks, store)` applies the change set `plan_changes` built
for a finished, non-dry, non-failed run whose row is still `running` (the runner calls it
before saving the record: `Store.journal_append` refuses a run that is no longer running).

**Order.** Changes are applied in intent order, each the same way:

1. **Journal first** (`Store.journal_append`): the intent, its type, the core's target (a
   note path, a task fingerprint, an `http.post` URL, the notifier's tool for a
   notification), the sink (`applied_by`) and, for a note, the bytes before and after.
   Notes are undoable; tasks, notifications and HTTP calls are not (`undoable=False`:
   undo restores files only, ADR 0002).
2. **Apply** through the sink (`Sink.apply`). For a note the core first reads the note
   (`Sink.read`, or the bytes the previous change of the run left for a chained one), checks
   that it is still what the plan saw (`before_sha256`) and recomputes the new bytes with
   `horae.apply.notes`; the sink's compare-and-swap (`expected_before_sha256`) checks again.
3. **Mark applied** (`Store.journal_mark_applied`); for a notification in the same
   transaction as `Store.record_notification(dedupe_key)`.

A notification whose `dedupe_key` was already sent (by any run) is skipped: not journalled,
not applied, listed in `ApplyResult.duplicates`. An unchanged note is skipped too.

**Failures.** A plan with problems, or with a refusal, applies nothing (`plan_invalid`,
`plan_refused`). Otherwise the first change that fails stops the apply: the changes before it
stay applied (and in `RunRecord.changes`), the rest are not applied, and the run becomes
`failed` with an error naming the kind (`target_changed`, the sink's `SinkError.kind`, or
`host_error` for any other exception, whose message is never kept), the intent and its sink.
An entry journalled for a change that failed keeps `applied = 0` ("maybe applied"): undo
compares the target with its before and after digests. A crash (the process dies, or a
`BaseException` such as a cancellation) leaves the journal as it is; the runner abandons the
run and `horae undo` can still revert what was written.
"""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from horae.apply.notes import NoteIntent, note_after
from horae.apply.plan import (
    ChangeSet,
    FileDiff,
    HttpItem,
    NotificationItem,
    TaskItem,
    entry_params,
)
from horae.apply.sink import MAX_NOTE_BYTES, Sink, SinkCall, SinkError, SinkRegistry
from horae.contracts.run import AppliedChange, RunRecord
from horae.store import Store

_MAX_ERROR = 4096


@dataclass(frozen=True)
class ApplyFailure:
    """The change that stopped the apply."""

    intent: int
    applied_by: str | None
    kind: str
    message: str


@dataclass(frozen=True)
class ApplyResult:
    """What `apply_changes` did. `record` is the run record to save: with `changes`, and
    `failed` when `failure` is set."""

    record: RunRecord
    applied: tuple[AppliedChange, ...] = ()
    duplicates: tuple[int, ...] = ()
    """Notifications skipped because their `dedupe_key` was already sent."""
    unchanged: tuple[int, ...] = ()
    """Note changes skipped because they change nothing."""
    failure: ApplyFailure | None = None


class ApplyRefused(ValueError):
    """`apply_changes` was called for a run that must not be applied (a programming error)."""


_Item = FileDiff | TaskItem | NotificationItem | HttpItem


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _with_changes(
    record: RunRecord, applied: list[AppliedChange], failure: ApplyFailure | None
) -> RunRecord:
    data = record.model_dump(mode="json")
    data["changes"] = [change.model_dump(mode="json") for change in applied]
    if failure is not None:
        where = f"intent {failure.intent}"
        if failure.applied_by is not None:
            where += f" ({failure.applied_by})"
        error = (
            f"{failure.kind}: {where} could not be applied: {failure.message}; "
            f"{len(applied)} change(s) applied before it, the rest not applied"
        )
        data["status"] = "failed"
        data["error"] = error[:_MAX_ERROR]
    return RunRecord.model_validate(data)


class _Stop(Exception):
    def __init__(self, failure: ApplyFailure) -> None:
        super().__init__(failure.kind)
        self.failure = failure


def _items(changeset: ChangeSet) -> list[_Item]:
    items: list[_Item] = [
        *changeset.files,
        *changeset.tasks,
        *changeset.notifications,
        *changeset.http_calls,
    ]
    return sorted(items, key=lambda item: item.intent)


def _target(item: _Item) -> str:
    if isinstance(item, FileDiff):
        return item.path
    if isinstance(item, TaskItem):
        return item.change.fingerprint
    if isinstance(item, HttpItem):
        return item.call.url
    return item.applied_by


async def apply_changes(
    record: RunRecord,
    changeset: ChangeSet,
    sinks: SinkRegistry,
    store: Store,
    *,
    clock: Callable[[], datetime],
) -> ApplyResult:
    """Journal and apply `changeset` (planned from `record`), in intent order.

    Raises `ApplyRefused` for a dry, failed or undone run, a change set of another run, or a
    record that already has changes; `StoreError` (e.g. `RunNotRunning`) from the journal."""
    if record.dry_run:
        raise ApplyRefused("a dry run is never applied")
    if record.status in {"failed", "undone"}:
        raise ApplyRefused(f"a {record.status} run is never applied")
    if record.changes:
        raise ApplyRefused("the run's changes were already applied")
    if changeset.run_id != record.id:
        raise ApplyRefused("the change set was planned for another run")
    applied: list[AppliedChange] = []
    duplicates: list[int] = []
    unchanged: list[int] = []
    failure: ApplyFailure | None = None
    if changeset.refused is not None:
        failure = ApplyFailure(0, None, "plan_refused", changeset.refused.message)
    elif changeset.problems:
        first = changeset.problems[0]
        message = (
            f"{len(changeset.problems)} intent(s) cannot be applied (first: intent "
            f"{first.intent}, {first.kind}); nothing was applied"
        )
        failure = ApplyFailure(first.intent, first.applied_by, "plan_invalid", message)
    else:
        try:
            await _apply_all(record, changeset, sinks, store, clock, applied, duplicates, unchanged)
        except _Stop as stop:
            failure = stop.failure
    return ApplyResult(
        _with_changes(record, applied, failure),
        tuple(applied),
        tuple(duplicates),
        tuple(unchanged),
        failure,
    )


async def _guarded[T](coro: Awaitable[T], intent: int, tool: str, what: str) -> T:
    """Await a sink call; a `SinkError` or any other exception stops the apply."""
    try:
        return await coro
    except SinkError as error:
        raise _Stop(ApplyFailure(intent, tool, str(error.kind), str(error.message))) from None
    except Exception:
        # Only the kind: an exception's message may hold data from a plugin.
        raise _Stop(ApplyFailure(intent, tool, "host_error", f"the sink {what} failed")) from None


async def _before(
    item: FileDiff, sink: Sink, call: SinkCall, pending: dict[tuple[int, str], bytes]
) -> bytes | None:
    slot = (id(sink), item.path)
    if slot in pending:
        return pending[slot]
    read = await _guarded(sink.read(call), item.intent, item.applied_by, "read")
    if read is not None and (type(read) is not bytes or len(read) > MAX_NOTE_BYTES):
        message = "the sink returned something other than note bytes"
        raise _Stop(ApplyFailure(item.intent, item.applied_by, "preview_invalid", message))
    return read


async def _apply_all(
    record: RunRecord,
    changeset: ChangeSet,
    sinks: SinkRegistry,
    store: Store,
    clock: Callable[[], datetime],
    applied: list[AppliedChange],
    duplicates: list[int],
    unchanged: list[int],
) -> None:
    pending: dict[tuple[int, str], bytes] = {}
    """The bytes the last applied change of the run left in each (sink, note)."""
    for item in _items(changeset):
        index, tool = item.intent, item.applied_by
        proposed = record.intents[index]
        intent = proposed.intent
        sink = sinks.sink_for(tool)
        if sink is None:
            raise _Stop(ApplyFailure(index, tool, "no_sink", f"no sink serves {tool}"))
        key: str | None = None
        if isinstance(item, NotificationItem):
            key = item.dedupe_key
            if item.duplicate or store.get_notification(key) is not None:
                duplicates.append(index)
                continue
        call = SinkCall(
            record.id,
            index,
            tool,
            entry_params(record, proposed),
            intent.model_copy(deep=True),
            record.input_hash,
            key,
        )
        before: bytes | None = None
        after: bytes | None = None
        expected: str | None = None
        if isinstance(item, FileDiff):
            assert isinstance(intent, NoteIntent)  # noqa: S101 - planned as a note
            before = await _before(item, sink, call, pending)
            changed = f"{item.path} changed since it was planned"
            if _sha(before) != item.before_sha256:
                raise _Stop(ApplyFailure(index, tool, "target_changed", changed))
            try:
                after = note_after(intent, before)
            except SinkError as error:
                raise _Stop(ApplyFailure(index, tool, error.kind, error.message)) from None
            if _sha(after) != item.after_sha256:
                raise _Stop(ApplyFailure(index, tool, "target_changed", changed))
            if before == after:
                unchanged.append(index)
                continue
            expected = item.before_sha256
        target = _target(item)
        entry = store.journal_append(
            record.id,
            intent=index,
            intent_type=intent.type,
            target=target,
            before=before,
            after=after,
            undoable=isinstance(item, FileDiff),
            applied_by=tool,
            at=clock(),
        )
        result = await _guarded(
            sink.apply(call, expected_before_sha256=expected), index, tool, "apply"
        )
        with store.transaction():
            if key is not None:
                store.record_notification(key, at=clock(), run_id=record.id)
            store.journal_mark_applied(entry.id)
        applied.append(AppliedChange(intent=index, target=target))
        if isinstance(item, FileDiff):
            assert after is not None  # noqa: S101 - set for every note
            pending[(id(sink), item.path)] = after
            written = getattr(result, "after", None)
            if written is not None and written != after:
                message = f"the sink wrote other bytes to {item.path} than the core planned"
                raise _Stop(ApplyFailure(index, tool, "apply_mismatch", message))
