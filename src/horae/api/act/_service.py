# SPDX-License-Identifier: Apache-2.0
"""What `GET` and `POST /act/<token>` do (#38, schemas decision 53 D to G, K).

**`preview` (`GET`) writes nothing.** It verifies the token (format, tag, key id, expiry),
then reads the store **read-only** (`Store.open(read_only=True, immutable=False)`): not yet
used, the task known. It shows the confirmation page, or the "task changed" page when the
task's `(status, due, snoozed_until)` no longer matches the token. No nonce, run, journal
entry or log line with the token: a link preview, a prefetcher or a scanner leaves the token
usable.

**`act` (`POST`) applies once**, in this order:

1. verify the token (as `GET`); any failure is the 404 page;
2. take the instance's apply lock (decision 34 D6, shared with the runs' apply phase and
   `horae undo`), waiting at most `LOCK_WAIT_S`, else 503 and the token stays usable; then
   the `_act` pipeline lock, and recover a `_act` run a crashed process left `running`;
3. delete the used-token rows that expired more than an hour ago (one bounded `DELETE`);
4. refuse (404, nothing written) a used token or a task the index does not know;
5. open the sinks (the plugin host) and find the route's sink, before anything is written:
   a sink that cannot be reached is 503 and the token stays usable;
6. begin a run of the reserved pipeline `_act`; if the task's state is not the token's, the
   token is consumed with outcome `stale`, nothing is applied, the run ends `failed`
   (`target_changed`) and the page says the task changed (409);
7. else, in **one transaction**, consume the nonce (`pending`; a primary-key conflict means
   used: 404) and journal the `task.update_status` the core built from the token's kind
   (never from the request); commit;
8. apply it through the sink, bounded by `call_timeout_s`; mark the journal entry applied,
   the outcome `applied`, update the task index, end the run `ok` (200); a sink failure
   leaves the token used (`failed`, single attempt), the run `failed` with the error kind,
   and the page says so (502).

A crash after step 7 leaves the token used and the change journalled "maybe applied": at most
once, never twice; the next `POST` recovers the run (`failed`, abandoned).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from horae.api.act import _page as page
from horae.api.act._key import ActKey
from horae.api.act._route import ActRoute
from horae.api.act._token import ActToken, state_digest, verify
from horae.apply.apply import index_task
from horae.apply.sink import Sink, SinkCall, SinkError, SinkRegistry, error_kind, prepare_sinks
from horae.apply.sink import sink_identity as read_identity
from horae.contracts import Usage
from horae.contracts.intents import TaskUpdateStatus
from horae.runner import PipelineLock, new_run_id, recover_pipeline, try_acquire_apply_lock
from horae.store import ACT_PIPELINE, ActTokenUsed, Store, TaskEntry

LOCK_WAIT_S = 3.0
"""How long a `POST` waits for the apply lock before answering 503 (the token stays usable)."""
_LOCK_POLL_S = 0.05
PURGE_AFTER_S = 3600
"""Used-token rows are kept this long after their token expired (a backward clock step)."""
_NO_COST = Usage(tokens_in=0, tokens_out=0, usd=0.0)

Outcome = Literal[
    "invalid", "used", "unknown", "shown", "changed", "busy", "applied", "stale", "failed"
]
"""What happened, for the log line (`invalid`, `used` and `unknown` are the same 404)."""


@dataclass(frozen=True)
class Result:
    page: page.Page
    outcome: Outcome
    token: ActToken | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


SinksFactory = Callable[[], AbstractAsyncContextManager[SinkRegistry]]
"""Opens the instance's sinks for one `POST` (`horae.cli_host.host_sinks`)."""


