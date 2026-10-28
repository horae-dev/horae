# SPDX-License-Identifier: Apache-2.0
"""The apply phase: journal, then apply, a run's planned changes (#22, schemas decision 34).

`apply_changes(record, changeset, sinks, store)` applies the change set `plan_changes` built
for a finished, non-dry, non-failed run whose row is still `running` (the runner calls it
before saving the record: `Store.journal_append` refuses a run that is no longer running).

**Order.** Changes are applied in intent order, each the same way:

1. **Journal first** (`Store.journal_append`): the intent, its type, the core's target (a
   note path, a task fingerprint, an `http.post` URL, the notifier's tool for a
   notification), the sink (`applied_by`) and where it writes (`sink_identity`, when the sink
   has an `identity`) and, for a note, the bytes before and after.
   Notes are undoable; tasks, notifications and HTTP calls are not (`undoable=False`:
   undo restores files only, ADR 0002).
2. **Apply** through the sink (`Sink.apply`). For a note the core first reads the note
   (`Sink.read`, or the bytes the previous change of the run left for a chained one), checks
   that it is still what the plan saw (`before_sha256`) and recomputes the new bytes with
   `horae.apply.notes`; the sink's compare-and-swap (`expected_before_sha256`) checks again.
3. **Mark applied** (`Store.journal_mark_applied`); for a notification in the same
   transaction as `Store.record_notification(dedupe_key)`. When the sink reports that it
   wrote other bytes than planned, they are journalled too (`actual_after`), so undo accepts
   them, and the apply stops (`apply_mismatch`).

**Bounded.** Each sink call (`read`, `apply`) is bounded by `call_timeout_s` (the runner
passes `horae.yaml` `plugin_defaults.call_timeout_s`): past it the call is cancelled and the
apply stops with kind `timeout`; the change stays "maybe applied" (`applied = 0`).

**Sinks first.** Before the first change, the registry's optional `prepare` is awaited with
the tools of the run's changes (`horae.apply.sink.prepare_sinks`): a plugin sink fetches its
identity over MCP, and `sink_identity` is synchronous. After the apply, the registry's
violations (`take_violations`, e.g. a plugin sink that returned a secret) are added to the
record's `violations` (at most 256 in all).

A notification whose `dedupe_key` was already sent (by any run) is skipped: not journalled,
not applied, listed in `ApplyResult.duplicates`. An unchanged note is skipped too.

**Failures.** A plan with problems, or with a refusal, applies nothing (`plan_invalid`,
`plan_refused`). Otherwise the first change that fails stops the apply: the changes before it
stay applied (and in `RunRecord.changes`), the rest are not applied, and the run becomes
`failed` with an error naming the kind (`target_changed`, the sink's `SinkError.kind` when it
matches `^[a-z_]{1,32}$`, `timeout`, or `host_error` for any other exception, whose message is
never kept), the intent and its sink.
An entry journalled for a change that failed keeps `applied = 0` ("maybe applied"): undo
compares the target with its before and after digests. A crash (the process dies, or a
`BaseException` such as a cancellation) leaves the journal as it is; the runner abandons the
run and `horae undo` can still revert what was written.
"""

