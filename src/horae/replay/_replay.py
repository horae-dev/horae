# SPDX-License-Identifier: Apache-2.0
"""`replay`: re-execute a recorded run from its record and compare what it proposes."""

import asyncio
import datetime
import difflib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from horae.contracts.pipeline import LlmStep, Pipeline
from horae.contracts.run import RunRecord
from horae.engine.canonical import pipeline_digest
from horae.engine.executor import execute
from horae.engine.ports import PluginRegistry, ResolvedPlugin
from horae.replay._tape import (
    NotReplayable,
    RecordedLlm,
    RecordedToolCaller,
    recorded_previous,
)

Outcome = Literal["identical", "different", "error"]

RESULT_SCHEMA_VERSION = 1
"""Version of `ReplayResult.to_dict()` (the `horae replay --json` report)."""


@dataclass(frozen=True)
class FieldDiff:
    """One summary field that differs: `field` is a dotted path such as `steps.2.status`."""

    field: str
    recorded: Any
    replayed: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "recorded": self.recorded, "replayed": self.replayed}


@dataclass(frozen=True)
class ReplayResult:
    """What a replay found. `outcome` is `identical`, `different` or `error`."""

    run_id: str
    pipeline: str
    outcome: Outcome
    recorded_digest: str
    current_digest: str
    against_current: bool = False
    intents_diff: str = ""
    """Unified diff of the canonical pretty JSON of the recorded and replayed intents
    (routing fields included); empty when they are equal."""
    summary: tuple[FieldDiff, ...] = ()
    """Status, step and LLM summary fields that differ."""
    error: str | None = None
    """Why the replay could not compare (pipeline changed, mismatch, not replayable)."""
    mismatches: tuple[str, ...] = ()
    """Calls the record could not answer (each also failed in the replayed run)."""
    notes: tuple[str, ...] = ()
    """Where the replay served a recorded outcome instead of recomputing it."""
    replayed: RunRecord | None = field(default=None, compare=False)
    """The replayed record (not persisted)."""

    @property
    def identical(self) -> bool:
        return self.outcome == "identical"

    @property
    def digest_changed(self) -> bool:
        return self.recorded_digest != self.current_digest

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON data, for `--json`."""
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "pipeline": self.pipeline,
            "outcome": self.outcome,
            "identical": self.identical,
            "pipeline_digest": {
                "recorded": self.recorded_digest,
                "current": self.current_digest,
                "changed": self.digest_changed,
            },
            "against_current": self.against_current,
            "error": self.error,
            "mismatches": list(self.mismatches),
            "notes": list(self.notes),
            "intents_diff": self.intents_diff,
            "summary": [diff.to_dict() for diff in self.summary],
        }

    def render(self) -> str:
        """A terminal report."""
        lines: list[str] = []
        if self.error is not None:
            lines.append(f"error: {self.error}")
        lines += [f"mismatch: {m}" for m in self.mismatches]
        lines += [f"note: {n}" for n in self.notes]
        if self.digest_changed and self.against_current:
            lines.append("note: replayed against the current pipeline definition")
        for diff in self.summary:
            lines.append(
                f"{diff.field}: {json.dumps(diff.recorded)} -> {json.dumps(diff.replayed)}"
            )
        if self.intents_diff:
            lines.append(self.intents_diff.rstrip("\n"))
        verdict = {
            "identical": "identical: the replay proposes the same intents",
            "different": "different: the replay differs from the recorded run",
            "error": "error: the replay could not be compared",
        }[self.outcome]
        lines.append(f"run {self.run_id} ({self.pipeline}): {verdict}")
        return "\n".join(lines)


def intents_json(record: RunRecord) -> str:
    """The run's intents (with their routing fields) as canonical pretty JSON."""
    data = [intent.model_dump(mode="json", exclude_none=True) for intent in record.intents]
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def diff_intents(recorded: RunRecord, replayed: RunRecord) -> str:
    """Unified diff of `intents_json` of both runs; empty when they are equal."""
    before, after = intents_json(recorded), intents_json(replayed)
    if before == after:
        return ""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"recorded/{recorded.id}/intents.json",
            tofile=f"replay/{replayed.id}/intents.json",
        )
    )


def _summary(record: RunRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": record.status,
        "input_hash": record.input_hash,
        "steps": len(record.steps),
        "inputs": len(record.inputs),
        "llm_calls": len(record.llm),
        "intents": len(record.intents),
        "plugins": {
            name: version.model_dump(mode="json") for name, version in record.plugins.items()
        },
    }
    for index, step in enumerate(record.steps):
        out[f"steps.{index}.kind"] = step.kind
        out[f"steps.{index}.status"] = step.status
        out[f"steps.{index}.error_kind"] = step.error.kind if step.error else None
    return out


