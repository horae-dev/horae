# SPDX-License-Identifier: Apache-2.0
"""Execute a validated `Pipeline` and produce its `RunRecord` (PLAN.md §2.1, §3.1, §3.2).

**Control flow is the definition.** Before the first call the engine builds a fixed plan from
the pipeline: every step, every call, every plugin resolved and every parameter rendered
(`horae.engine.template`). It then walks that plan in order. Nothing a tool or the LLM
returns is ever read as an instruction: a result that looks like steps, a "next step" or a
template is plain data. The only deviation from "every step, in order" is the fixed policy
below, which can stop the run early (the remaining steps are recorded as `skipped`) or skip an
`act` step; it can never add, repeat or reorder a step.

**Concurrency.** The calls of a `collect` step run concurrently, at most `MAX_PARALLEL_CALLS`
in flight, **started in declaration order** (a call starts only once the previous one has
started). Their results are then processed one by one in declaration order, exactly as if they
had run sequentially: snapshots, failed sources, stderr, violations and intents are recorded in
declaration order whatever the order in which the calls finished, so the record (and a
replay) does not depend on timing. Every source of the step is called and recorded, even when
an earlier one's proposals then fail the step. `transform` calls run one at a time (each is
fed the previous output), and steps run one after the other.

**Releasing plugins.** When `tools` has a `release` method (`ReleasingToolCaller`), each
plugin is released right after the last step of the plan that calls it (or as soon as the run
stops early), so its process does not outlive its use (schemas decision 35 A).

**Who proposes, who applies (ADR 0002).** `collect` sources, `transform` tools and the `llm`
step **propose** intents. `act` and `notify` entries are never called here: each entry
(`<sink>.<tool>: {params}`) names the sink or notifier that will **apply** the intents routed
to it, later (#21/#22). `execute` therefore calls no tool that writes, dry run or not.

**Planning** fails the run before any plugin is called, with the offending step `failed`
and every other step `skipped`, when:

- a tool or skill resolves to no plugin (`plugin_unknown`), or the plugin does not list it
  under the `provides` entry of the step kind (`tool_not_provided`: `collect` uses `sources`,
  `transform` `transforms`, `act` `sinks`, `notify` `notifiers`, `llm` `skills`);
- a parameter template is invalid (`template_error`), or a rendered parameter is not plain
  JSON data (`params_invalid`: e.g. `NaN`, a date object, nesting deeper than 64);
- a template variable (`horae run --var`) or a rendered parameter holds the value (or an
  encoding) of a secret this process resolved, in a string, an object key or as a number's
  whole value (`pin: 12345678`) (`params_invalid`, naming the secret only, #53, schemas
  decision 43 D): a secret is never a trusted parameter. The variable's value is redacted in
  the record's `context`. The same holds for an `llm` step's skill: a prompt or output
  schema (loaded through the `SkillSource`) holding a secret is `params_invalid`;
- an `act` entry has no valid `mode`, or its binding parameters (or a `notify` entry's
  `link_prefix`) are invalid (`params_invalid`, see below);
- the pipeline has more than `MAX_ITEMS` `collect` calls in all, or uses more than
  `MAX_ITEMS` plugins (`plan_too_large`: the run record could not hold them);
- the pipeline has an `llm` step and no backend was given (`llm_unavailable`).

**Data between steps.** One JSON value flows through the run:

- `collect` calls each source with `input=None` (concurrently); its output is the list
  `[{"source": <ref>, "params": {...}, "data": <output>}, ...]` of the sources that succeeded,
  in declaration order, each also recorded as an `InputSnapshot` with its position (`call`)
  and the raw intents it proposed (a later `collect` step starts a new list; a source whose
  proposals fail the step is recorded too). The step record's `output` lists the sources that
  failed, `[{"source", "call", "params", "error_kind"}]` (`None` when none did);
- each `transform` receives the current value and its output replaces it (a fold); each call
  is recorded in `StepRecord.calls` (position, ref, params, the SHA-256 of its input, its raw
  intents or its error kind), the step's `output` is the final value;
- `llm` receives the current value and its schema-valid output replaces it;
- `act` and `notify` do not change it.

Every value is copied through JSON at each boundary, so a tool cannot change data it handed
over or was handed. A result must be plain JSON data (objects with string keys, arrays,
strings, integers, finite floats, booleans, `null`; no lone surrogate), nested at most
`MAX_DEPTH` deep (`output_invalid`), and at most `MAX_OUTPUT_BYTES` as compact UTF-8 JSON
(`output_too_large`).

**Intents: validation and routing.** Each proposing call's batch is checked with
`horae.intents.validate_intents` against the proposer's own `permissions.intents` (`source`
is the plugin name, or `skill:<skill>` for the LLM step, whose permissions are those of the
plugin shipping the skill). A rejected `http.post` host is also recorded as an
`egress_blocked` violation. Then every intent is **routed** to exactly one `act` / `notify`
entry of a later step, which is recorded in `ProposedIntent.applied_by` (the entry's tool),
`applied_by_step` and `applied_by_call`. An entry takes an intent when:

1. its plugin's `permissions.intents` lists the intent type (the sink must be allowed to apply
   it), and the type is one the entry binds: `notify` entries bind `notify.push` only; `act`
   entries bind the types of their **required** `mode` (table below);
2. the intent's target matches the entry's **trusted** binding parameter exactly, as plain
   strings (no case folding, Unicode, path or URL normalisation: `Notes/A.md` for `Notes/a.md`,
   an NFD spelling of an NFC path, or `…/x?a=1` for `…/x` do not match): a note intent's
   `path` must equal the
   entry's `path`, an `http.post` `url` the entry's `url`. When the proposer **omits** the
   target, the core fills it from the one entry that binds the type; if there is none or more
   than one, the intent is unrouted or ambiguous;
3. a `notify.push` with a `link` is taken only by a `notify` entry with a trusted
   `link_prefix` parameter the link falls under; without `link_prefix`, a proposed link is
   never routed. A prefix is an `https://` or `obsidian://` link with a host, no user info,
   query or fragment, and a clean path of at least one segment that **ends with `/`** (no
   empty, `.` or `..` segment, backslash, or the escapes below), e.g.
   `https://notes.example.org/daily/` or
   `obsidian://vault/Main/`. Both are parsed with `urllib.parse.urlsplit`: the link must have
   the same scheme and exactly the same netloc, and its path must start with the prefix's
   path; the rest of the path may have no empty, `.` or `..` segment, backslash, or `%2e`,
   `%2f`, `%5c` or `%25` (any case). The link may add its own query and fragment;
4. a `task.update_status` is routed only if its `task` fingerprint appears, as an exact string
   value, in the data collected so far in the run (`RunRecord.inputs`, and the current
   `collect` step's earlier sources and own output; for an `llm` step with a loaded skill,
   only its **viewed** input, what the model was shown): a proposer can change the status of
   a task it was shown, not of one it guessed. `task.create` binds no target: its title is free
   text.

An intent that no entry takes fails the step (`intent_unrouted`), as does one that several
entries take (`intent_ambiguous`): untrusted data can never pick a target outside the trusted
parameters (PLAN.md §2.1 tenet 5). This is intended: two entries that may apply the same type
to the same target (e.g. two notifiers of `notify.push`, or two `mode: tasks` sinks) make
every such intent ambiguous; give each its own target, or only one entry the type. The
proposer's permissions are checked first: a type it may not propose is `intent_rejected`
even when no entry could take it either. Other parameters of an entry (`topic`, `when`…) are
trusted metadata for the sink, recorded with the entry in the `act` / `notify` step's `output`:
`[{"call": <ref>, "params": {...}, "intents": [<indices into RunRecord.intents>]}]`.

**Untrusted text.** Routing binds where an intent goes, never what it says: note content,
task titles, notification titles and bodies and `http.post` bodies are untrusted text from
the proposer. Appliers (#21/#22) must treat them as data (never as markup to interpret,
commands, templates or further targets).

**The action guard** (#35, `horae.engine.guard`, schemas decision 46). Every batch the `llm`
step proposes (fresh or reused) is checked after validation and before routing: at most the
skill's `intent_limits` of each type, no Markdown image or Obsidian embed, no HTML tag outside
a small inert allowlist and no HTML attribute, no `javascript:` / `data:` / `file:` /
protocol-relative link in its text, and every
URL in its text (and a `notify.push` `link`) an exact string of the viewed input. A refusal
records a `guard` violation (a content-free detail) and **degrades the whole step**
(`guard_violation`): nothing it proposed is routed, its output is not used, later steps are
skipped. On a reuse, a refusal is a cache miss, and a cached entry is deleted (`discard`, when
the cache has it). Intents of `collect` and `transform` steps are not guarded (first-party
plugins, #52).

| `mode` | intent types | binding parameter |
|---|---|---|
| `create` | `note.create` | `path` (required) |
| `append-section` | `note.append_section` | `path` (required) |
| `replace-owned` | `note.replace_owned_block` | `path` (required) |
| `tasks` | `task.create`, `task.update_status` | none (fingerprint check, rule 4) |
| `post` | `http.post` | `url` (required) |

A missing or unknown `mode`, a `mode` whose types the sink may not apply, or a missing or
invalid binding parameter is a plan error (`params_invalid`). A step's intents are kept only if the
whole step is accepted, an intent enters `RunRecord.intents` only when the step of its entry
runs, and the run-wide total is capped at `MAX_RUN_INTENTS` (`run_intents_exceeded`). Nothing
is applied here: that is #21/#22, which must not apply a `failed` run.

**Replays** (#23). Besides the raw proposals above, the record keeps what `horae.replay`
needs to re-execute the run from the record alone: the canonical definition
(`pipeline_definition`), the model alias targets (`models`), the `lock_digest` and
`config_digest` the caller gives, the refs each plugin was resolved for
(`plugins[].provides`), and for each LLM attempt the engine's own view of the request
(`model_alias`, `input_hash` of the input, `repair`) and of the response (`schema_errors`,
each capped at 4096 characters, at most 256: the repair retry is sent these same errors),
whatever the backend reports as `request`. `planned_calls` is the engine's rendering of
every call, shared with replay.

**The LLM step.** Its cache key, recorded as `RunRecord.input_hash` once the output is
accepted, is the SHA-256 of the canonical JSON of `{skill, plugin: {name, version, commit},
model, input}`, where `model` is the alias's target from `models` when the caller knows it, else
the alias. With a `SkillSource` (#27), the skill is loaded while planning (`skill_invalid`
otherwise), `input` is the step input restricted to the skill's input view (what the model
sees, also what `LlmRequest.input` and `LlmCall.input_hash` hold), and the key adds
`skill_version` and `skill_digest` (prompt, output schema, view and intents), so a new version
or an edited prompt is a cache miss; `LlmCall.skill_version` and `LlmCall.skill_digest` are the
loaded skill's, and the LLM's intents are checked against the plugin's permissions narrowed to
the skill's `intents` (`intent_rejected`). A viewed
input of `null` (the input does not have the view's shape) degrades the step without calling
the LLM (`input_invalid`). The skill is loaded before the backend is checked, so an invalid
skill is `skill_invalid` even without a backend.
When `previous` (an accepted output, `PreviousLlm`, e.g. the one a replayed run reused) has the
same key, or else the `cache` (`LlmCache`, the store's `llm_cache` table under the runner, #32)
holds a live entry for the key that still passes the skill's full output schema (the cache
re-validates it), no LLM call is made: its output and intents are reused (validated and routed
again), recorded as an `LlmCall` with `cached=True`, zero usage and the request marker
`{skill, model, reused_input_hash, source_run?}` (`source_run`: the run that produced it),
and the run continues. If the reused intents no longer validate or route (e.g. an explicit
target bound to `{{date}}` moved to a new day) or the action guard refuses them, it is a cache
miss: nothing of the entry is recorded and the LLM is called (a guarded cache entry is also
deleted). Without a usable entry (or when `cache.lookup` raises: a
miss) the LLM is called. A hit calls no backend, so no usage is reserved or charged (schemas
decision 45). A reused run re-proposes the same intents, including `notify.push`: appliers
must deduplicate notifications (e.g. by `input_hash` and intent).
`timeout_s` (default `DEFAULT_LLM_TIMEOUT_S`) is one deadline for both attempts. The budget
counts what every attempt cost, including the `usage` of a failed attempt (`LlmError.usage`,
also of one whose output could not be validated, `validator_unavailable`, which is not
repaired; and of one cancelled by the deadline while its output was being validated,
`horae.engine.ports.attached_usage`);
a backend's cached response costs nothing and counts towards neither the cost nor the budget.

**De-identification** (#34, schemas decision 47). With a `deid` policy, a skill whose input
view declares field classes and a cloud model alias, the order is: input view, then
pseudonymize (`deid.pseudonymize`, deterministic from the viewed input, run in a worker thread
within the step's `timeout_s`), then the request (`LlmRequest.input` is the pseudonymized
input, `classified` and `deidentified` set; the backend renders it and refuses one that holds
a declared or resolved secret, T10), then validation of the output (the backend), then the
attempt is recorded (`llm[]`) and its usage spent, then **re-identification** of the accepted
output and raw intents (refused with `output_too_large` before substituting when the result
would pass `MAX_REIDENTIFIED_BYTES`; the call and its cost stay recorded), then the intents
are validated and routed on real values (the action guard of #35 runs after that, on real
values too). The cache key (`input_hash`) is computed on the real viewed input; the
recorded `LlmCall.input_hash`, `response` and `intents` are what the provider saw and
returned (pseudonymized), `request.deid` the summary (version, digest, counts) and the
pseudonyms left `unresolved` (unknown or mangled: left as they are, no repair); the step's
output and the routed intents carry real values. A cache hit or a replayed `previous` holds
the pseudonymized output and is re-identified the same way, but only when its
`deid_input_sha256` equals this run's de-identified input digest (`None` on both sides
without de-identification): otherwise it is a miss. A policy that cannot
de-identify (`DeidUnavailable`, e.g. an unreadable vocabulary) degrades the step
(`deid_unavailable`) before anything is sent. A request refused for a secret
(`secret_in_request`) also records a `secret_in_request` violation naming the secrets.

**Step policy** (step status, then the effect on the run; the run status is the worst of
`failed` > `degraded` > `noop` > `ok`):

| Step | Situation | Step | Run | Later steps |
|---|---|---|---|---|
| proposer | intents rejected, unrouted, ambiguous, over the cap | failed | failed | skipped |
| collect | some sources fail, at least one succeeds | degraded | degraded | run (1) |
| collect | every source fails | failed | failed | skipped |
| transform | a transform fails | failed | failed | skipped |
| llm | backend or validator error, timeout, budget, schema x2 (3) | degraded | degraded | skipped |
| llm | the action guard refuses its proposals | degraded | degraded | skipped |
| llm | unchanged input, previous output reused | ok | ok or noop (2) | run |
| act | after a degraded `collect` (`partial_input`) | skipped | (degraded) | run |
| act, notify | otherwise | ok | ok | run |

"Proposer" is any `collect`, `transform` or `llm` step. (1) On the partial data, except that
every `act` step is skipped. (2) `noop` when the run records no intent at all. (3) Also a
response that is not a valid `LlmResponse` (`output_invalid`); an output the validator could
not check (`validator_unavailable`) degrades the step at once, without a repair.

Rationale: a write is never made from partial or unvalidated data (a degraded `collect` skips
every `act` step, whose intents are dropped, while `notify` still runs: telling the user about
partial data is better than silence; `transform` and `llm` failures stop the run), reading is
best effort (PLAN.md §2.2 "degraded"), and rejected or unroutable intents are a permission or
contract breach and always fail the run (ADR 0002). The `noop` status means "nothing to do":
the input was unchanged and nothing is left to apply.

**Cancellation.** If the task running `execute` is cancelled, it raises `RunCancelled`, an
`asyncio.CancelledError` that carries the partial, schema-valid `RunRecord` (the current step
`failed` with kind `cancelled`, later steps `skipped`, run `failed`). `on_step` is called after
each step record is final, in order, so a caller can also persist steps as they finish.
"""

