# SPDX-License-Identifier: Apache-2.0
"""Replay a recorded run from its record alone (#23, PLAN.md §2.1 tenet 6).

`replay(record, current, plugins=...)` re-executes the run with the engine
(`horae.engine.execute`) and compares the replayed run with the recorded one.

**No external service is reachable.** Replay builds its own ports and accepts no tool caller
or LLM backend: a `RecordedToolCaller` answers `collect` / `transform` calls and a
`RecordedLlm` answers the LLM step, both from the record only. The caller's `PluginRegistry`
is asked once, before the run, to resolve every tool and skill of the pipeline; the engine
then sees only that frozen snapshot. `act` / `notify` entries are never called by the engine.
`horae replay` resolves with a `LockRegistry` (`horae.lock` and the record), never with a
live plugin host.

**Same run.** The replay uses the record's run id, `context` (`now`, time zone, `vars`, so
templates render the same), `started_at`, `dry_run` and model alias targets (`models`).
Clocks are injected: timestamps are frozen at the run's start (or the caller's `clock`) and the
monotonic clock is constant, so nothing depends on the wall clock.

**Which definition.** The record keeps the canonical pipeline definition
(`pipeline_definition`), so a run replays as it ran even after the pipeline file was edited
(noted). With `against_current=True` the recorded data is replayed through the *current*
definition, which shows how an edit (routing, act / notify entries, permissions) changes the
intents. A record without a definition replays only through a current definition with the
same digest. A changed `horae.lock` or `horae.yaml` (`models`, `plugins`), seen from
`lock_digest` / `config_digest`, is an error, or a note with `against_current`.

**Matching** is per call, by position. Each tool call is matched in order against the
pipeline's planned calls (`horae.engine.planned_calls`, the engine's own rendering) and must
have the recorded tool and canonical params at the same `(step, call)`:

- a `collect` source is answered with its `InputSnapshot` (data and the raw intents it
  proposed) or its failed-source entry (`error_kind`, and its message line);
- a `transform` call is answered from `StepRecord.calls` (raw intents or failure; the step's
  final output for its last call). The first call of a step must be given the input whose
  hash the record keeps;
- the LLM step is answered by attempt from `RunRecord.llm`. Whatever the backend recorded as
  `request`, the engine-owned `model_alias`, `input_hash` and `repair` must match, and the
  recorded `schema_errors` are served. An attempt that the record ends with a failure (backend
  error, timeout…) is answered with that failure and its cost;
- a run that reused the previous output on unchanged input is replayed with that output as
  `PreviousLlm`, so the replay takes the same path (the recorded `models` keep the cache key).

The engine re-validates and re-routes every raw proposal, so a rejected or unroutable intent
fails the replayed step exactly as it failed the run. A call the record cannot answer is a
**mismatch**: it fails in the replayed run (kind `replay_mismatch`) and the outcome is `error`;
it is never a live call.

**Comparison.** `outcome` is `identical` when the canonical pretty JSON of the intents
(routing fields included) and the summary (run status, input hash, step kinds, statuses and
error kinds, counts, violation kinds, plugin versions) are equal, `different` otherwise, with a
unified diff (`intents_diff`) and the summary fields that differ (`summary`). An `undone` run's
status is compared as it was before the undo, derived from its steps (noted). Messages,
timings, `stderr` and cost are not compared.

**Limits** (what a replay does not reproduce):

- per-call `stderr`, and failure messages beyond their recorded excerpt (a replayed message
  may differ; messages are not compared);
- intermediate outputs of a multi-call `transform` step: only the final output and each
  call's input hash are recorded, so only the first call's input is checked;
- the plugin host's own findings (`secret_in_output` violations, #24): they describe what a
  live process returned, which the record keeps only redacted, so they are left out of the
  compared violation kinds;
- records written before the per-call fields existed (`InputSnapshot.call`,
  `StepRecord.calls`, the `LlmCall` engine fields) are not replayable.
"""

from horae.replay._lock import LockRegistry, pipeline_refs
from horae.replay._replay import (
    RESULT_SCHEMA_VERSION,
    Digest,
    FieldDiff,
    ReplayResult,
    definition_for,
    diff_intents,
    diff_summary,
    intents_json,
    recorded_definition,
    replay,
    replay_async,
    status_before_undo,
)
from horae.replay._tape import (
    MISMATCH,
    RecordedLlm,
    RecordedToolCaller,
    missing_per_call,
    recorded_previous,
)

__all__ = [
    "MISMATCH",
    "RESULT_SCHEMA_VERSION",
    "Digest",
    "FieldDiff",
    "LockRegistry",
    "RecordedLlm",
    "RecordedToolCaller",
    "ReplayResult",
    "definition_for",
    "diff_intents",
    "diff_summary",
    "intents_json",
    "missing_per_call",
    "pipeline_refs",
    "recorded_definition",
    "recorded_previous",
    "replay",
    "replay_async",
    "status_before_undo",
]