import asyncio
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
from horae.apply.sink import (
    MAX_NOTE_BYTES,
    Sink,
    SinkCall,
    SinkError,
    SinkRegistry,
    error_kind,
    prepare_sinks,
    sink_identity,
    take_violations,
)
from horae.contracts.run import AppliedChange, RunRecord, Violation
from horae.contracts.types import MAX_ITEMS
from horae.secrets.redact import current as current_redactor
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
    record: RunRecord,
    applied: list[AppliedChange],
    failure: ApplyFailure | None,
    violations: tuple[Violation, ...] = (),
) -> RunRecord:
    data = record.model_dump(mode="json")
    data["changes"] = [change.model_dump(mode="json") for change in applied]
    if violations:
        kept = [*record.violations, *violations][:MAX_ITEMS]
        data["violations"] = [violation.model_dump(mode="json") for violation in kept]
    if failure is not None:
        where = f"intent {failure.intent}"
        if failure.applied_by is not None:
            where += f" ({failure.applied_by})"
        error = (
            f"{failure.kind}: {where} could not be applied: {failure.message}; "
            f"{len(applied)} change(s) applied before it, the rest not applied"
        )
        data["status"] = "failed"
        # Redacted before it is cut (#53): a secret cut in half would not match any more.
        data["error"] = current_redactor().redact_text(error)[0][:_MAX_ERROR]
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
    call_timeout_s: float | None = None,
) -> ApplyResult:
    """Journal and apply `changeset` (planned from `record`), in intent order; each sink call
    is bounded by `call_timeout_s` (`None`: unbounded).

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
        # Before any identity is read (`sink_identity` is synchronous): the run's sinks only.
        await prepare_sinks(sinks, (item.applied_by for item in _items(changeset)))
        try:
            await _apply_all(
                record,
                changeset,
                sinks,
                store,
                clock,
                _Out(applied, duplicates, unchanged),
                call_timeout_s,
            )
        except _Stop as stop:
            failure = stop.failure
    violations = take_violations(sinks)
    return ApplyResult(
        _with_changes(record, applied, failure, violations),
        tuple(applied),
        tuple(duplicates),
        tuple(unchanged),
        failure,
    )


async def _guarded[T](
    coro: Awaitable[T], intent: int, tool: str, what: str, timeout_s: float | None
) -> T:
    """Await a sink call, at most `timeout_s`; a `SinkError`, the timeout or any other
    exception stops the apply."""
    try:
        async with asyncio.timeout(timeout_s):
            return await coro
    except SinkError as error:
        kind = error_kind(error.kind)
        message = str(error.message) if kind != "host_error" else f"the sink {what} failed"
        raise _Stop(ApplyFailure(intent, tool, kind, message)) from None
    except TimeoutError:
        message = f"the sink {what} took longer than {timeout_s:g} s"
        raise _Stop(ApplyFailure(intent, tool, "timeout", message)) from None
    except Exception:
        # Only the kind: an exception's message may hold data from a plugin.
        raise _Stop(ApplyFailure(intent, tool, "host_error", f"the sink {what} failed")) from None


async def _before(
    item: FileDiff,
    sink: Sink,
    call: SinkCall,
    pending: dict[tuple[int, str], bytes],
    timeout_s: float | None,
) -> bytes | None:
    slot = (id(sink), item.path)
    if slot in pending:
        return pending[slot]
    read = await _guarded(sink.read(call), item.intent, item.applied_by, "read", timeout_s)
    if read is not None and (type(read) is not bytes or len(read) > MAX_NOTE_BYTES):
        message = "the sink returned something other than note bytes"
        raise _Stop(ApplyFailure(item.intent, item.applied_by, "preview_invalid", message))
    return read


@dataclass(frozen=True)
class _Out:
    applied: list[AppliedChange]
    duplicates: list[int]
    unchanged: list[int]


def _identity(sink: Sink, index: int, tool: str, known: dict[int, str | None]) -> str | None:
    if id(sink) not in known:
        try:
            known[id(sink)] = sink_identity(sink)
        except SinkError as error:
            kind = error_kind(error.kind)
            raise _Stop(ApplyFailure(index, tool, kind, str(error.message))) from None
        except Exception:
            message = "the sink's identity could not be read"
            raise _Stop(ApplyFailure(index, tool, "host_error", message)) from None
    return known[id(sink)]


def _written(result: object) -> tuple[bool, bytes | None]:
    """Whether the sink's `Applied.after` is usable, and the bytes it says it wrote."""
    written = getattr(result, "after", None)
    if written is None:
        return True, None
    if type(written) is not bytes or len(written) > MAX_NOTE_BYTES:
        return False, None
    return True, written


async def _apply_all(
    record: RunRecord,
    changeset: ChangeSet,
    sinks: SinkRegistry,
    store: Store,
    clock: Callable[[], datetime],
    out: _Out,
    timeout_s: float | None,
) -> None:
    applied, duplicates, unchanged = out.applied, out.duplicates, out.unchanged
    pending: dict[tuple[int, str], bytes] = {}
    """The bytes the last applied change of the run left in each (sink, note)."""
    identities: dict[int, str | None] = {}
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
            before = await _before(item, sink, call, pending, timeout_s)
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
        identity = _identity(sink, index, tool, identities)
        entry = store.journal_append(
            record.id,
            intent=index,
            intent_type=intent.type,
            target=target,
            before=before,
            after=after,
            undoable=isinstance(item, FileDiff),
            applied_by=tool,
            sink_identity=identity,
            at=clock(),
        )
        result = await _guarded(
            sink.apply(call, expected_before_sha256=expected), index, tool, "apply", timeout_s
        )
        usable, written = _written(result) if isinstance(item, FileDiff) else (True, None)
        actual = written if written is not None and written != after else None
        with store.transaction():
            if key is not None:
                store.record_notification(key, at=clock(), run_id=record.id)
            store.journal_mark_applied(entry.id, actual_after=actual)
        applied.append(AppliedChange(intent=index, target=target))
        if isinstance(item, FileDiff):
            assert after is not None  # noqa: S101 - set for every note
            pending[(id(sink), item.path)] = after
            if not usable or actual is not None:
                message = f"the sink wrote other bytes to {item.path} than the core planned"
                raise _Stop(ApplyFailure(index, tool, "apply_mismatch", message))