import asyncio
import contextlib
import copy
import datetime
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from horae.contracts.intents import Intent, IntentType, NotifyLink, NotifyPush, TaskUpdateStatus
from horae.contracts.pipeline import (
    ActStep,
    Budget,
    CollectStep,
    LlmSpec,
    LlmStep,
    NotifyStep,
    Pipeline,
    PluginCall,
    Step,
    TransformStep,
    call_parts,
)
from horae.contracts.run import (
    MAX_PIPELINE_BYTES,
    MAX_RUN_INTENTS,
    CallRecord,
    ErrorKind,
    InputSnapshot,
    LlmCall,
    PluginVersion,
    ProposedIntent,
    RunContext,
    RunRecord,
    RunStatus,
    StepError,
    StepKind,
    StepRecord,
    StepStatus,
    Usage,
    Violation,
)
from horae.contracts.types import MAX_ITEMS, HttpsUrl, NotePath, Sha256Hex, Token
from horae.engine.canonical import canonical_json, canonical_pipeline, pipeline_digest, sha256_hex
from horae.engine.guard import GUARD_ERROR, guard_for
from horae.engine.ports import (
    Deidentifier,
    DeidUnavailable,
    EngineCallError,
    LlmBackend,
    LlmCache,
    LlmError,
    LlmRequest,
    LlmResponse,
    LoadedSkill,
    PluginRegistry,
    PreviousLlm,
    Pseudonyms,
    ResolvedPlugin,
    SkillSource,
    SkillUnavailable,
    ToolCaller,
    ToolResult,
    UnknownPlugin,
    attached_usage,
)
from horae.engine.template import TemplateError, render, variables
from horae.intents import MAX_BODY_DEPTH, IntentRejected, safe_excerpt, validate_intents
from horae.secrets.redact import current as current_redactor

_log = logging.getLogger("horae.engine")

DEFAULT_LLM_TIMEOUT_S = 120.0
"""Timeout of the LLM step (both attempts) when the pipeline sets no `timeout_s`."""

MAX_LLM_ATTEMPTS = 2
"""The first call and one repair retry (PLAN.md §3.4)."""

MAX_DEPTH = MAX_BODY_DEPTH
"""Deepest nesting of arrays and objects in a tool or LLM output, and in parameters."""

MAX_OUTPUT_BYTES = 4 * 1024 * 1024
"""Largest output of one call (compact UTF-8 JSON): 4 MiB."""

MAX_PARALLEL_CALLS = 8
"""Most `collect` calls of one step in flight at once."""

ACT_MODES: dict[str, frozenset[IntentType]] = {
    "create": frozenset({"note.create"}),
    "append-section": frozenset({"note.append_section"}),
    "replace-owned": frozenset({"note.replace_owned_block"}),
    "tasks": frozenset({"task.create", "task.update_status"}),
    "post": frozenset({"http.post"}),
}
"""The intent types an `act` entry's `mode` binds."""