@dataclass
class ActService:
    """The `/act` logic of one instance (see the module docstring)."""

    key: ActKey
    store_path: Path
    state_dir: Path
    route: ActRoute
    sinks: SinksFactory
    timezone: str = "UTC"
    call_timeout_s: float | None = 30.0
    lock_wait_s: float = LOCK_WAIT_S
    clock: Callable[[], datetime] = field(default=_utc_now)

    # --- GET -------------------------------------------------------------------------------

    def preview(self, token: str) -> Result:
        """The `GET` page (synchronous: run it in a worker thread). Writes nothing."""
        parsed = verify(self.key, token, int(self.clock().timestamp()))
        if parsed is None:
            return Result(page.NOT_VALID, "invalid")
        if not self.store_path.is_file():
            return Result(page.NOT_VALID, "unknown", parsed)
        with Store.open(self.store_path, read_only=True, immutable=False) as store:
            if store.act_outcome(parsed.nonce) is not None:
                return Result(page.NOT_VALID, "used", parsed)
            task = store.get_task(parsed.task)
        if task is None:
            return Result(page.NOT_VALID, "unknown", parsed)
        if _state(task) != parsed.state:
            return Result(page.stale_preview(task.title, task.due), "changed", parsed)
        shown = page.confirm(parsed.kind, parsed.duration, task.title, task.due)
        return Result(shown, "shown", parsed)

    # --- POST ------------------------------------------------------------------------------

    async def act(self, token: str) -> Result:
        """The `POST` page (see the module docstring)."""
        parsed = verify(self.key, token, int(self.clock().timestamp()))
        if parsed is None:
            return Result(page.NOT_VALID, "invalid")
        async with self._locked() as locked:
            if not locked:
                return Result(page.BUSY, "busy", parsed)
            with Store.open(self.store_path) as store:
                return await self._act(store, parsed)

    @contextlib.asynccontextmanager
    async def _locked(self) -> AsyncIterator[bool]:
        """The apply lock, then the `_act` pipeline lock; `False` when either is busy."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lock_wait_s
        apply_lock = try_acquire_apply_lock(self.state_dir)
        while apply_lock is None and loop.time() < deadline:
            await asyncio.sleep(_LOCK_POLL_S)
            apply_lock = try_acquire_apply_lock(self.state_dir)
        if apply_lock is None:
            yield False
            return
        with apply_lock:
            act_lock = PipelineLock.try_acquire(self.state_dir, ACT_PIPELINE)
            if act_lock is None:
                yield False
                return
            with act_lock:
                yield True

    async def _act(self, store: Store, token: ActToken) -> Result:
        now = self.clock()
        recover_pipeline(store, ACT_PIPELINE, clock=self.clock)
        store.act_purge(int(now.timestamp()) - PURGE_AFTER_S)
        if store.act_outcome(token.nonce) is not None:
            return Result(page.NOT_VALID, "used", token)
        task = store.get_task(token.task)
        if task is None:
            return Result(page.NOT_VALID, "unknown", token)
        stack = contextlib.AsyncExitStack()
        try:
            registry = await stack.enter_async_context(self.sinks())
            sink = registry.sink_for(self.route.tool)
            if sink is None:
                raise SinkError("no_sink", "no sink serves the tasks entry")
            await prepare_sinks(registry, (self.route.tool,))
            identity = read_identity(sink)
        except Exception:
            # The plugin host could not start, or the sink is missing or has no readable
            # identity: nothing was written, the token stays usable.
            await stack.aclose()
            return Result(page.UNAVAILABLE, "busy", token)
        async with stack:
            return await self._apply(store, token, task, sink, identity)

    def until(self, token: ActToken, now: datetime) -> datetime | None:
        """When a snooze ends, computed now (maintainer decision 6): an hour or three from
        now, or 09:00 tomorrow in the instance time zone; `None` for `task.done`."""
        if token.kind == "task.done":
            return None
        now = now.astimezone(UTC).replace(microsecond=0)
        if token.duration == "1h":
            return now + timedelta(hours=1)
        if token.duration == "3h":
            return now + timedelta(hours=3)
        zone = ZoneInfo(self.timezone)
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, clock_time(9, 0), tzinfo=zone)

    async def _apply(
        self, store: Store, token: ActToken, task: TaskEntry, sink: Sink, identity: str | None
    ) -> Result:
        now = self.clock()
        run_id = new_run_id(ACT_PIPELINE, now)
        store.begin_run(run_id, ACT_PIPELINE, now)
        if _state(task) != token.state:
            store.act_consume(
                token.nonce,
                kid=token.kid,
                expires_at=token.expires_at,
                at=now,
                run_id=run_id,
                outcome="stale",
            )
            error = "target_changed: the task changed since the link was minted; nothing applied"
            store.finish_bare_run(
                run_id, status="failed", at=self.clock(), cost=_NO_COST, error=error
            )
            return Result(page.stale(task.title), "stale", token)
        until = self.until(token, now)
        intent = TaskUpdateStatus(
            type="task.update_status",
            task=token.task,
            status="done" if token.kind == "task.done" else "snoozed",
            until=until,
        )
        try:
            with store.transaction():
                store.act_consume(
                    token.nonce,
                    kid=token.kid,
                    expires_at=token.expires_at,
                    at=now,
                    run_id=run_id,
                    outcome="pending",
                )
                entry = store.journal_append(
                    run_id,
                    intent=0,
                    intent_type="task.update_status",
                    target=token.task,
                    undoable=False,
                    applied_by=self.route.tool,
                    sink_identity=identity,
                    at=now,
                )
        except ActTokenUsed:
            error = "already_used: the link was used meanwhile; nothing applied"
            store.finish_bare_run(
                run_id, status="failed", at=self.clock(), cost=_NO_COST, error=error
            )
            return Result(page.NOT_VALID, "used", token)
        call = SinkCall(run_id, 0, self.route.tool, dict(self.route.params), intent)
        kind: str | None = None
        try:
            async with asyncio.timeout(self.call_timeout_s):
                await sink.apply(call, expected_before_sha256=None)
        except SinkError as exc:
            kind = error_kind(exc.kind)
        except TimeoutError:
            kind = "timeout"
        except Exception:
            kind = "host_error"
        if kind is not None:
            store.act_set_outcome(token.nonce, "failed")
            error = f"{kind}: the sink did not apply the task change; the link is used up"
            store.finish_bare_run(
                run_id, status="failed", at=self.clock(), cost=_NO_COST, error=error
            )
            return Result(page.failed(task.title), "failed", token)
        with store.transaction():
            store.journal_mark_applied(entry.id)
            store.act_set_outcome(token.nonce, "applied")
        index_task(
            store,
            intent,
            tool=self.route.tool,
            run_id=run_id,
            at=self.clock(),
            timezone=self.timezone,
        )
        store.finish_bare_run(run_id, status="ok", at=self.clock(), cost=_NO_COST)
        shown = None if until is None else until.astimezone(ZoneInfo(self.timezone))
        text = None if shown is None else shown.strftime("%Y-%m-%d %H:%M")
        return Result(page.applied(token.kind, token.duration, text, task.title), "applied", token)


def _state(task: TaskEntry) -> bytes:
    return state_digest(task.status, task.due, task.snoozed_until)


__all__ = ["LOCK_WAIT_S", "PURGE_AFTER_S", "ActService", "Outcome", "Result", "SinksFactory"]
