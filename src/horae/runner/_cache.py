# SPDX-License-Identifier: Apache-2.0
"""The LLM cache: the $0 path on unchanged input (#32, schemas decision 45).

The store's `llm_cache` table is the single source of reusable LLM outputs for live runs.

**Two keys.** The engine's cache key (`RunRecord.input_hash`: SHA-256 of the canonical JSON
of the skill, its version and digest, the skill plugin's name, version and commit, the
resolved model and the viewed input) says *what was asked*. An entry is **stored** under
`stored_key(input_hash, model_alias)`: SHA-256 of `{input_hash, alias, version}`, where
`alias` is the step's model alias as `horae.yaml` declares it now, minus its `secret` and
`price` (backend, model, `base_url`, `structured_output`, `max_output_tokens`,
`max_tokens_field`, and any later field such as the `claude-cli` command; unset fields
left out), and `version` is `LLM_KEY_VERSION` (`horae.llm.backends._shared`, bumped when
Horae's own system preamble or request framing changes); for a cloud alias under a
de-identification policy (#34), `deid` too: the transforms' version and the vocabulary's
digest (`Deidentifier.cache_settings`, decision 47 F), so another vocabulary is a miss and an
entry (which holds the pseudonymized output) is only reused with the mapping that made it.
So another backend, endpoint or output mode behind the same model name is a miss, while
the run record keeps the engine key.
An entry's response is `{output, intents, backend, model, skill_version, input_hash,
run_id, created_at}`: `input_hash` is the engine key (what undo invalidates by), `run_id` the
run that produced it, `created_at` when.

- **Lookup** (`StoreLlmCache.lookup`, the engine's `LlmCache` port): an entry is live when it
  has not expired (`expires_at`) and was created less than `cache.ttl_s` ago (the current
  setting: lowering it applies at once to every entry; raising it applies to entries written
  from then on, whose `expires_at` is later). It is then **re-validated** against the skill's
  full output schema (the output with its reserved `intents` put back) by the same validator
  the backend path uses (`default_validator`: `horae.llm.validate.validate_output`, the
  killable worker with its CPU and memory ceilings, #31): an entry that fails, or that the
  validator cannot check (`validator_unavailable`), is a miss and is deleted. A
  hit names the alias' *current* backend and model and the producing run (`source_run`). A
  hit makes no backend call, so the usage ledger is not touched: a cached output costs
  nothing. A store error or a malformed entry is a miss (logged by type).
- **Write** (`StoreLlmCache.remember`, after the run's record is saved): only an **accepted**
  output: the run is `ok` or `noop` (not `degraded`, `failed` or `undone`) and not a dry run,
  its `llm` step is `ok`, and the step's last call is schema-valid, has recorded intents and
  is not itself a reuse (a hit never extends an entry's life). `Store.cache_put_safe` refuses
  an entry that holds a secret's value: it is then simply not cached. A store error is
  logged, never fails the run. Two processes writing the same key both succeed: the last
  write wins (`INSERT OR REPLACE` under the store's write lock).
- **Purge** (`StoreLlmCache.purge`, at the start of each non-dry run): entries past the
  current `ttl_s` or their `expires_at`, then the oldest beyond `cache.max_entries` (first in,
  first out); best effort. `horae cache purge` deletes on demand.
"""

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeGuard, get_args

from horae.contracts import RunRecord, Usage
from horae.contracts.instance import CacheConfig, ModelAlias
from horae.engine import PreviousLlm
from horae.engine.canonical import sha256_hex
from horae.engine.ports import LlmBackendName, LlmError
from horae.llm.backends._shared import LLM_KEY_VERSION
from horae.llm.base import Validator, production_validator
from horae.llm.deid import Deidentifier
from horae.skills import INTENTS_PROPERTY
from horae.store import Store

_log = logging.getLogger("horae.runner")

_BACKENDS = frozenset(get_args(LlmBackendName))
_STATUSES = frozenset({"ok", "noop"})
"""The run statuses whose LLM output may be cached (D2's rule, decision 30)."""
CACHE_VALIDATION_TIMEOUT_S = 10.0
"""The longest a cached output's re-validation may take (`VALIDATION_TIMEOUT_S`, #31)."""

