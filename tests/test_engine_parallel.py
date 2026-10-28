# SPDX-License-Identifier: Apache-2.0
"""Parallel `collect`, per-call timeouts and host violations in the engine (#24)."""

import asyncio
import datetime
from typing import Any

import pytest

from horae.contracts import Pipeline, ToolCall
from horae.contracts.run import Violation
from horae.engine import (
    MAX_PARALLEL_CALLS,
    ToolCallError,
    ToolResult,
    execute,
    make_context,
    pipeline_digest,
    planned_calls,
)
from horae.engine.testing import FixturePlugins
from horae.replay import replay

START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
CONTEXT = make_context(START, "UTC")


def _manifest(sources: list[str], transforms: list[str] = ()) -> dict[str, Any]:  # type: ignore[assignment]
    return {
        "name": "slow",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": sources, "transforms": list(transforms)},
        "permissions": {},
    }


class Delayed(FixturePlugins):
    """Each source sleeps a different time (later sources finish first) and records when
    calls start and finish, and how many are in flight."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.sources = [f"slow.s{n}" for n in range(count)]
        self.add(_manifest(self.sources, ["slow.t"]))
        self.started: list[str] = []
        self.finished: list[str] = []
        self.flight = 0
        self.peak = 0
        self.timeouts: list[float | None] = []

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        self.started.append(ref)
        self.timeouts.append(timeout_s)
        self.flight += 1
        self.peak = max(self.peak, self.flight)
        try:
            if ref == "slow.t":
                return ToolResult(data={"input": input})
            position = int(ref.removeprefix("slow.s"))
            await asyncio.sleep(0.002 * (len(self.sources) - position))
            if position == 1:
                raise ToolCallError("timeout", "took too long", stderr=f"stderr {position}")
            violation = Violation(kind="secret_in_output", detail=f"v{position}")
            return ToolResult(
                data={"n": position}, stderr=f"stderr {position}", violations=(violation,)
            )
        finally:
            self.flight -= 1
            self.finished.append(ref)


async def _execute(pipeline: Pipeline, tools: FixturePlugins) -> Any:
    return await execute(
        pipeline,
        run_id="r1",
        context=CONTEXT,
        started_at=START,
        plugins=tools,
        tools=tools,
    )


def test_collect_calls_run_concurrently_and_are_recorded_in_declaration_order() -> None:
    count = MAX_PARALLEL_CALLS + 4
    tools = Delayed(count)
    pipeline = Pipeline.model_validate({"name": "p", "steps": [{"collect": tools.sources}]})
    record = asyncio.run(_execute(pipeline, tools))
    assert tools.started == tools.sources  # started in declaration order
    assert tools.finished != tools.sources  # but they did not finish in it
    assert 1 < tools.peak <= MAX_PARALLEL_CALLS
    expected = [ref for ref in tools.sources if ref != "slow.s1"]
    assert [snapshot.source for snapshot in record.inputs] == expected
    assert [snapshot.call for snapshot in record.inputs] == [n for n in range(count) if n != 1]
    assert [v.detail for v in record.violations] == [f"v{n}" for n in range(count) if n != 1]
    stderr = record.steps[0].stderr
    assert stderr is not None
    lines = [line for line in stderr.splitlines() if line.startswith("stderr")]
    assert lines == [f"stderr {n}" for n in range(count)]
    assert record.steps[0].status == "degraded"


def test_a_replay_of_a_parallel_collect_is_identical() -> None:
    tools = Delayed(MAX_PARALLEL_CALLS + 2)
    steps = [{"collect": tools.sources}, {"transform": ["slow.t"]}]
    pipeline = Pipeline.model_validate({"name": "p", "steps": steps})
    record = asyncio.run(_execute(pipeline, tools))
    result = replay(record, pipeline, plugins=tools)
    assert result.identical, result


def test_a_call_timeout_reaches_the_tool_caller_and_changes_the_digest() -> None:
    tools = Delayed(1)
    explicit = {"tool": "slow.s0", "params": {"a": 1}, "timeout_s": 5}
    steps = [{"collect": [explicit, {"tool": "slow.s0", "params": {"a": 2}}]}]
    pipeline = Pipeline.model_validate({"name": "p", "steps": steps})
    asyncio.run(_execute(pipeline, tools))
    assert tools.timeouts == [5.0, None]
    assert [c.timeout_s for c in planned_calls(pipeline, CONTEXT)] == [5.0, None]

    def digest(call: Any) -> str:
        return pipeline_digest(
            Pipeline.model_validate({"name": "p", "steps": [{"collect": [call]}]})
        )

    mapping = digest({"slow.s0": {"a": 1}})
    assert digest({"tool": "slow.s0", "params": {"a": 1}}) == mapping
    assert digest(explicit) != mapping
    assert digest({"tool": "slow.s0"}) == digest("slow.s0")


@pytest.mark.parametrize(
    "call",
    [
        {"tool": "slow.s0", "timeout_s": 0.5},
        {"tool": "slow.s0", "timeout_s": 601},
        {"tool": "slow.s0", "timeout_s": "5"},
        {"tool": "slow", "params": {}},
        {"tool": "slow.s0", "params": {}, "extra": 1},
        {"slow.s0": {}, "timeout_s": 5},
    ],
)
def test_invalid_explicit_calls_are_rejected(call: Any) -> None:
    with pytest.raises(ValueError, match="validation error"):
        Pipeline.model_validate({"name": "p", "steps": [{"collect": [call]}]})


def test_the_explicit_form_is_a_tool_call() -> None:
    pipeline = Pipeline.model_validate(
        {"name": "p", "steps": [{"collect": [{"tool": "slow.s0", "timeout_s": 3}]}]}
    )
    call = pipeline.steps[0].collect[0]  # type: ignore[union-attr]
    assert call == ToolCall(tool="slow.s0", timeout_s=3.0)


def test_a_misbehaving_host_violation_list_is_ignored() -> None:
    tools = FixturePlugins()
    tools.add(_manifest(["slow.x"]))
    tools.handlers["slow.x"] = lambda p, i: ToolResult(data=1, violations=["nope"])  # type: ignore[arg-type]
    pipeline = Pipeline.model_validate({"name": "p", "steps": [{"collect": ["slow.x"]}]})
    record = asyncio.run(_execute(pipeline, tools))
    assert record.status == "ok"
    assert record.violations == []
    tools.handlers["slow.x"] = lambda p, i: ToolResult(data=1, violations="nope")  # type: ignore[arg-type]
    record = asyncio.run(_execute(pipeline, tools))
    assert record.violations == []