TARGET_FIELDS: dict[IntentType, str] = {
    "note.create": "path",
    "note.append_section": "path",
    "note.replace_owned_block": "path",
    "http.post": "url",
}
"""The intent field an `act` entry's parameter of the same name binds."""

_NOTIFY_TYPES: frozenset[IntentType] = frozenset({"notify.push"})
_LINK: TypeAdapter[str] = TypeAdapter(NotifyLink)
_LINK_SCHEMES = frozenset({"https", "obsidian"})
_UNSAFE_ESCAPES = ("%2e", "%2f", "%5c", "%25")
_BINDING_TYPES: dict[str, TypeAdapter[str]] = {
    "path": TypeAdapter(NotePath),
    "url": TypeAdapter(HttpsUrl),
}
_ERROR_KIND: TypeAdapter[str] = TypeAdapter(ErrorKind)
_DIGEST: TypeAdapter[str] = TypeAdapter(Sha256Hex)
_MODELS: TypeAdapter[dict[str, str]] = TypeAdapter(dict[Token, Token])

_MAX_MESSAGE = 4096
_MAX_STDERR = 65536
_STDERR_CUT = "\n[…]\n"
_PROVIDES = {"collect": "sources", "transform": "transforms", "act": "sinks", "notify": "notifiers"}
_EFFECT: dict[StepStatus, RunStatus] = {
    "ok": "ok",
    "skipped": "ok",
    "degraded": "degraded",
    "failed": "failed",
}
_RANK: dict[RunStatus, int] = {"ok": 0, "noop": 1, "degraded": 2, "failed": 3}
_ZERO = Usage(tokens_in=0, tokens_out=0, usd=0.0)

StepHook = Callable[[int, StepRecord], Awaitable[None]]
"""`on_step(index, record)`: awaited after each step record is final."""


class RunCancelled(asyncio.CancelledError):
    """The run was cancelled; `record` is its partial run record (status `failed`)."""

    def __init__(self, record: RunRecord) -> None:
        super().__init__("the run was cancelled")
        self.record = record


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _cap(text: str, limit: int = _MAX_MESSAGE) -> str:
    text = text or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _kind(kind: object) -> str:
    """`kind` if it is a valid `ErrorKind`, else `host_error` (a misbehaving port)."""
    try:
        return _ERROR_KIND.validate_python(kind)
    except ValidationError:
        return "host_error"


def _raw(intents: Any) -> Any:
    """Raw proposals as recorded: `None` for "none" (an empty list), anything else as is."""
    return None if isinstance(intents, list) and not intents else intents


def _intent_list(raw: Any) -> Any:
    """`None` and empty tuples mean "no intents"; a tuple is read as a list."""
    if raw is None:
        return []
    if isinstance(raw, tuple):
        return list(raw)
    return raw


# --- plain JSON data -------------------------------------------------------------------------


class _DataError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


_SCALARS = (str, int, bool, type(None))


def _plain_problem(value: object) -> tuple[str, str] | None:
    """Why `value` is not plain, bounded JSON data: `(kind, reason)`, or `None`.

    Iterative (no recursion), so any nesting is safe to check. Each value takes at least one
    byte, so more values than `MAX_OUTPUT_BYTES` is already too large."""
    stack: list[tuple[object, int]] = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > MAX_OUTPUT_BYTES:
            return "output_too_large", f"larger than {MAX_OUTPUT_BYTES} bytes"
        kind = type(item)
        if kind in _SCALARS:
            continue
        if kind is float:
            if not math.isfinite(item):  # type: ignore[arg-type]
                return "output_invalid", "contains a non-finite number"
            continue
        if kind is not dict and kind is not list:
            return "output_invalid", f"contains a <{kind.__name__}>, not a JSON value"
        if depth == MAX_DEPTH:
            return "output_invalid", f"nested deeper than {MAX_DEPTH}"
        if kind is dict:
            mapping: dict[object, object] = item  # type: ignore[assignment]
            if any(type(key) is not str for key in mapping):
                return "output_invalid", "has an object key that is not a string"
            stack.extend((child, depth + 1) for child in mapping.values())
        else:
            sequence: list[object] = item  # type: ignore[assignment]
            stack.extend((child, depth + 1) for child in sequence)
    return None


def _json_copy(value: Any, what: str = "output") -> Any:
    """A deep copy of `value` through JSON; `_DataError` if it is not plain, bounded data."""
    problem = _plain_problem(value)
    if problem is not None:
        raise _DataError(problem[0], f"the {what} {problem[1]}")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:  # lone surrogates, huge ints
        raise _DataError("output_invalid", f"the {what} is not plain JSON data") from error
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise _DataError("output_too_large", f"the {what} is larger than {MAX_OUTPUT_BYTES} bytes")
    return json.loads(encoded)


# --- plan ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    """One plugin call of a pipeline, with its parameters rendered (`planned_calls`)."""

    step: int
    """Index of the step in the pipeline."""
    call: int
    """Index of the call in its step's list of calls."""
    kind: StepKind
    ref: str
    params: dict[str, Any] | None
    """The rendered parameters; `None` when a template is invalid (see `error`)."""
    error: str | None = None
    """Why the parameters could not be rendered (`TemplateError`)."""
    timeout_s: float | None = None
    """The call's own timeout (`ToolCall.timeout_s`), passed to the `ToolCaller`."""


def planned_calls(pipeline: Pipeline, context: RunContext) -> list[PlannedCall]:
    """Every `collect`, `transform`, `act` and `notify` call of `pipeline`, in declaration order,
    with its parameters rendered from `context` exactly as `execute` renders them (it uses this
    function). Rendering is pure: nothing is resolved or called.

    `execute` calls the `collect` / `transform` calls of this list in this order (a prefix of
    them when a step stops the run, none when planning fails). Raises `TemplateError` when the
    context's variables themselves are invalid."""
    names = variables(context)
    planned: list[PlannedCall] = []
    for index, step in enumerate(pipeline.steps):
        kind = _kind_of(step)
        for position, call in enumerate(_calls_of(step)):
            ref, params, timeout_s = call_parts(call)
            try:
                rendered = render(copy.deepcopy(params), names)
            except TemplateError as error:
                planned.append(PlannedCall(index, position, kind, ref, None, str(error), timeout_s))
                continue
            planned.append(PlannedCall(index, position, kind, ref, rendered, None, timeout_s))
    return planned


@dataclass(frozen=True)
class _Call:
    ref: str
    params: dict[str, Any]
    plugin: ResolvedPlugin
    position: int = 0
    """Index of the call in its step's list of calls."""
    timeout_s: float | None = None


@dataclass(frozen=True)
class _Entry:
    """An `act` or `notify` entry: who applies which intents, bound to which targets."""

    step: int
    call: int
    ref: str
    params: dict[str, Any]
    plugin: ResolvedPlugin
    types: frozenset[IntentType]
    binds: dict[str, str]

    def takes(self, intent: Intent) -> bool:
        """Exact string comparison: no case folding, Unicode or URL normalisation."""
        if intent.type not in self.types:
            return False
        if isinstance(intent, NotifyPush):
            prefix = self.binds.get("link_prefix")
            return intent.link is None or (prefix is not None and _link_under(intent.link, prefix))
        bound = TARGET_FIELDS.get(intent.type)
        return bound is None or self.binds.get(bound) == getattr(intent, bound)


@dataclass(frozen=True)
class _Planned:
    index: int
    kind: StepKind
    calls: tuple[_Call, ...] = ()
    entries: tuple[_Entry, ...] = ()
    llm: LlmSpec | None = None
    skill_plugin: ResolvedPlugin | None = None
    skill: LoadedSkill | None = None


class _PlanError(Exception):
    def __init__(self, index: int, kind: StepKind, error: StepError) -> None:
        super().__init__(error.message)
        self.index = index
        self.kind = kind
        self.error = error


def _kind_of(step: Step) -> StepKind:
    if isinstance(step, CollectStep):
        return "collect"
    if isinstance(step, LlmStep):
        return "llm"
    if isinstance(step, ActStep):
        return "act"
    if isinstance(step, NotifyStep):
        return "notify"
    return "transform"


def _calls_of(step: Step) -> list[PluginCall]:
    if isinstance(step, CollectStep):
        return step.collect
    if isinstance(step, ActStep):
        return step.act
    if isinstance(step, NotifyStep):
        return step.notify
    if isinstance(step, TransformStep):
        return step.transform
    return []  # the llm step calls no plugin tool


def _clean_path(path: str, *, prefix: bool) -> bool:
    """A `link_prefix` path (`prefix=True`: absolute, at least one segment, ending in `/`), or
    the rest of a link's path after it: no empty (but a trailing), `.` or `..` segment, no
    backslash, no escaped dot, slash, backslash or percent sign."""
    lowered = path.lower()
    if "\\" in path or any(escape in lowered for escape in _UNSAFE_ESCAPES):
        return False
    segments = path.split("/")
    if prefix:
        if len(segments) < 3 or segments[0] != "" or segments[-1] != "":
            return False
        segments = segments[1:-1]
    elif segments[-1] == "":
        segments = segments[:-1]
    return all(segment not in ("", ".", "..") for segment in segments)


def _check_link_prefix(prefix: str) -> str:
    """`prefix` if it is a safe `link_prefix`; `ValueError` otherwise."""
    parts = urlsplit(prefix)
    if (
        parts.scheme not in _LINK_SCHEMES
        or not parts.netloc
        or "@" in parts.netloc
        or "?" in prefix
        or "#" in prefix
        or not prefix.endswith("/")
        or not _clean_path(parts.path, prefix=True)
    ):
        raise ValueError("unsafe link prefix")
    return prefix


