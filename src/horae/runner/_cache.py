# SPDX-License-Identifier: Apache-2.0
"""The LLM cache: the $0 path on unchanged input (#32, schemas decision 45).

The store's `llm_cache` table is the single source of reusable LLM outputs for live runs. An
entry is keyed by the engine's cache key (`RunRecord.input_hash`: SHA-256 of the canonical
JSON of the skill, its version and digest, the skill plugin's name, version and commit, the
resolved model and the viewed input) and holds what the engine needs to reuse the output
without a call: `{"output", "intents", "backend", "model", "skill_version"}`.

- **Lookup** (`StoreLlmCache.lookup`, the engine's `LlmCache` port): an entry is live when it
  has not expired (`expires_at`) and was created less than `cache.ttl_s` ago (the current
  setting, so lowering it takes effect at once). A hit makes no backend call, so the usage
  ledger is not touched: a cached output costs nothing. A store error or a malformed entry is
  a miss (logged by type).
- **Write** (`StoreLlmCache.remember`, after the run's record is saved): only an **accepted**
  output: the run is not `failed` and not a dry run, its `llm` step is `ok`, and the step's
  last call is schema-valid, has recorded intents and is not itself a reuse (a hit never
  extends an entry's life). `Store.cache_put_safe` refuses an entry that holds a secret's
  value: it is then simply not cached. A store error is logged, never fails the run.
- **Purge** (`StoreLlmCache.purge`, at the start of each non-dry run): expired entries, then
  the oldest beyond `cache.max_entries`; best effort.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from horae.contracts import RunRecord, Usage
from horae.contracts.instance import CacheConfig
from horae.engine import PreviousLlm
from horae.store import Store

_log = logging.getLogger("horae.runner")

_BACKENDS = frozenset({"claude-cli", "anthropic", "openai-compatible", "ollama"})


def _is_reuse(request: Any) -> bool:
    return isinstance(request, dict) and isinstance(request.get("reused_input_hash"), str)


def accepted_entry(record: RunRecord) -> tuple[str, str, dict[str, Any], Usage] | None:
    """`(key, skill, response, usage)` of the record's accepted LLM output, if it may be cached."""
    key = record.input_hash
    if key is None or record.dry_run or record.status == "failed":
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
    }
    return key, call.skill, response, usage


class StoreLlmCache:
    """The engine's `LlmCache` over `Store`'s `llm_cache` table, with the instance's policy."""

    def __init__(
        self, store: Store, config: CacheConfig, clock: Callable[[], datetime], *, read: bool
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.read = read
        """`False` under `horae run --no-cache`: every lookup is a miss."""

    def lookup(self, key: str) -> PreviousLlm | None:
        if not self.read:
            return None
        now = self.clock()
        try:
            entry = self.store.cache_get(key, now=now)
        except Exception as exc:
            _log.warning("LLM cache lookup failed (%s); calling the model", type(exc).__name__)
            return None
        if entry is None or entry.created_at <= now - timedelta(seconds=self.config.ttl_s):
            return None
        response = entry.response
        if not isinstance(response, dict):
            return None
        backend, model, version = (
            response.get("backend"),
            response.get("model"),
            response.get("skill_version"),
        )
        if (
            "output" not in response
            or not isinstance(response.get("intents"), list)
            or backend not in _BACKENDS
            or not isinstance(model, str)
            or not isinstance(version, str)
        ):
            return None
        return PreviousLlm(
            input_hash=entry.key,
            output=response["output"],
            intents=response["intents"],
            skill_version=version,
            model=model,
            backend=backend,
        )

    def remember(self, record: RunRecord) -> bool:
        """Cache the record's accepted LLM output (see the module docstring); whether stored."""
        entry = accepted_entry(record)
        if entry is None:
            return False
        key, skill, response, usage = entry
        now = self.clock()
        try:
            return self.store.cache_put_safe(
                key,
                skill=skill,
                skill_version=response["skill_version"],
                model=response["model"],
                response=response,
                usage=usage,
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


__all__ = ["StoreLlmCache", "accepted_entry"]
