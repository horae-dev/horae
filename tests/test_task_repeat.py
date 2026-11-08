# SPDX-License-Identifier: Apache-2.0
"""A repeated `task.create` fingerprint across runs (#138, schemas decision 58)."""

import asyncio
from pathlib import Path
from typing import Any

from apply_fixtures import START
from horae.apply import Applied, StaticSinks, apply_changes, plan_changes, render_text
from horae.apply.testing import FixtureSink
from horae.contracts import RunRecord
from horae.store import Store
from test_act_mint import FP, TASK_CREATE, _record

DB = "s.db"


def _again(record: RunRecord, run_id: str) -> RunRecord:
    return record.model_copy(update={"id": run_id})


def _run(store: Store, record: RunRecord, sink: FixtureSink) -> tuple[Any, Any]:
    store.begin_run(record.id, record.pipeline, record.started_at)
    sinks = StaticSinks(default=sink)

    async def go() -> tuple[Any, Any]:
        planned = await plan_changes(record, sinks, known_task=store.get_task)
        result = await apply_changes(record, planned, sinks, store, clock=lambda: START)
        return planned, result

    planned, result = asyncio.run(go())
    store.save_run(result.record)
    return planned, result


def test_same_fingerprint_across_two_runs_is_existing_the_second_time(tmp_path: Path) -> None:
    first = _record([TASK_CREATE])
    sink = FixtureSink()
    with Store.open(tmp_path / DB) as store:
        planned, _ = _run(store, first, sink)
        assert [t.change.action for t in planned.tasks] == ["create"]
        second = _again(first, "run-2")
        again = asyncio.run(
            plan_changes(second, StaticSinks(default=sink), known_task=store.get_task)
        )
        change = again.tasks[0].change
        assert change.action == "existing"
        assert change.before == change.after
        assert "(unchanged)" in render_text(again)
        # Apply still sends it to the sink, the authority on the vault.
        planned, result = _run(store, second, sink)
        assert result.failure is None
        assert len(sink.applied) == 2


def test_a_changed_due_is_planned_as_an_update(tmp_path: Path) -> None:
    first = _record([TASK_CREATE])
    moved = _record([{**TASK_CREATE, "due": "2026-11-02", "title": "Old task!"}])
    with Store.open(tmp_path / DB) as store:
        _run(store, first, FixtureSink())
        plan = asyncio.run(
            plan_changes(
                _again(moved, "run-2"),
                StaticSinks(default=FixtureSink()),
                known_task=store.get_task,
            )
        )
        change = plan.tasks[0].change
        assert change.action == "update"
        assert change.before == "- [ ] Old task (due 2026-10-28)"
        assert change.after == "- [ ] Old task! (due 2026-11-02)"


def test_a_closed_task_is_not_reopened(tmp_path: Path) -> None:
    first = _record([TASK_CREATE])
    moved = _record([{**TASK_CREATE, "due": "2026-11-02"}])
    with Store.open(tmp_path / DB) as store:
        _run(store, first, FixtureSink())
        store.set_task_status(FP, "done", at=START)
        _, result = _run(store, _again(moved, "run-2"), FixtureSink())
        assert result.failure is None
        task = store.get_task(FP)
        assert task is not None
        assert (task.status, str(task.due)) == ("done", "2026-11-02")


def test_a_fingerprint_repeated_in_one_run_is_existing_after_the_first() -> None:
    record = _record([TASK_CREATE, TASK_CREATE])
    plan = asyncio.run(plan_changes(record, StaticSinks(default=FixtureSink())))
    assert [t.change.action for t in plan.tasks] == ["create", "existing"]


def test_a_sink_that_changes_nothing_keeps_the_index_consistent(tmp_path: Path) -> None:
    class Answer(FixtureSink):
        def __init__(self, details: dict[str, Any]) -> None:
            super().__init__()
            self.details = details

        async def apply(self, call: Any, *, expected_before_sha256: str | None) -> Applied:
            await super().apply(call, expected_before_sha256=expected_before_sha256)
            return Applied(call.tool, details=self.details)

    record = _record([TASK_CREATE])
    with Store.open(tmp_path / DB) as store:
        # Dismissed (e.g. ticked in the vault): the index learns nothing.
        _run(store, record, Answer({"changed": False, "dismissed": True}))
        assert store.get_task(FP) is None
        # Adopted after an index loss: indexed as a todo, once.
        _run(store, _again(record, "run-2"), Answer({"changed": False, "adopted": True}))
        task = store.get_task(FP)
        assert task is not None
        assert task.status == "todo"
        # A known, ticked task stays ticked when the sink adopts it again.
        store.set_task_status(FP, "done", at=START)
        _run(store, _again(record, "run-3"), Answer({"changed": False, "adopted": True}))
        task = store.get_task(FP)
        assert task is not None
        assert task.status == "done"
