# SPDX-License-Identifier: Apache-2.0
"""`replay`: re-execute a recorded run from its record and compare what it proposes."""

import asyncio
import datetime
import difflib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from horae.contracts.pipeline import LlmStep, Pipeline
from horae.contracts.run import RunRecord, RunStatus
from horae.engine.canonical import pipeline_digest
from horae.engine.executor import execute
from horae.engine.ports import PluginRegistry, ResolvedPlugin, UnknownPlugin
from horae.replay._lock import pipeline_refs
from horae.replay._tape import (
    RecordedLlm,
    RecordedToolCaller,
    _is_reuse,
    missing_per_call,
    recorded_previous,
)

Outcome = Literal["identical", "different", "error"]
Definition = Literal["recorded", "current"]

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
class Digest:
    """A digest as recorded by the run and as computed now (`None`: not known)."""

    recorded: str | None = None
    current: str | None = None

    @property
    def changed(self) -> bool | None:
        """`None` when either side is unknown."""
        if self.recorded is None or self.current is None:
            return None
        return self.recorded != self.current

    def to_dict(self) -> dict[str, Any]:
        return {"recorded": self.recorded, "current": self.current, "changed": self.changed}


@dataclass(frozen=True)
class ReplayResult:
    """What a replay found. `outcome` is `identical`, `different` or `error`."""

    run_id: str
    pipeline: str | None
    outcome: Outcome
    pipeline_digest: Digest = Digest()
    lock_digest: Digest = Digest()
    """`horae.lock`, recorded (`RunRecord.lock_digest`) and current."""
    config_digest: Digest = Digest()
    """The `models` and `plugins` sections of `horae.yaml`, recorded and current."""
    against_current: bool = False
    definition: Definition | None = None
    """Which pipeline definition was replayed: the one the record keeps, or the current one."""
    intents_diff: str = ""
    """Unified diff of the canonical pretty JSON of the recorded and replayed intents
    (routing fields included); empty when they are equal."""
    summary: tuple[FieldDiff, ...] = ()
    """Status, step, LLM and violation summary fields that differ."""
    error: str | None = None
    """Why the replay could not compare (pipeline changed, mismatch, not replayable…)."""
    mismatches: tuple[str, ...] = ()
    """Calls the record could not answer (each also failed in the replayed run)."""
    notes: tuple[str, ...] = ()
    """What the reader should know: a changed definition or instance replayed anyway, an
    undone run's status…"""
    replayed: RunRecord | None = field(default=None, compare=False)
    """The replayed record (not persisted)."""

    @property
    def identical(self) -> bool:
        return self.outcome == "identical"

    @property
    def digest_changed(self) -> bool:
        """The pipeline definition changed since the run."""
        return self.pipeline_digest.changed is True

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON data, for `--json`: always every key (`null` or empty when unknown)."""
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "pipeline": self.pipeline,
            "outcome": self.outcome,
            "identical": self.identical,
            "against_current": self.against_current,
            "definition": self.definition,
            "pipeline_digest": self.pipeline_digest.to_dict(),
            "lock_digest": self.lock_digest.to_dict(),
            "config_digest": self.config_digest.to_dict(),
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
        lines.append(f"run {self.run_id} ({self.pipeline or '?'}): {verdict}")
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


_RANK: dict[RunStatus, int] = {"ok": 0, "noop": 1, "degraded": 2, "failed": 3}


def status_before_undo(record: RunRecord) -> RunStatus:
    """The status the engine gave the run, derived from its steps (as the engine derives it):
    an `undone` record keeps no other trace of it."""
    if record.status != "undone":
        return record.status
    status: RunStatus = "ok"
    for step in record.steps:
        effect: RunStatus = step.status if step.status in ("degraded", "failed") else "ok"
        if _RANK[effect] > _RANK[status]:
            status = effect
    if status == "ok" and not record.intents and any(_is_reuse(c) for c in record.llm):
        status = "noop"
    return status


def _summary(record: RunRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status_before_undo(record),
        "input_hash": record.input_hash,
        "steps": len(record.steps),
        "inputs": len(record.inputs),
        "llm_calls": len(record.llm),
        "intents": len(record.intents),
        "violations": [violation.kind for violation in record.violations],
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
    """Status (an `undone` run's as before the undo), step kinds, statuses and error kinds,
    counts, violation kinds and plugin versions."""
    before, after = _summary(recorded), _summary(replayed)
    keys = list(before) + [key for key in after if key not in before]
    return tuple(
        FieldDiff(key, before.get(key), after.get(key))
        for key in keys
        if before.get(key) != after.get(key)
    )


class _Snapshot:
    """The caller's registry, resolved once, up front, for every ref of the pipeline: the
    engine only looks the results up. Nothing that calls a tool is reachable."""

    def __init__(self, plugins: PluginRegistry, pipeline: Pipeline) -> None:
        self._tools: dict[str, ResolvedPlugin] = {}
        self._skills: dict[str, ResolvedPlugin] = {}
        for kind, refs in pipeline_refs(pipeline).items():
            for ref in sorted(refs):
                try:
                    if kind == "skills":
                        self._skills[ref] = plugins.plugin_for_skill(ref)
                    else:
                        self._tools[ref] = plugins.plugin_for_tool(ref)
                except UnknownPlugin:
                    continue  # the engine fails its plan with `plugin_unknown`, as live

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        try:
            return self._tools[ref]
        except KeyError:
            raise UnknownPlugin(ref) from None

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        try:
            return self._skills[skill]
        except KeyError:
            raise UnknownPlugin(skill) from None


def _frozen(at: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: at


class _Refused(Exception):
    """The replay cannot compare; the message says why."""


def recorded_definition(record: RunRecord) -> Pipeline | None:
    """The pipeline definition the record keeps (`pipeline_definition`), or `None`.

    Raises `ValueError` when it does not match the record's `pipeline_digest`."""
    if record.pipeline_definition is None:
        return None
    try:
        pipeline = Pipeline.model_validate(record.pipeline_definition)
    except ValidationError:  # pragma: no cover (RunRecord already validates it)
        raise ValueError("the recorded pipeline definition is invalid") from None
    if pipeline_digest(pipeline) != record.pipeline_digest:
        raise ValueError("the recorded pipeline definition does not match its digest")
    return pipeline


def definition_for(
    record: RunRecord, current: Pipeline | None, against_current: bool
) -> tuple[Pipeline | None, Definition | None]:
    """The definition a replay runs: the current one with `against_current`, else the one the
    record keeps, else the current one (when its digest is the recorded one). `(None, None)`
    when there is none to run (the replay then says why)."""
    if against_current:
        return (current, "current") if current is not None else (None, None)
    try:
        recorded = recorded_definition(record)
    except ValueError:
        return None, None
    if recorded is not None:
        return recorded, "recorded"
    if current is not None and pipeline_digest(current) == record.pipeline_digest:
        return current, "current"
    return None, None


def _choose(
    record: RunRecord, current: Pipeline | None, against_current: bool, notes: list[str]
) -> tuple[Pipeline, Definition]:
    name = record.pipeline
    if current is not None and current.name != name:
        raise _Refused(f"run {record.id} is a run of {name}, not of {current.name}")
    if against_current:
        if current is None:
            raise _Refused(f"--against-current: pipeline {name} is not in the instance")
        if pipeline_digest(current) != record.pipeline_digest:
            notes.append(f"pipeline {name} changed since the run: replayed its current definition")
        return current, "current"
    try:
        recorded = recorded_definition(record)
    except ValueError as invalid:
        raise _Refused(f"not replayable: {invalid}") from None
    if recorded is not None:
        if current is not None and pipeline_digest(current) != record.pipeline_digest:
            notes.append(
                f"pipeline {name} changed since the run: replayed the recorded definition "
                "(--against-current replays the current one)"
            )
        return recorded, "recorded"
    if current is None:
        raise _Refused(
            f"the record keeps no pipeline definition and pipeline {name} is not in the instance"
        )
    digest = pipeline_digest(current)
    if digest != record.pipeline_digest:
        raise _Refused(
            f"pipeline {name} changed since the run (recorded digest "
            f"{record.pipeline_digest[:12]}, current {digest[:12]}) and the record keeps no "
            "definition; replay against the current definition with --against-current"
        )
    return current, "current"


def _drift(digests: list[tuple[str, Digest]], against_current: bool, notes: list[str]) -> None:
    for what, digest in digests:
        if digest.changed is not True or digest.recorded is None or digest.current is None:
            continue
        detail = f"recorded digest {digest.recorded[:12]}, current {digest.current[:12]}"
        if not against_current:
            raise _Refused(
                f"{what} changed since the run ({detail}); replay against the current instance "
                "with --against-current"
            )
        notes.append(f"{what} changed since the run ({detail}): replayed with the current one")


async def replay_async(
    record: RunRecord,
    pipeline: Pipeline | None = None,
    *,
    plugins: PluginRegistry,
    models: Mapping[str, str] | None = None,
    against_current: bool = False,
    lock_digest: str | None = None,
    config_digest: str | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
) -> ReplayResult:
    """`replay`, as a coroutine."""
    current = None if pipeline is None else pipeline_digest(pipeline)
    base: dict[str, Any] = {
        "run_id": record.id,
        "pipeline": record.pipeline,
        "pipeline_digest": Digest(record.pipeline_digest, current),
        "lock_digest": Digest(record.lock_digest, lock_digest),
        "config_digest": Digest(record.config_digest, config_digest),
        "against_current": against_current,
    }
    notes: list[str] = []
    try:
        chosen, definition = _choose(record, pipeline, against_current, notes)
        base["definition"] = definition
        _drift(
            [
                ("horae.lock", base["lock_digest"]),
                ("horae.yaml (models, plugins)", base["config_digest"]),
            ],
            against_current,
            notes,
        )
        missing = missing_per_call(record)
        if missing is not None:
            raise _Refused(f"not replayable: {missing} (recorded before #23)")
    except _Refused as refused:
        return ReplayResult(outcome="error", error=str(refused), notes=tuple(notes), **base)
    if record.status == "undone":
        before = status_before_undo(record)
        notes.append(f"the run was undone: its status is compared as {before}, as before the undo")
    used_models = record.models
    if used_models is None and models is not None:
        notes.append("the record keeps no model targets: replayed with the given ones")
        used_models = dict(models)
    tools = RecordedToolCaller(record, chosen, record.context)
    has_llm = any(isinstance(step, LlmStep) for step in chosen.steps)
    llm = RecordedLlm(record) if has_llm else None
    replayed = await execute(
        chosen,
        run_id=record.id,
        context=record.context,
        started_at=record.started_at,
        plugins=_Snapshot(plugins, chosen),
        tools=tools,
        llm=llm,
        dry_run=record.dry_run,
        previous=recorded_previous(record),
        models=used_models,
        lock_digest=record.lock_digest,
        config_digest=record.config_digest,
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
        "notes": tuple(notes),
        "replayed": replayed,
        **base,
    }
    if mismatches:
        why = f"{len(mismatches)} call(s) could not be answered from the record"
        return ReplayResult(outcome="error", error=why, **common)
    return ReplayResult(outcome="different" if intents_diff or summary else "identical", **common)


def replay(
    record: RunRecord,
    pipeline: Pipeline | None = None,
    *,
    plugins: PluginRegistry,
    models: Mapping[str, str] | None = None,
    against_current: bool = False,
    lock_digest: str | None = None,
    config_digest: str | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
) -> ReplayResult:
    """Re-execute `record` from the record alone, and compare.

    - `pipeline` is the **current** definition, if the caller has one. The replay runs the
      definition the record keeps (`pipeline_definition`) unless `against_current`; without a
      recorded definition it runs `pipeline` if its digest is the recorded one, else it reports
      an error ("pipeline changed").
    - `against_current`: replay through `pipeline` even if it changed, and report a changed
      `horae.lock` or `horae.yaml` as a note instead of an error.
    - `plugins` is used for resolution only (manifests, permissions, versions), once, before
      the replay starts: replay builds its own ports, a `RecordedToolCaller` and a
      `RecordedLlm`, and never calls a tool or a backend.
    - `models` (alias to concrete model) is used only when the record keeps none
      (`RunRecord.models`): it is part of the LLM cache key.
    - `lock_digest` / `config_digest`: the current instance's (`horae.config.Instance`), to
      compare with the recorded ones; `None` skips the check.
    - `clock` gives the replayed record's timestamps (default: frozen at the run's start).

    Not for use inside a running event loop: use `replay_async` there."""
    return asyncio.run(
        replay_async(
            record,
            pipeline,
            plugins=plugins,
            models=models,
            against_current=against_current,
            lock_digest=lock_digest,
            config_digest=config_digest,
            clock=clock,
        )
    )