CacheValidator = Validator
"""`validate(output, schema, timeout_s=...)`: the errors of a full output (empty when valid),
or an awaitable of them; raises when it cannot validate. `horae.llm.base.Validator`."""


def default_validator() -> CacheValidator:
    """The validator of the backend path, `horae.llm.validate.validate_output` (#31, the
    default of `ModelBackends` too): the killable, memory-bounded worker, never a synchronous
    validation on the event loop (decisions 44 and 45 F)."""
    return production_validator()


def full_output(output: Any, intents: list[Any], schema: Mapping[str, Any]) -> Any:
    """The output as the model gave it: with its reserved `intents` put back when the schema
    declares them (or when there are some, which a schema without them then rejects)."""
    properties = schema.get("properties")
    declared = isinstance(properties, Mapping) and INTENTS_PROPERTY in properties
    if not declared and not intents:
        return output
    if not isinstance(output, dict):
        return {INTENTS_PROPERTY: intents, "output": output}  # cannot be valid: a miss
    return {**output, INTENTS_PROPERTY: intents}


def _is_reuse(request: Any) -> bool:
    return isinstance(request, dict) and isinstance(request.get("reused_input_hash"), str)


@dataclass(frozen=True)
class AcceptedEntry:
    """What `remember` stores for a record (see `accepted_entry`)."""

    input_hash: str
    model_alias: str | None
    skill: str
    response: dict[str, Any]
    usage: Usage


def accepted_entry(record: RunRecord) -> AcceptedEntry | None:
    """The record's accepted LLM output, if it may be cached (see the module docstring)."""
    key = record.input_hash
    if key is None or record.dry_run or record.status not in _STATUSES:
        return None
    steps = [i for i, step in enumerate(record.steps) if step.kind == "llm"]
    if not steps or record.steps[steps[0]].status != "ok":
        return None
    calls = [c for c in record.llm if c.step == steps[0]]
    if not calls or any(_is_reuse(c.request) for c in calls):
        return None
    call = calls[-1]
    if call.schema_valid is False or call.intents is None:
        return None
    usage = Usage(
        tokens_in=sum(c.usage.tokens_in for c in calls),
        tokens_out=sum(c.usage.tokens_out for c in calls),
        usd=sum(c.usage.usd for c in calls),
    )
    response = {
        "output": call.response,
        "intents": list(call.intents),
        "backend": call.backend,
        "model": call.model,
        "skill_version": call.skill_version,
        "input_hash": key,
        "run_id": record.id,
    }
    return AcceptedEntry(key, call.model_alias, call.skill, response, usage)


@dataclass(frozen=True)
class _Parsed:
    output: Any
    intents: list[Any]
    backend: LlmBackendName
    model: str
    skill_version: str
    source_run: str | None


def _is_backend(value: Any) -> TypeGuard[LlmBackendName]:
    return value in _BACKENDS


def _parse(response: Any) -> _Parsed | None:
    if not isinstance(response, dict) or "output" not in response:
        return None
    intents, backend = response.get("intents"), response.get("backend")
    model, version = response.get("model"), response.get("skill_version")
    if (
        not isinstance(intents, list)
        or not _is_backend(backend)
        or not isinstance(model, str)
        or not isinstance(version, str)
    ):
        return None
    run_id = response.get("run_id")
    source_run = run_id if isinstance(run_id, str) else None
    return _Parsed(response["output"], intents, backend, model, version, source_run)