def diff_summary(recorded: RunRecord, replayed: RunRecord) -> tuple[FieldDiff, ...]:
    """Status, step kinds, statuses and error kinds, LLM call count, plugin versions."""
    before, after = _summary(recorded), _summary(replayed)
    keys = list(before) + [key for key in after if key not in before]
    return tuple(
        FieldDiff(key, before.get(key), after.get(key))
        for key in keys
        if before.get(key) != after.get(key)
    )


@dataclass(frozen=True)
class _ResolveOnly:
    """The caller's registry, reduced to resolution: nothing that calls a tool is reachable."""

    _lookup_tool: Callable[[str], ResolvedPlugin]
    _lookup_skill: Callable[[str], ResolvedPlugin]

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        return self._lookup_tool(ref)

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        return self._lookup_skill(skill)


def _frozen(at: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: at


async def replay_async(
    record: RunRecord,
    pipeline: Pipeline,
    *,
    plugins: PluginRegistry,
    models: Mapping[str, str] | None = None,
    against_current: bool = False,
    clock: Callable[[], datetime.datetime] | None = None,
) -> ReplayResult:
    """`replay`, as a coroutine."""
    current = pipeline_digest(pipeline)

    def result(outcome: Outcome, **kwargs: Any) -> ReplayResult:
        return ReplayResult(
            run_id=record.id,
            pipeline=record.pipeline,
            outcome=outcome,
            recorded_digest=record.pipeline_digest,
            current_digest=current,
            against_current=against_current,
            **kwargs,
        )

    if pipeline.name != record.pipeline:
        return result("error", error=f"run {record.id} is a run of {record.pipeline}")
    if current != record.pipeline_digest and not against_current:
        return result(
            "error",
            error=(
                f"pipeline {record.pipeline} changed since the run (recorded digest "
                f"{record.pipeline_digest[:12]}, current {current[:12]}); replay against the "
                "current definition with --against-current"
            ),
        )
    try:
        tools = RecordedToolCaller(record, pipeline, record.context)
    except NotReplayable as unsupported:
        return result("error", error=f"not replayable: {unsupported}")
    has_llm = any(isinstance(step, LlmStep) for step in pipeline.steps)
    llm = RecordedLlm(record) if has_llm else None
    replayed = await execute(
        pipeline,
        run_id=record.id,
        context=record.context,
        started_at=record.started_at,
        plugins=_ResolveOnly(plugins.plugin_for_tool, plugins.plugin_for_skill),
        tools=tools,
        llm=llm,
        dry_run=record.dry_run,
        previous=recorded_previous(record),
        models=models,
        clock=clock or _frozen(record.started_at),
        monotonic=lambda: 0.0,
    )
    mismatches = (*tools.mismatches, *(llm.mismatches if llm else ()))
    intents_diff = diff_intents(record, replayed)
    summary = diff_summary(record, replayed)
    common: dict[str, Any] = {
        "intents_diff": intents_diff,
        "summary": summary,
        "mismatches": mismatches,
        "notes": tuple(tools.notes),
        "replayed": replayed,
    }
    if mismatches:
        why = f"{len(mismatches)} call(s) could not be answered from the record"
        return result("error", error=why, **common)
    return result("different" if intents_diff or summary else "identical", **common)


def replay(
    record: RunRecord,
    pipeline: Pipeline,
    *,
    plugins: PluginRegistry,
    models: Mapping[str, str] | None = None,
    against_current: bool = False,
    clock: Callable[[], datetime.datetime] | None = None,
) -> ReplayResult:
    """Re-execute `record` with `pipeline` from the record alone, and compare.

    - `plugins` is used for resolution only (manifests, permissions, versions): replay builds
      its own ports, a `RecordedToolCaller` and a `RecordedLlm`, and never calls a tool or
      a backend.
    - `models` (alias to concrete model, from `horae.yaml`) is part of the LLM cache key: give
      the run's mapping so that a run that reused unchanged input reuses it again.
    - `against_current`: replay even if `pipeline` is not the definition that ran (its digest
      differs); otherwise that is reported as an error.
    - `clock` gives the replayed record's timestamps (default: frozen at the run's start).

    Not for use inside a running event loop: use `replay_async` there."""
    return asyncio.run(
        replay_async(
            record,
            pipeline,
            plugins=plugins,
            models=models,
            against_current=against_current,
            clock=clock,
        )
    )