def _link_under(link: str, prefix: str) -> bool:
    """`link` falls under `prefix` (a `_check_link_prefix`-valid prefix), compared parsed."""
    try:
        got, want = urlsplit(link), urlsplit(prefix)
    except ValueError:
        return False
    if got.scheme != want.scheme or got.netloc != want.netloc:
        return False
    if not got.path.startswith(want.path):
        return False
    return _clean_path(got.path[len(want.path) :], prefix=False)


def _entry_binding(
    kind: StepKind, ref: str, params: dict[str, Any], plugin: ResolvedPlugin
) -> tuple[frozenset[IntentType], dict[str, str]]:
    """The intent types and targets an `act` / `notify` entry binds; `ValueError` if its
    binding parameters are invalid."""
    allowed = frozenset(plugin.manifest.permissions.intents)
    binds: dict[str, str] = {}
    if kind == "notify":
        prefix = params.get("link_prefix")
        if prefix is not None:
            try:
                binds["link_prefix"] = _check_link_prefix(_LINK.validate_python(prefix))
            except (ValidationError, ValueError):
                message = "must be an https:// or obsidian:// link with a clean path ending in /"
                raise ValueError(f"{ref}: `link_prefix` {message}") from None
        return _NOTIFY_TYPES & allowed, binds
    for name, adapter in _BINDING_TYPES.items():
        if name in params:
            try:
                binds[name] = adapter.validate_python(params[name])
            except ValidationError:
                raise ValueError(f"{ref}: `{name}` is not a valid target") from None
    mode = params.get("mode")
    if not isinstance(mode, str) or mode not in ACT_MODES:
        what = "a missing" if mode is None else "an unknown"
        raise ValueError(f"{ref}: {what} `mode` (one of {', '.join(sorted(ACT_MODES))})")
    types = ACT_MODES[mode]
    if not types <= allowed:
        raise ValueError(f"{ref}: plugin may not apply mode {mode} (permissions.intents)")
    for intent_type in sorted(types):
        target = TARGET_FIELDS.get(intent_type)
        if target is not None and target not in binds:
            raise ValueError(f"{ref}: mode {mode} needs a `{target}` parameter")
    return types, binds


def _plan(
    pipeline: Pipeline,
    context: RunContext,
    plugins: PluginRegistry,
    has_llm: bool,
    skills: SkillSource | None = None,
) -> tuple[_Planned, ...]:
    planned: list[_Planned] = []
    used: set[str] = set()
    collect_calls = 0
    redactor = current_redactor()
    first = _kind_of(pipeline.steps[0])
    for name, value in sorted(context.vars.items()):
        held = redactor.names_in(value)
        if held:
            message = f"the variable {name} holds secret {', '.join(sorted(held))}"
            error = StepError(kind="params_invalid", message=f"{message} (never a parameter)")
            raise _PlanError(0, first, error)
    try:
        rendered_calls = {(c.step, c.call): c for c in planned_calls(pipeline, context)}
    except TemplateError as error:
        raise _PlanError(0, first, StepError(kind="template_error", message=str(error))) from None
    for index, step in enumerate(pipeline.steps):
        kind = _kind_of(step)

        def fail(
            error_kind: str, message: str, index: int = index, kind: StepKind = kind
        ) -> NoReturn:
            raise _PlanError(index, kind, StepError(kind=error_kind, message=_cap(message)))

        def use(plugin: ResolvedPlugin) -> None:
            used.add(plugin.manifest.name)
            if len(used) > MAX_ITEMS:
                fail("plan_too_large", f"the pipeline uses more than {MAX_ITEMS} plugins")

        if isinstance(step, LlmStep):
            try:
                skill_plugin = plugins.plugin_for_skill(step.llm.skill)
            except UnknownPlugin:
                fail("plugin_unknown", f"no installed plugin ships skill {step.llm.skill}")
            if step.llm.skill not in skill_plugin.manifest.provides.skills:
                fail("tool_not_provided", f"{step.llm.skill} is not in the plugin's skills")
            use(skill_plugin)
            # The skill is loaded before the backend check: an invalid skill is reported
            # (`skill_invalid`) even where no backend is configured.
            skill = None if skills is None else _load_skill(skills, step.llm.skill, skill_plugin)
            if isinstance(skill, StepError):
                fail(skill.kind, skill.message)
            if not has_llm:
                fail("llm_unavailable", "the pipeline has an `llm` step and no LLM backend")
            planned.append(
                _Planned(index, kind, llm=step.llm, skill_plugin=skill_plugin, skill=skill)
            )
            continue
        calls: list[_Call] = []
        entries: list[_Entry] = []
        for position in range(len(_calls_of(step))):
            planned_call = rendered_calls[(index, position)]
            ref = planned_call.ref
            if kind == "collect":
                collect_calls += 1
                if collect_calls > MAX_ITEMS:
                    fail("plan_too_large", f"more than {MAX_ITEMS} collect calls in all")
            try:
                plugin = plugins.plugin_for_tool(ref)
            except UnknownPlugin:
                fail("plugin_unknown", f"no installed plugin provides {ref}")
            if ref not in getattr(plugin.manifest.provides, _PROVIDES[kind]):
                fail("tool_not_provided", f"{ref} is not in the plugin's {_PROVIDES[kind]}")
            use(plugin)
            rendered = planned_call.params
            if rendered is None:
                fail("template_error", f"{ref}: {planned_call.error}")
            problem = _plain_problem(rendered)
            if problem is not None:
                fail("params_invalid", f"{ref}: the parameters {problem[1]}")
            held = redactor.find(rendered)  # strings, keys and whole numbers
            if held:
                names = ", ".join(sorted(held))
                fail("params_invalid", f"{ref}: the parameters hold secret {names}")
            if kind in ("act", "notify"):
                try:
                    types, binds = _entry_binding(kind, ref, rendered, plugin)
                except ValueError as error:
                    fail("params_invalid", str(error))
                entries.append(_Entry(index, position, ref, rendered, plugin, types, binds))
            else:
                calls.append(_Call(ref, rendered, plugin, position, planned_call.timeout_s))
        planned.append(_Planned(index, kind, calls=tuple(calls), entries=tuple(entries)))
    return tuple(planned)


def _load_skill(skills: SkillSource, ref: str, plugin: ResolvedPlugin) -> LoadedSkill | StepError:
    """The skill of the `llm` step, loaded and checked while planning (`skill_invalid`)."""
    try:
        skill = skills.load_skill(ref, plugin)
        name, intents = skill.name, frozenset(skill.intents)
        limits = dict(skill.intent_limits)
    except SkillUnavailable as error:
        return StepError(kind="skill_invalid", message=_cap(str(error) or "skill unavailable"))
    except Exception as error:  # a port bug must not crash the run
        return StepError(kind="host_error", message=f"unexpected {type(error).__name__}")
    if name != ref:
        return StepError(kind="skill_invalid", message=_cap(f"{ref}: the source loaded {name}"))
    extra = sorted(intents - set(plugin.manifest.permissions.intents))
    if extra:
        message = f"{ref}: intents {', '.join(extra)} are not in the plugin's permissions"
        return StepError(kind="skill_invalid", message=_cap(message))
    if not set(limits) <= intents or not all(
        type(value) is int and value >= 1 for value in limits.values()
    ):
        message = f"{ref}: the skill's intent limits do not match its intents"
        return StepError(kind="skill_invalid", message=_cap(message))
    # #53 (schemas decision 43 D): a secret in the prompt or output schema refuses the run
    # here, before any step runs; `ModelBackends.prepare` keeps the same check as a backstop.
    try:
        held = current_redactor().find([skill.prompt, dict(skill.output_schema)])
    except Exception as error:  # a port bug must not crash the run
        return StepError(kind="host_error", message=f"unexpected {type(error).__name__}")
    if held:
        names = ", ".join(sorted(held))
        message = f"{ref}: the prompt or output schema holds secret {names} (never a parameter)"
        return StepError(kind="params_invalid", message=_cap(message))
    return skill


def _proposer(plugin: ResolvedPlugin, skill: LoadedSkill | None) -> ResolvedPlugin:
    """Whose permissions the LLM's intents are checked against: the plugin's, narrowed to
    the loaded skill's `intents` (an empty list: the skill proposes none)."""
    if skill is None:
        return plugin
    permissions = plugin.manifest.permissions.model_copy(
        update={"intents": [t for t in plugin.manifest.permissions.intents if t in skill.intents]}
    )
    manifest = plugin.manifest.model_copy(update={"permissions": permissions})
    return ResolvedPlugin(manifest, plugin.commit)


# --- execution -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Failure:
    kind: str
    message: str
    stderr: object = None
    violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class _Result:
    data: Any
    intents: Any
    stderr: object = None
    violations: tuple[Violation, ...] = ()


@dataclass
class _Outcome:
    status: StepStatus
    error: StepError | None = None
    output: Any = None
    intents: list[ProposedIntent] = field(default_factory=list)
    stop: bool = False
    calls: list[CallRecord] | None = None


