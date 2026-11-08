# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures of the `/act` tests (#38): a key, a store with one task, a service over an
in-memory sink, a test client, and a worker function for the multi-process test."""

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from horae.api import ApiContext, create_app
from horae.api.act import ActKey, ActRoute, mint, state_digest
from horae.api.act._limit import RateLimiter
from horae.api.act._routes import ActSetup
from horae.api.act._service import ActService
from horae.apply import Applied, SinkCall, SinkRegistry, StaticSinks
from horae.apply.testing import FixtureSink
from horae.scheduler import SchedulerState
from horae.secrets import Secret
from horae.store import Store, TaskEntry

T0 = dt.datetime(2026, 11, 8, 7, 30, tzinfo=dt.UTC)
NOW = int(T0.timestamp())
PUBLIC = "https://horae.example.org"
HOST = "horae.example.org"
KEY_VALUE = "act-key-for-tests-0123456789-abcdefghijklmnopqrstuvwxyz"
OTHER_KEY_VALUE = "another-act-key-for-tests-9876543210-zyxwvutsrqponmlkjih"
TASK = "github:issue:123"
TITLE = "Renew the passport"
DUE = dt.date(2026, 11, 10)
TOOL = "vault.tasks"
ROUTE = ActRoute(TOOL, {"mode": "tasks"})
IDLE = SchedulerState("external", False, None, 1, ())


def key(value: str = KEY_VALUE) -> ActKey:
    return ActKey(Secret("HORAE_ACT_KEY", value))


class Clock:
    """A wall clock the test moves."""

    def __init__(self, at: dt.datetime = T0) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += dt.timedelta(**delta)


def seed(store_path: Path, *, title: str = TITLE, fingerprint: str = TASK) -> None:
    """A store holding one `todo` task due `DUE`."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with Store.open(store_path) as store:
        store.upsert_task(
            TaskEntry(fingerprint=fingerprint, title=title, status="todo", due=DUE), at=T0
        )


def token(
    kind: str = "task.done",
    *,
    duration: str | None = None,
    at: int = NOW,
    status: str = "todo",
    due: dt.date | None = DUE,
    task: str = TASK,
    value: str = KEY_VALUE,
) -> str:
    """A token minted for the seeded task in its seeded state."""
    return mint(
        key(value),
        kind=kind,
        task=task,
        state=state_digest(status, due, None),
        now=at,
        duration=duration,
    )


@dataclass
class SlowSink(FixtureSink):
    """A `FixtureSink` whose `apply` takes `delay` seconds (to overlap concurrent requests),
    raises `boom`, or answers `answer` (as plugin-obsidian does for a recurring task)."""

    delay: float = 0.0
    boom: BaseException | None = None
    answer: Applied | None = None
    calls: list[SinkCall] = field(default_factory=list)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom is not None:
            raise self.boom
        if self.answer is not None:
            return self.answer
        return await super().apply(call, expected_before_sha256=expected_before_sha256)


def sinks_of(sink: FixtureSink) -> Callable[[], Any]:
    @asynccontextmanager
    async def open_sinks() -> AsyncIterator[SinkRegistry]:
        yield StaticSinks({TOOL: sink})

    return open_sinks


@dataclass
class Env:
    store_path: Path
    state_dir: Path
    sink: SlowSink
    clock: Clock
    service: ActService
    limiter: RateLimiter

    def app(self, *, public_url: str | None = PUBLIC, service: ActService | None = None) -> Any:
        setup = ActSetup(service or self.service, public_url, limiter=self.limiter)
        context = ApiContext(
            self.store_path, self.state_dir, lambda: IDLE, frozenset({HOST}), setup
        )
        return create_app(context)

    def client(self, **kwargs: Any) -> TestClient:
        return TestClient(self.app(**kwargs), base_url=PUBLIC, raise_server_exceptions=False)


def make_env(tmp_path: Path, *, limiter: RateLimiter | None = None, value: str = KEY_VALUE) -> Env:
    state_dir = tmp_path / "state"
    store_path = state_dir / "horae.db"
    seed(store_path)
    sink = SlowSink()
    clock = Clock()
    service = ActService(
        key(value),
        store_path,
        state_dir,
        ROUTE,
        sinks_of(sink),
        timezone="Europe/Paris",
        call_timeout_s=5.0,
        lock_wait_s=0.3,
        clock=clock,
    )
    roomy = RateLimiter(
        per_ip_per_min=6000, per_ip_burst=1000, global_per_min=6000, global_burst=1000
    )
    return Env(store_path, state_dir, sink, clock, service, limiter or roomy)


def post_in_process(state_dir: str, store_path: str, marker: str, tok: str, delay: float) -> str:
    """One `POST` in its own process (the multi-process test): its outcome. The sink appends
    a line to `marker` when it applies."""

    class MarkerSink(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            await asyncio.sleep(delay)
            with open(marker, "a", encoding="utf-8") as out:
                out.write(f"{call.intent.type}\n")
            return Applied(TOOL)

    service = ActService(
        key(),
        Path(store_path),
        Path(state_dir),
        ROUTE,
        sinks_of(MarkerSink()),
        lock_wait_s=10.0,
        clock=Clock(),
    )
    return asyncio.run(service.act(tok)).outcome
