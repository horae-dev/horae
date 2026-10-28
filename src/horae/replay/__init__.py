# SPDX-License-Identifier: Apache-2.0
"""Replay a recorded run from its record alone (#23, PLAN.md §2.1 tenet 6).

`replay(record, pipeline, plugins=...)` re-executes `pipeline` with the engine
(`horae.engine.execute`) and compares the replayed run with the recorded one.

**No external service is reachable.** Replay builds its own ports and accepts no tool caller
or LLM backend: a `RecordedToolCaller` answers `collect` / `transform` calls and a
`RecordedLlm` answers the LLM step, both from the record only. The caller's `PluginRegistry` is
wrapped so that only its two resolution methods are reachable (it is used for manifests,
permissions, versions and commits). `act` / `notify` entries are never called by the engine.

**Same run.** The replay uses the record's run id, `context` (`now`, time zone, `vars`, so
templates render the same), `started_at` and `dry_run`. Clocks are injected: timestamps are
frozen at the run's start (or the caller's `clock`) and the monotonic clock is constant, so
nothing depends on the wall clock.

**Matching.** Each tool call is matched in order against the pipeline's planned calls, by
`(step index, call index, tool ref, canonical JSON of the rendered params)`:

- a `collect` source is answered with its `InputSnapshot` (same step, tool and params), or
  with its recorded failure (`steps[i].output` lists failed sources with their `error_kind`),
  and the intents the record attributes to that tool in that step;
- a `transform` step's last call is answered with the step's recorded output (and its intents);
  a failed step is answered with its recorded error at the failing call;
- the LLM step is answered by attempt from `RunRecord.llm` (output, raw intents, usage, cache
  flag, the request as recorded); the request's skill, model alias, input and repair errors must
  match when the engine recorded its own request shape. An attempt that the record ends with a
  failure (backend error, timeout…) is answered with that failure and its cost;
- a run that reused the previous output on unchanged input (an `LlmCall` with
  `reused_input_hash`) is replayed with that output as `PreviousLlm`, so the replay takes the
  same path, provided `models` gives the same alias targets (the cache key includes them).

A call the record cannot answer is a **mismatch**: it fails in the replayed run (kind
`replay_mismatch`) and the result's outcome is `error`; it is never a live call.

**Comparison.** `outcome` is `identical` when the canonical pretty JSON of the intents
(routing fields included) and the summary (run status, input hash, step kinds, statuses and
error kinds, counts, plugin versions) are equal, `different` otherwise, with a unified diff
(`intents_diff`) and the summary fields that differ (`summary`). Messages, timings, `stderr`
and cost are not compared.

**Pipeline changes.** The record keeps the pipeline's digest, not its definition. If the
current definition's digest differs, the outcome is `error` ("pipeline changed"), unless
`against_current=True`: the recorded data is then replayed through the current definition,
which shows how an edit (routing, act / notify entries, permissions) changes the intents.
Sources must still be called with recorded params in the same step; a transform step's
recorded output is served whatever its tools now are.

**Gaps in the recorded data** (what an exact replay cannot recover from a `RunRecord`):

- the pipeline definition itself (only `pipeline_digest`);
- the raw intents a source or transform proposed: only the validated, routed intents are kept,
  and only for steps whose entries ran. Replay re-proposes those (targets filled). Intents of
  a tool used twice in one step are attributed to its first call. A transform step that failed
  on its intents is served its recorded error (noted); a `collect` step that failed on its
  intents is not replayable (its sources' data is not recorded either);
- intermediate outputs of a multi-call `transform` step (only the final one is recorded);
- the order of successful and failed sources within a `collect` step, when one tool is called
  twice with the same params;
- the model alias targets (`models`) used in the LLM cache key, and the plugin manifests
  (`provides`, permissions as installed): the caller supplies both;
- the schema errors of an invalid LLM response, unless the repair attempt recorded them;
  per-call `stderr` and failure messages longer than their recorded excerpt.
"""

from horae.replay._lock import LockRegistry
from horae.replay._replay import (
    RESULT_SCHEMA_VERSION,
    FieldDiff,
    ReplayResult,
    diff_intents,
    diff_summary,
    intents_json,
    replay,
    replay_async,
)
from horae.replay._tape import (
    MISMATCH,
    NotReplayable,
    RecordedLlm,
    RecordedToolCaller,
    planned_calls,
    recorded_previous,
)

__all__ = [
    "MISMATCH",
    "RESULT_SCHEMA_VERSION",
    "FieldDiff",
    "LockRegistry",
    "NotReplayable",
    "RecordedLlm",
    "RecordedToolCaller",
    "ReplayResult",
    "diff_intents",
    "diff_summary",
    "intents_json",
    "planned_calls",
    "recorded_previous",
    "replay",
    "replay_async",
]