@dataclass
class _Run:
    tools: ToolCaller
    llm: LlmBackend | None
    previous: PreviousLlm | None
    cache: LlmCache | None
    models: Mapping[str, str]
    entries: tuple[_Entry, ...]
    monotonic: Callable[[], float]
    deid: Deidentifier | None = None
    data: Any = None
    inputs: list[InputSnapshot] = field(default_factory=list)
    llm_calls: list[LlmCall] = field(default_factory=list)
    intents: list[ProposedIntent] = field(default_factory=list)
    pending: dict[int, list[ProposedIntent]] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)
    stderr: list[tuple[str, str]] = field(default_factory=list)
    input_hash: str | None = None
    partial: bool = False
    reused: bool = False
    cost: Usage = field(default_factory=lambda: _ZERO)

    def spend(self, usage: Usage) -> None:
        self.cost = Usage(
            tokens_in=self.cost.tokens_in + usage.tokens_in,
            tokens_out=self.cost.tokens_out + usage.tokens_out,
            usd=self.cost.usd + usage.usd,
        )

    async def call(self, call: _Call, data: Any) -> _Result | _Failure:
        """One tool call; failures are returned, never raised. Touches no run state (the calls
        of a `collect` step run concurrently): `absorb` records its stderr and violations."""
        try:
            result: object = await self.tools.call(
                call.ref, copy.deepcopy(call.params), _json_copy(data), timeout_s=call.timeout_s
            )
        except EngineCallError as error:
            message = _describe(call.ref, error.kind, error.message)
            return _Failure(_kind(error.kind), message, getattr(error, "stderr", None))
        except Exception as error:  # a host bug must not crash the run
            return _Failure("host_error", f"{call.ref}: unexpected {type(error).__name__}")
        if not isinstance(result, ToolResult):
            return _Failure(
                "output_invalid", f"{call.ref}: output_invalid: the host returned no ToolResult"
            )
        violations = _violations(result.violations)
        try:
            output = _json_copy(result.data, "tool output")
            intents = _json_copy(_intent_list(result.intents), "tool's intents")
        except _DataError as error:
            message = f"{call.ref}: {error.kind}: {error.message}"
            return _Failure(error.kind, message, result.stderr, violations)
        return _Result(output, intents, result.stderr, violations)

    def absorb(self, call: _Call, outcome: "_Result | _Failure") -> None:
        """Record a finished call's stderr and violations (in declaration order)."""
        if isinstance(outcome.stderr, str) and outcome.stderr:
            self.stderr.append((call.plugin.manifest.name, outcome.stderr))
        room = MAX_ITEMS - len(self.violations)
        self.violations.extend(outcome.violations[: max(0, room)])

    def propose(
        self,
        raw: Any,
        plugin: ResolvedPlugin,
        source: str,
        index: int,
        by: str,
        pending: int,
        collected: tuple[Any, ...] = (),
        shown: tuple[Any, ...] | None = None,
        guard: Callable[[tuple[Intent, ...]], str | None] | None = None,
    ) -> tuple[list[ProposedIntent], StepError | None]:
        """Validate and route one call's proposals; `pending` intents of this step are kept.

        `collected` is input data of the current `collect` step not yet in `self.inputs`
        (searched, with them, for `task.update_status` fingerprints). `shown`, when given, is
        what the proposer was shown (the LLM step's viewed input): fingerprints are searched
        there only. `guard` (the LLM step's action guard, `horae.engine.guard`) runs on the
        validated batch before routing: a detail it returns is recorded as a `guard` violation
        and returned as a `guard_violation` error (nothing of the batch is routed)."""
        raw = _intent_list(raw)
        entries = [entry for entry in self.entries if entry.step > index]
        unroutable: list[tuple[str, int, str]] = []
        if isinstance(raw, list):
            raw = [self._fill(payload, i, entries, unroutable) for i, payload in enumerate(raw)]
        try:
            typed: tuple[Intent, ...] = validate_intents(
                raw, permissions=plugin.manifest.permissions, source=source
            )
        except IntentRejected as rejected:
            # An intent whose target could not be filled fails its schema (the target is
            # required): that is a routing error, reported once permissions passed.
            unfilled = {position for _, position, _ in unroutable}
            if unroutable and all(
                r.index in unfilled and r.code == "intent_invalid" for r in rejected.rejections
            ):
                return [], _route_error(source, unroutable)
            for rejection in rejected.rejections:
                if rejection.code == "host_undeclared":
                    detail = _cap(f"{source}: {rejection.describe()}")
                    self.violations.append(Violation(kind="egress_blocked", detail=detail))
            return [], rejected.step_error()
        if unroutable:  # pragma: no cover (an unfilled target always fails its schema)
            return [], _route_error(source, unroutable)
        if guard is not None:
            refused = guard(typed)
            if refused is not None:
                _log.warning("action guard: %s", refused)
                if len(self.violations) < MAX_ITEMS:
                    self.violations.append(Violation(kind="guard", detail=refused))
                return [], StepError(kind=GUARD_ERROR, message=_cap(refused))
        kept = len(self.intents) + sum(len(p) for p in self.pending.values()) + pending
        if kept + len(typed) > MAX_RUN_INTENTS:
            message = f"more than {MAX_RUN_INTENTS} intents in the run"
            return [], StepError(kind="run_intents_exceeded", message=message)
        seen = ([snap.data for snap in self.inputs], collected) if shown is None else shown
        unknown = set(check_task_fingerprints(typed, seen))
        routed: list[ProposedIntent] = []
        for position, intent in enumerate(typed):
            if position in unknown:
                unroutable.append(("intent_unrouted", position, intent.type))
                continue
            takers = [entry for entry in entries if entry.takes(intent)]
            if len(takers) != 1:
                code = "intent_unrouted" if not takers else "intent_ambiguous"
                unroutable.append((code, position, intent.type))
                continue
            entry = takers[0]
            routed.append(
                ProposedIntent(
                    step=index,
                    proposed_by=by,
                    intent=intent,
                    applied_by=entry.ref,
                    applied_by_step=entry.step,
                    applied_by_call=entry.call,
                )
            )
        if unroutable:
            return [], _route_error(source, unroutable)
        return routed, None

    @staticmethod
    def _fill(
        payload: Any, position: int, entries: list[_Entry], unroutable: list[tuple[str, int, str]]
    ) -> Any:
        """`payload` with its omitted target filled from the one entry that binds its type;
        unchanged (and noted in `unroutable`) when no single entry does."""
        if not isinstance(payload, dict):
            return payload
        intent_type = payload.get("type")
        if not isinstance(intent_type, str) or intent_type not in TARGET_FIELDS:
            return payload
        name = TARGET_FIELDS[intent_type]
        if payload.get(name) is not None:
            return payload
        takers = [entry for entry in entries if intent_type in entry.types]
        if len(takers) != 1:
            code = "intent_unrouted" if not takers else "intent_ambiguous"
            unroutable.append((code, position, intent_type))
            return payload
        return fill_target(payload, takers[0].binds)


def fill_target(payload: Any, binds: Mapping[str, Any]) -> Any:
    """`payload` (one raw intent) with its omitted target field (`TARGET_FIELDS`: `path`, or
    `url` for `http.post`) taken from `binds` (an `act` entry's parameters, or a skill eval
    case's `targets`); unchanged when it has one, binds none, or is not target-bound."""
    if not isinstance(payload, dict):
        return payload
    intent_type = payload.get("type")
    if not isinstance(intent_type, str) or intent_type not in TARGET_FIELDS:
        return payload
    name = TARGET_FIELDS[intent_type]
    if payload.get(name) is not None or binds.get(name) is None:
        return payload
    return {**payload, name: binds[name]}


def check_task_fingerprints(typed: Sequence[Intent], shown: Sequence[Any]) -> list[int]:
    """The positions in `typed` of the `task.update_status` intents whose `task` fingerprint
    is not, as an exact string value, in `shown` (what the proposer was shown; rule 4 of the
    module docstring): a proposer can change the status of a task it was shown, not of one it
    guessed. Shared by the engine (`_Run.propose`) and the skill eval judge (decision 48)."""
    updates = [(i, it) for i, it in enumerate(typed) if isinstance(it, TaskUpdateStatus)]
    if not updates:
        return []
    known = _strings(*shown)
    return [position for position, intent in updates if intent.task not in known]


def _violations(raw: object) -> tuple[Violation, ...]:
    """The host's violations that are `Violation`s (anything else is dropped)."""
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(item for item in raw if isinstance(item, Violation))


def _strings(*values: Any) -> frozenset[str]:
    """Every string value (not object key) inside `values`, iteratively."""
    found: set[str] = set()
    stack: list[Any] = list(values)
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return frozenset(found)


def _route_error(source: str, unroutable: list[tuple[str, int, str]]) -> StepError:
    """Only closed-set values (codes, positions, intent types) are echoed."""
    lines = [f"{source!r} proposed {len(unroutable)} intent(s) no single entry can apply"]
    lines += [f"#{position} {intent_type} {code}" for code, position, intent_type in unroutable]
    return StepError(kind=unroutable[0][0], message=_cap("\n".join(lines)))


def _describe(ref: str, kind: object, message: object) -> str:
    return f"{ref}: {_kind(kind)}: {safe_excerpt(message, 200)}"


def _cut(text: str, room: int) -> str:
    """`text` cut to `room` characters, keeping its head and (mostly) its tail."""
    if len(text) <= room:
        return text
    if room <= len(_STDERR_CUT):
        return text[:room]
    keep = room - len(_STDERR_CUT)
    head = keep // 4
    return text[:head] + _STDERR_CUT + text[len(text) - (keep - head) :]


def _join_stderr(parts: list[tuple[str, str]]) -> str | None:
    """Each call's stderr gets an equal share of the 64 KiB cap, head and tail kept."""
    if not parts:
        return None
    share = _MAX_STDERR // len(parts)
    chunks = []
    for plugin, text in parts:
        prefix = f"--- {plugin} ---\n"
        chunks.append(prefix + _cut(text, max(0, share - len(prefix) - 1)))
    return _cap("\n".join(chunks), _MAX_STDERR)