class StoreLlmCache:
    """The engine's `LlmCache` over `Store`'s `llm_cache` table, with the instance's policy."""

    def __init__(
        self,
        store: Store,
        config: CacheConfig,
        clock: Callable[[], datetime],
        *,
        read: bool,
        models: Mapping[str, ModelAlias] | None = None,
        validate: CacheValidator | None = None,
        validation_timeout_s: float = CACHE_VALIDATION_TIMEOUT_S,
        deid: Deidentifier | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.read = read
        """`False` under `horae run --no-cache`: every lookup is a miss."""
        self.models = dict(models or {})
        """`horae.yaml`'s model aliases: part of the stored key, and what a hit names."""
        self.validate = validate or default_validator()
        self.validation_timeout_s = validation_timeout_s
        """The longest a re-validation may take (the worker is killed past it: a miss)."""
        self.deid = deid
        """The instance's de-identification policy (#34): its settings for a cloud alias join
        the stored key (decision 47 F)."""

    def stored_key(self, input_hash: str, model_alias: str | None) -> str:
        """The key an entry for the engine key `input_hash` is stored under (module docstring);
        an alias `horae.yaml` does not declare counts as `null`."""
        spec = None if model_alias is None else self.models.get(model_alias)
        alias = (
            None
            if spec is None
            else spec.model_dump(mode="json", exclude={"secret", "price"}, exclude_none=True)
        )
        key: dict[str, Any] = {"input_hash": input_hash, "alias": alias, "version": LLM_KEY_VERSION}
        settings = None if self.deid is None else self.deid.cache_settings(model_alias)
        if settings is not None:
            key["deid"] = settings
        return sha256_hex(key)

    def _forget(self, stored: str, why: str) -> None:
        _log.warning("LLM cache entry %s; deleted, calling the model", why)
        try:
            self.store.cache_delete(stored)
        except Exception as exc:
            _log.warning("LLM cache delete failed (%s)", type(exc).__name__)

    async def _valid(self, entry: _Parsed, schema: Mapping[str, Any]) -> bool:
        full = full_output(entry.output, entry.intents, schema)
        try:
            found = self.validate(full, schema, timeout_s=self.validation_timeout_s)
            if inspect.isawaitable(found):
                found = await found
            return not tuple(found)
        except LlmError as exc:  # `validator_unavailable`: a miss, never a degraded step
            _log.warning("LLM cache entry could not be validated (%s)", exc.kind)
            return False
        except Exception as exc:  # fail closed: an entry that cannot be validated is a miss
            _log.warning("LLM cache validation failed (%s)", type(exc).__name__)
            return False

    async def lookup(
        self, key: str, *, model_alias: str, output_schema: Mapping[str, Any] | None
    ) -> PreviousLlm | None:
        if not self.read:
            return None
        now = self.clock()
        stored = self.stored_key(key, model_alias)
        try:
            entry = self.store.cache_get(stored, now=now)
        except Exception as exc:
            _log.warning("LLM cache lookup failed (%s); calling the model", type(exc).__name__)
            return None
        if entry is None or entry.created_at <= now - timedelta(seconds=self.config.ttl_s):
            return None
        parsed = _parse(entry.response)
        if parsed is None:
            self._forget(stored, "is malformed")
            return None
        if output_schema is not None and not await self._valid(parsed, output_schema):
            self._forget(stored, "fails the skill's output schema")
            return None
        spec = self.models.get(model_alias)
        backend, model = parsed.backend, parsed.model
        if spec is not None:  # the alias' current backend and model, not the stored ones
            backend, model = spec.backend, spec.model
        return PreviousLlm(
            input_hash=key,
            output=parsed.output,
            intents=parsed.intents,
            skill_version=parsed.skill_version,
            model=model,
            backend=backend,
            source_run=parsed.source_run,
        )

    def remember(self, record: RunRecord) -> bool:
        """Cache the record's accepted LLM output (see the module docstring); whether stored."""
        entry = accepted_entry(record)
        if entry is None:
            return False
        now = self.clock()
        response = {**entry.response, "created_at": now.isoformat()}
        try:
            return self.store.cache_put_safe(
                self.stored_key(entry.input_hash, entry.model_alias),
                skill=entry.skill,
                skill_version=response["skill_version"],
                model=response["model"],
                response=response,
                usage=entry.usage,
                at=now,
                expires_at=now + timedelta(seconds=self.config.ttl_s),
            )
        except Exception as exc:
            _log.warning("LLM cache write failed (%s); not cached", type(exc).__name__)
            return False

    def purge(self) -> None:
        """Delete expired entries, then the oldest beyond `max_entries` (best effort)."""
        now = self.clock()
        try:
            self.store.cache_purge(created_before=now - timedelta(seconds=self.config.ttl_s))
            self.store.cache_purge_expired(now)
            self.store.cache_trim(self.config.max_entries)
        except Exception as exc:
            _log.warning("LLM cache purge failed (%s)", type(exc).__name__)


__all__ = [
    "CACHE_VALIDATION_TIMEOUT_S",
    "AcceptedEntry",
    "CacheValidator",
    "StoreLlmCache",
    "accepted_entry",
    "default_validator",
    "full_output",
]