async def _call_all(run: _Run, calls: tuple[_Call, ...]) -> list[_Result | _Failure]:
    """Every call, concurrently (at most `MAX_PARALLEL_CALLS` in flight), started in
    declaration order: a slot is taken before the next task is created, and the event loop
    runs new tasks in creation order. The results are in declaration order."""
    slots = asyncio.Semaphore(MAX_PARALLEL_CALLS)

    async def one(call: _Call) -> _Result | _Failure:
        try:
            return await run.call(call, None)
        finally:
            slots.release()

    tasks: list[asyncio.Task[_Result | _Failure]] = []
    async with asyncio.TaskGroup() as group:
        for call in calls:
            await slots.acquire()
            tasks.append(group.create_task(one(call)))
    return [task.result() for task in tasks]


async def _collect(run: _Run, step: _Planned) -> _Outcome:
    collected: list[dict[str, Any]] = []
    snapshots: list[InputSnapshot] = []
    kept: list[ProposedIntent] = []
    failed: list[dict[str, Any]] = []
    messages: list[str] = []
    rejected: StepError | None = None
    results = await _call_all(run, step.calls)
    for call, result in zip(step.calls, results, strict=True):
        run.absorb(call, result)
        if isinstance(result, _Failure):
            failed.append(
                {
                    "source": call.ref,
                    "call": call.position,
                    "params": call.params,
                    "error_kind": result.kind,
                }
            )
            messages.append(result.message)
            continue
        snapshots.append(
            InputSnapshot(
                step=step.index,
                call=call.position,
                source=call.ref,
                params=call.params,
                data=result.data,
                intents=_raw(result.intents),
            )
        )
        if rejected is None:
            name = call.plugin.manifest.name
            seen = (*(entry["data"] for entry in collected), result.data)
            accepted, rejected = run.propose(
                result.intents, call.plugin, name, step.index, call.ref, len(kept), seen
            )
            kept.extend(accepted)
        collected.append({"source": call.ref, "params": call.params, "data": result.data})
    run.inputs.extend(snapshots)
    if rejected is not None:
        # Every source that ran is recorded, with the rejected proposals, so that a replay
        # re-validates them.
        return _Outcome("failed", rejected, output=failed or None)
    run.data = collected
    if not failed:
        return _Outcome("ok", intents=kept)
    error = StepError(kind="source_failed", message=_cap("\n".join(messages)))
    if not collected:
        return _Outcome("failed", error, output=failed)
    run.partial = True
    return _Outcome("degraded", error, output=failed, intents=kept)


async def _transform(run: _Run, step: _Planned) -> _Outcome:
    kept: list[ProposedIntent] = []
    calls: list[CallRecord] = []
    for call in step.calls:
        input_hash = sha256_hex(run.data)
        result = await run.call(call, run.data)
        run.absorb(call, result)
        if isinstance(result, _Failure):
            calls.append(
                CallRecord(
                    call=call.position,
                    ref=call.ref,
                    params=call.params,
                    input_hash=input_hash,
                    error_kind=result.kind,
                )
            )
            error = StepError(kind=result.kind, message=_cap(result.message))
            return _Outcome("failed", error, calls=calls)
        calls.append(
            CallRecord(
                call=call.position,
                ref=call.ref,
                params=call.params,
                input_hash=input_hash,
                intents=_raw(result.intents),
            )
        )
        name = call.plugin.manifest.name
        accepted, rejected = run.propose(
            result.intents, call.plugin, name, step.index, call.ref, len(kept)
        )
        if rejected is not None:
            return _Outcome("failed", rejected, calls=calls)
        kept.extend(accepted)
        run.data = result.data
    return _Outcome("ok", output=run.data, intents=kept, calls=calls)


async def _apply_step(run: _Run, step: _Planned) -> _Outcome:
    """`act` / `notify`: no call; record the intents routed to each entry."""
    routed = run.pending.pop(step.index, [])
    if step.kind == "act" and run.partial:
        message = (
            f"a collect step degraded: not writing from partial data "
            f"({len(routed)} intent(s) dropped)"
        )
        return _Outcome("skipped", StepError(kind="partial_input", message=message))
    base = len(run.intents)
    run.intents.extend(routed)
    output = [
        {
            "call": entry.ref,
            "params": entry.params,
            "intents": [
                base + i for i, intent in enumerate(routed) if intent.applied_by_call == entry.call
            ],
        }
        for entry in step.entries
    ]
    return _Outcome("ok", output=output)


def _cache_key(
    run: _Run, spec: LlmSpec, plugin: ResolvedPlugin, skill: LoadedSkill | None, viewed: Any
) -> str:
    key: dict[str, Any] = {
        "skill": spec.skill,
        "plugin": {
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "commit": plugin.commit,
        },
        "model": run.models.get(spec.model, spec.model),
        "input": viewed,
    }
    if skill is not None:
        key["skill_version"] = skill.version
        key["skill_digest"] = skill.digest
    return sha256_hex(key)


_MAX_DEID_NOTES = 16


def _deid_record(
    pseudo: Pseudonyms, *unresolved: list[str], accepted: bool = True
) -> dict[str, Any]:
    """`llm[].request.deid`: the summary (never a value) and, for an accepted output, the
    pseudonyms that re-identification could not resolve."""
    recorded = dict(pseudo.summary())
    if accepted:
        notes = sorted({str(note) for found in unresolved for note in found})
        recorded["unresolved"] = notes[:_MAX_DEID_NOTES]
    return recorded


MAX_REIDENTIFIED_BYTES = 256 * 1024
"""Largest re-identified LLM output and intents together (compact UTF-8 JSON): the skill
output bound (`horae.skills.MAX_OUTPUT_BYTES`, restated: the engine does not import skills).
Checked before substituting (`Pseudonyms.growth`), so a model repeating a pseudonym cannot
make the engine build a huge output (#34 finding 1)."""


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _reidentified(pseudo: Pseudonyms | None, output: Any, raw: Any) -> tuple[Any, Any, Any]:
    """`(output, raw intents, deid record)` with real values put back (#34); unchanged, and
    no record, without pseudonyms. `_DataError` (`output_too_large`) when the result would
    be larger than `MAX_REIDENTIFIED_BYTES` (computed before substituting)."""
    if pseudo is None:
        return output, raw, None
    size = _encoded_size(output) + _encoded_size(raw) + pseudo.growth(output) + pseudo.growth(raw)
    if size > MAX_REIDENTIFIED_BYTES:
        raise _DataError(
            "output_too_large",
            f"the re-identified LLM output would be larger than {MAX_REIDENTIFIED_BYTES} bytes",
        )
    real_output, in_output = pseudo.reidentify(output)
    real_raw, in_intents = pseudo.reidentify(raw)
    return (
        _json_copy(real_output, "re-identified LLM output"),
        _json_copy(real_raw, "re-identified LLM intents"),
        _deid_record(pseudo, in_output, in_intents),
    )


def _deid_digest(pseudo: Pseudonyms) -> str | None:
    """The SHA-256 of a de-identified input (`summary()["input_sha256"]`)."""
    digest = pseudo.summary().get("input_sha256")
    return digest if isinstance(digest, str) else None


def _view_options(skill: LoadedSkill | None) -> dict[str, Any]:
    """The skill's input view options (`{}` without a skill, or for a skill without any)."""
    if skill is None:
        return {}
    options = getattr(skill, "view_options", None)
    return dict(options) if isinstance(options, Mapping) else {}


def _classified(options: Mapping[str, Any]) -> bool:
    """Whether the view options name a field class (#34)."""
    return any(
        isinstance(value, Mapping) and isinstance(value.get("class"), str)
        for value in options.values()
    )


async def _reuse(
    run: _Run,
    step: _Planned,
    spec: LlmSpec,
    plugin: ResolvedPlugin,
    key: str,
    viewed: Any,
    sent: Any,
    pseudo: Pseudonyms | None,
) -> _Outcome | None:
    """An accepted output for `key`, reused; `None` if there is none or it cannot be reused
    (then the LLM is called)."""
    previous = run.previous
    from_cache = False
    if (previous is None or previous.input_hash != key) and run.cache is not None:
        schema = None if step.skill is None else step.skill.output_schema
        from_cache = True
        try:
            previous = await run.cache.lookup(key, model_alias=spec.model, output_schema=schema)
        except Exception:  # a broken cache is a miss, never a failed run
            previous = None
    if not isinstance(previous, PreviousLlm) or previous.input_hash != key:
        return None
    # Pseudonyms name values only under the mapping that made them (#34 finding 7).
    if previous.deid_input_sha256 != (None if pseudo is None else _deid_digest(pseudo)):
        return None
    try:
        output = _json_copy(previous.output, "previous LLM output")
        raw = _json_copy(_intent_list(previous.intents), "previous LLM intents")
        # A cached output is the pseudonymized one: the same input gives the same mapping.
        real_output, real_raw, deid_record = _reidentified(pseudo, output, raw)
        marker = _reuse_marker(spec, key, previous.source_run)
        if deid_record is not None:
            marker["deid"] = deid_record
        call = LlmCall(
            step=step.index,
            skill=spec.skill,
            skill_version=previous.skill_version if step.skill is None else step.skill.version,
            skill_digest=None if step.skill is None else step.skill.digest,
            backend=previous.backend,
            model=previous.model,
            cached=True,
            schema_valid=True,
            model_alias=spec.model,
            input_hash=sha256_hex(sent),
            request=marker,
            response=output,
            intents=raw,
            usage=_ZERO,
        )
    except (_DataError, ValidationError):
        return None
    violations = len(run.violations)
    proposer = _proposer(plugin, step.skill)
    source = f"skill:{spec.skill}"
    accepted, rejected = run.propose(
        real_raw, proposer, source, step.index, spec.skill, 0, **_shown(step, viewed, source)
    )
    if rejected is not None:
        # E.g. a target bound to `{{date}}` moved on: a cache miss, not a failure. A cached
        # entry the action guard refuses is also deleted (schemas decision 46).
        del run.violations[violations:]
        if from_cache and rejected.kind == GUARD_ERROR:
            await _discard(run.cache, key, spec.model)
        return None
    run.llm_calls.append(call)
    run.data, run.input_hash, run.reused = real_output, key, True
    return _Outcome("ok", output=real_output, intents=accepted)


def _reuse_marker(spec: LlmSpec, key: str, source_run: str | None) -> dict[str, Any]:
    """The recorded request of a reuse: no call was made (`reused_input_hash`), and the run
    that produced the output, when known (`source_run`)."""
    marker: dict[str, Any] = {"skill": spec.skill, "model": spec.model, "reused_input_hash": key}
    if source_run is not None:
        marker["source_run"] = source_run
    return marker


async def _discard(cache: LlmCache | None, key: str, model_alias: str) -> None:
    """Delete a cached entry (`LlmCache.discard`, part of the port: a cache that cannot delete
    has a no-op); never raises."""
    if cache is not None:
        with contextlib.suppress(Exception):  # a broken cache never fails the run
            await cache.discard(key, model_alias=model_alias)


def _shown(step: _Planned, viewed: Any, source: str) -> dict[str, Any]:
    """The action guard on what the model was shown (the viewed input), with the skill's
    per-type intent limits; with a skill, `task.update_status` fingerprints must also come
    from the viewed input."""
    limits = None if step.skill is None else step.skill.intent_limits
    guard = {"guard": guard_for(viewed, source, limits)}
    return guard if step.skill is None else {**guard, "shown": (viewed,)}


async def _llm_step(run: _Run, step: _Planned) -> _Outcome:
    spec, plugin, backend = step.llm, step.skill_plugin, run.llm
    if spec is None or plugin is None or backend is None:  # pragma: no cover (planned)
        raise RuntimeError("an llm step is planned with its spec, plugin and backend")
    skill = step.skill
    try:
        viewed = run.data if skill is None else _json_copy(skill.view(run.data), "viewed input")
    except _DataError as error:
        return _Outcome("degraded", StepError(kind=error.kind, message=error.message), stop=True)
    except Exception as error:  # a skill bug must not crash the run
        message = f"unexpected {type(error).__name__} applying the input view"
        return _Outcome("degraded", StepError(kind="host_error", message=message), stop=True)
    if skill is not None and viewed is None:
        # Nothing of the input matches the view's shape: no call is made.
        message = f"{spec.skill}: the step input does not have the shape of the input view"
        return _Outcome("degraded", StepError(kind="input_invalid", message=message), stop=True)

    def degraded(kind: str, message: str) -> _Outcome:
        return _Outcome("degraded", StepError(kind=kind, message=_cap(message)), stop=True)

    # The step's deadline covers de-identification too (#34 finding 9): it runs in a worker
    # thread, so a slow one times the step out instead of blocking the event loop.
    timeout = spec.timeout_s or DEFAULT_LLM_TIMEOUT_S
    deadline = run.monotonic() + timeout
    options = _view_options(skill)
    classified = _classified(options)
    pseudo: Pseudonyms | None = None
    if classified and run.deid is not None:
        policy = run.deid
        try:
            async with asyncio.timeout(timeout):
                pseudo = await asyncio.to_thread(policy.pseudonymize, spec.model, options, viewed)
        except TimeoutError:
            return degraded("timeout", f"de-identifying took longer than {timeout} s")
        except DeidUnavailable as error:
            return degraded("deid_unavailable", f"{spec.skill}: {safe_excerpt(str(error), 200)}")
        except Exception as error:  # a policy bug must not crash the run, nor send in clear
            return degraded("host_error", f"unexpected {type(error).__name__} de-identifying")
    try:
        sent = viewed if pseudo is None else _json_copy(pseudo.data, "pseudonymized input")
    except _DataError as error:
        return degraded(error.kind, error.message)
    key = _cache_key(run, spec, plugin, skill, viewed)
    reused = await _reuse(run, step, spec, plugin, key, viewed, sent, pseudo)
    if reused is not None:
        return reused
    prompt = None if skill is None else skill.prompt
    output_schema = None if skill is None else skill.output_schema
    spent_usd, spent_tokens = 0.0, 0
    repair: tuple[str, ...] = ()
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        if spent_usd >= spec.budget.usd or spent_tokens >= spec.budget.tokens_out:
            return degraded("budget_exceeded", "no budget left for the repair retry")
        remaining = deadline - run.monotonic()
        if remaining <= 0:
            return degraded("timeout", f"the LLM step took longer than {timeout} s")
        budget = Budget(
            usd=spec.budget.usd - spent_usd, tokens_out=spec.budget.tokens_out - spent_tokens
        )
        request = LlmRequest(
            spec.skill,
            spec.model,
            _json_copy(sent),
            budget,
            remaining,
            attempt,
            repair,
            prompt=prompt,
            output_schema=None if output_schema is None else copy.deepcopy(output_schema),
            skill_version=None if skill is None else skill.version,
            skill_digest=None if skill is None else skill.digest,
            classified=classified,
            deidentified=None if pseudo is None else pseudo.summary(),
        )
        try:
            async with asyncio.timeout(remaining):
                response = await backend.complete(request)
        except TimeoutError as error:
            # Cancelled while its output was being validated, the attempt was billed: its
            # usage travels with the cancellation (schemas decision 44 E).
            billed = attached_usage(error)
            if billed is not None:
                run.spend(billed)
            return degraded("timeout", f"the LLM step took longer than {timeout} s")
        except LlmError as error:
            usage = getattr(error, "usage", None)
            if isinstance(usage, Usage):
                run.spend(usage)
            message = current_redactor().redact_text(error.message)[0]
            if error.kind == "secret_in_request" and len(run.violations) < MAX_ITEMS:
                # T10: refused before it was sent; the message names secrets, never values.
                detail = _cap(f"{spec.skill}: {message}", 500)
                run.violations.append(Violation(kind="secret_in_request", detail=detail))
            return degraded(_kind(error.kind), safe_excerpt(message, 200))
        except Exception as error:  # a backend bug must not crash the run
            return degraded("host_error", f"unexpected {type(error).__name__}")
        try:
            if not isinstance(response, LlmResponse):
                raise _DataError("output_invalid", "the LLM backend returned no LlmResponse")
            output = _json_copy(response.output, "LLM output")
            raw = _json_copy(_intent_list(response.intents), "LLM intents")
            recorded = (
                _json_copy(response.request, "LLM request")
                if response.request is not None
                else {
                    "skill": spec.skill,
                    "model": spec.model,
                    "input": request.input,
                    "attempt": attempt,
                    "repair": list(repair),
                }
            )
            if not isinstance(response.usage, Usage):
                raise _DataError("output_invalid", "the LLM usage is not a Usage")
            usage = _ZERO if response.cached is True else response.usage
            # Capped like the record's messages; the repair retry is sent these same errors.
            errors = tuple(_cap(str(error)) for error in response.schema_errors)[:MAX_ITEMS]
            # The record keeps what the provider saw (#34); re-identification comes after
            # the call is recorded and its usage spent (finding 8).
            if pseudo is not None and isinstance(recorded, dict):
                recorded["deid"] = _deid_record(pseudo, accepted=False)
            llm_call = LlmCall(
                step=step.index,
                skill=spec.skill,
                skill_version=response.skill_version if skill is None else skill.version,
                skill_digest=None if skill is None else skill.digest,
                backend=response.backend,
                model=response.model,
                cached=response.cached,
                attempt=attempt,
                schema_valid=not errors,
                model_alias=spec.model,
                input_hash=sha256_hex(request.input),
                repair=list(repair),
                schema_errors=list(errors),
                request=recorded,
                response=output,
                intents=raw,
                usage=usage,
            )
        except _DataError as error:
            return degraded(error.kind, error.message)
        except (ValidationError, TypeError, ValueError):
            return degraded("output_invalid", "the LLM response does not fit the run record")
        run.spend(usage)
        spent_usd += usage.usd
        spent_tokens += usage.tokens_out
        run.llm_calls.append(llm_call)
        if spent_usd > spec.budget.usd or spent_tokens > spec.budget.tokens_out:
            return degraded("budget_exceeded", "the LLM step spent more than its budget")
        if not errors:
            # Validated, then re-identified (#34): a failure here keeps the call and its cost.
            try:
                real_output, real_raw, deid_record = _reidentified(pseudo, output, raw)
            except _DataError as error:
                return degraded(error.kind, error.message)
            if deid_record is not None and isinstance(recorded, dict):
                request_record = {**recorded, "deid": deid_record}
                run.llm_calls[-1] = llm_call.model_copy(update={"request": request_record})
            source = f"skill:{spec.skill}"
            proposer = _proposer(plugin, skill)
            accepted, rejected = run.propose(
                real_raw,
                proposer,
                source,
                step.index,
                spec.skill,
                0,
                **_shown(step, viewed, source),
            )
            if rejected is not None:
                # The guard degrades the whole step (nothing of it is routed); a permission or
                # routing breach fails the run.
                status: StepStatus = "degraded" if rejected.kind == GUARD_ERROR else "failed"
                return _Outcome(status, rejected, stop=True)
            run.data, run.input_hash = real_output, key
            return _Outcome("ok", output=real_output, intents=accepted)
        repair = errors
    return degraded(
        "schema_invalid", f"the output failed its schema after {MAX_LLM_ATTEMPTS} attempts"
    )


_STEPS = {
    "collect": _collect,
    "transform": _transform,
    "llm": _llm_step,
    "act": _apply_step,
    "notify": _apply_step,
}


async def execute(
    pipeline: Pipeline,
    *,
    run_id: str,
    context: RunContext,
    started_at: datetime.datetime,
    plugins: PluginRegistry,
    tools: ToolCaller,
    llm: LlmBackend | None = None,
    skills: SkillSource | None = None,
    dry_run: bool = False,
    previous: PreviousLlm | None = None,
    cache: LlmCache | None = None,
    models: Mapping[str, str] | None = None,
    lock_digest: str | None = None,
    config_digest: str | None = None,
    on_step: StepHook | None = None,
    clock: Callable[[], datetime.datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    deid: Deidentifier | None = None,
) -> RunRecord:
    """Run `pipeline` once and return its record; never raises for a step or port failure.

    - `context`: the template context (`horae.engine.make_context`, with the instance's time
      zone).
    - `started_at`: the run's start, the value given to `Store.begin_run` (the record keeps it).
    - `tools` is called for `collect` and `transform` only; `act` / `notify` entries are
      routed to, never called (ADR 0002).
    - `skills`: loads the `llm` step's skill while planning (`skill_invalid` when it cannot);
      its prompt, output schema and version go into each `LlmRequest`, its input view
      restricts what the model sees, and its version and digest join the cache key. Without
      it (fakes and older callers) the request carries the whole input and no prompt.
    - `previous`: an accepted LLM output for the unchanged-input path (replay gives the one
      the recorded run reused); `cache`: looked up by the step's cache key when `previous`
      does not match (the runner's `StoreLlmCache`, #32). `models`: model alias to concrete
      model (`horae.yaml`), part of the LLM cache key.
    - `models` (when given), `lock_digest` and `config_digest` (`horae.config.Instance`) are
      recorded as given, for replays; an invalid value raises `ValueError` before anything
      runs. The canonical pipeline definition is recorded too (`pipeline_definition`), unless
      it is larger than `MAX_PIPELINE_BYTES` or holds a resolved secret's value (#53: a
      literal secret in a pipeline file; its parameters refuse the run at plan time, and the
      definition is not copied into the store).
    - `deid`: the instance's de-identification policy (`horae.llm.deid.Deidentifier`, #34):
      with a skill that declares field classes and a cloud model alias, the model is sent
      the pseudonymized viewed input and the accepted output and intents are re-identified
      (see **De-identification** in the module docstring). Without it, nothing is
      pseudonymized (and `ModelBackends` refuses a classified request to a cloud alias).
    - `dry_run` is recorded only: the engine applies nothing either way.
    - `on_step(index, record)` is awaited after each step record is final; its exceptions
      propagate. `clock` gives the wall-clock timestamps, `monotonic` the durations and the
      LLM deadline.

    Raises `RunCancelled` (an `asyncio.CancelledError`) with the partial record if cancelled.
    Persistence, locking and the run timeout are the caller's (#19)."""
    digest = pipeline_digest(pipeline)
    try:
        for value in (lock_digest, config_digest):
            if value is not None:
                _DIGEST.validate_python(value)
        recorded_models = None if models is None else _MODELS.validate_python(dict(models))
    except ValidationError:
        raise ValueError("`lock_digest`, `config_digest` or `models` is invalid") from None
    if recorded_models is not None and len(recorded_models) > MAX_ITEMS:
        raise ValueError(f"more than {MAX_ITEMS} model aliases")
    canonical = canonical_pipeline(pipeline)
    definition = canonical if len(canonical_json(canonical)) <= MAX_PIPELINE_BYTES else None
    if definition is not None and current_redactor().find(definition):
        definition = None  # a pipeline file holding a secret is not copied into the record
    records: list[StepRecord] = []
    status: RunStatus = "ok"
    run_error: str | None = None
    try:
        plan = _plan(pipeline, context, plugins, llm is not None, skills)
    except _PlanError as error:
        plan = ()
        status, run_error = "failed", f"step {error.index}: {error.error.message}"
        for index, declared in enumerate(pipeline.steps):
            if index == error.index:
                records.append(StepRecord(kind=error.kind, status="failed", error=error.error))
            else:
                records.append(StepRecord(kind=_kind_of(declared), status="skipped"))
    run = _Run(
        tools=tools,
        llm=llm,
        previous=previous,
        cache=cache,
        models=dict(models or {}),
        entries=tuple(entry for p in plan for entry in p.entries),
        monotonic=monotonic,
        deid=deid,
    )
    used = {c.plugin.manifest.name: c.plugin for p in plan for c in p.calls}
    used.update({e.plugin.manifest.name: e.plugin for e in run.entries})
    used.update({p.skill_plugin.manifest.name: p.skill_plugin for p in plan if p.skill_plugin})
    resolved: dict[str, set[str]] = {}
    for p in plan:
        for c in p.calls:
            resolved.setdefault(c.plugin.manifest.name, set()).add(c.ref)
        for e in p.entries:
            resolved.setdefault(e.plugin.manifest.name, set()).add(e.ref)
        if p.skill_plugin is not None and p.llm is not None:
            resolved.setdefault(p.skill_plugin.manifest.name, set()).add(p.llm.skill)

    recorded_vars, _ = current_redactor().scrub_data(dict(context.vars))
    recorded_context = context.model_copy(update={"vars": recorded_vars})

    def record(final: RunStatus, error: str | None, steps: list[StepRecord]) -> RunRecord:
        return RunRecord(
            schema_version=1,
            id=run_id,
            pipeline=pipeline.name,
            pipeline_digest=digest,
            context=recorded_context,
            lock_digest=lock_digest,
            config_digest=config_digest,
            models=recorded_models,
            pipeline_definition=definition,
            input_hash=run.input_hash,
            dry_run=dry_run,
            started_at=started_at,
            finished_at=max(clock(), started_at),
            status=final,
            plugins={
                name: PluginVersion(
                    version=p.manifest.version,
                    commit=p.commit,
                    provides=sorted(resolved.get(name, ())),
                )
                for name, p in used.items()
            },
            inputs=run.inputs,
            steps=steps,
            llm=run.llm_calls,
            intents=run.intents,
            changes=[],
            violations=run.violations,
            cost=run.cost,
            error=_cap(error) if error else None,
        )

    last_use: dict[str, int] = {}
    for p in plan:
        for c in p.calls:
            last_use[c.plugin.manifest.name] = p.index
    release = getattr(tools, "release", None)

    async def release_after(index: int, stopping: bool) -> None:
        if not callable(release):
            return
        for name in sorted(last_use):
            if stopping or last_use[name] == index:
                del last_use[name]
                with contextlib.suppress(Exception):  # a port problem never fails the run
                    await release(name)

    in_step = False
    try:
        if on_step is not None:
            for index, step_record in enumerate(records):
                await on_step(index, step_record)
        stopped = False
        for step in plan:
            if stopped:
                step_record = StepRecord(kind=step.kind, status="skipped")
            else:
                step_start = max(clock(), started_at)
                begin = monotonic()
                run.stderr = []
                in_step = True
                outcome = await _STEPS[step.kind](run, step)
                in_step = False
                elapsed = max(0.0, monotonic() - begin)
                step_record = StepRecord(
                    kind=step.kind,
                    status=outcome.status,
                    output=outcome.output,
                    error=outcome.error,
                    started_at=step_start,
                    finished_at=max(clock(), step_start),
                    stderr=_join_stderr(run.stderr),
                    duration_ms=int(elapsed * 1000),
                    calls=outcome.calls,
                )
                if outcome.status != "failed":
                    for intent in outcome.intents:
                        run.pending.setdefault(intent.applied_by_step or 0, []).append(intent)
                effect = _EFFECT[outcome.status]
                if _RANK[effect] > _RANK[status]:
                    status = effect
                if outcome.error is not None and run_error is None and outcome.status != "ok":
                    run_error = f"step {step.index} ({step.kind}): {outcome.error.message}"
                stopped = outcome.stop or outcome.status == "failed"
                await release_after(step.index, stopped)
            records.append(step_record)
            if on_step is not None:
                await on_step(step.index, step_record)
    except asyncio.CancelledError as cancelled:
        partial = list(records)
        for index in range(len(partial), len(pipeline.steps)):
            kind = _kind_of(pipeline.steps[index])
            if index == len(records) and in_step:
                why = StepError(kind="cancelled", message="the run was cancelled")
                partial.append(StepRecord(kind=kind, status="failed", error=why))
            else:
                partial.append(StepRecord(kind=kind, status="skipped"))
        raise RunCancelled(record("failed", "the run was cancelled", partial)) from cancelled

    if status == "ok" and run.reused and not run.intents:
        status = "noop"
    return record(status, run_error, records)


__all__ = [
    "ACT_MODES",
    "DEFAULT_LLM_TIMEOUT_S",
    "MAX_DEPTH",
    "MAX_LLM_ATTEMPTS",
    "MAX_OUTPUT_BYTES",
    "MAX_PARALLEL_CALLS",
    "TARGET_FIELDS",
    "PlannedCall",
    "RunCancelled",
    "StepHook",
    "check_task_fingerprints",
    "execute",
    "fill_target",
    "planned_calls",
]
