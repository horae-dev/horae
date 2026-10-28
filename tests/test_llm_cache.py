# SPDX-License-Identifier: Apache-2.0
"""The LLM cache and the $0 path (#32, schemas decision 45): an unchanged input makes no LLM
call (across runs and processes), entries expire by policy, `--no-cache` skips the lookup,
only accepted outputs are cached, secrets are never cached, and cached runs replay exactly."""

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from horae import cli_run
from horae.apply import StaticSinks
from horae.cli import app
from horae.config import Instance, load_instance
from horae.contracts import CacheConfig, InstanceConfig, RunRecord
from horae.engine import LlmRequest, LlmResponse, PreviousLlm
from horae.engine.testing import FakeLlm, llm_response
from horae.replay import replay
from horae.runner import Ports, Runner, StoreLlmCache, accepted_entry
from horae.secrets import Secret, redact
from horae.store import Store, StoreError
from runner_fixtures import Plugins, instance, write_instance

FIXTURES = Path(__file__).resolve().parent / "runner_fixtures.py"
T0 = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)


class CountingLlm:
    """A fake backend that answers every request and counts the calls."""

    def __init__(self, output: Any = None, intents: Any = None) -> None:
        self.output = {"items": ["Review PR 42"]} if output is None else output
        self.intents = intents
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return llm_response(self.output, intents=self.intents)

    @property
    def calls(self) -> int:
        return len(self.requests)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    return instance(tmp_path)


@pytest.fixture
def store(inst: Instance) -> Iterator[Store]:
    with Store.open(inst.store_path) as opened:
        yield opened


def _run(
    inst: Instance,
    store: Store,
    llm: Any,
    *,
    plugins: Plugins | None = None,
    clock: Clock | None = None,
    **kwargs: Any,
) -> RunRecord:
    plugins = plugins or Plugins()
    runner = Runner(inst, store, Ports(plugins, plugins, llm), clock=clock or Clock())
    return asyncio.run(runner.run("brief", **kwargs))


def _with(inst: Instance, **config: Any) -> Instance:
    data = {**inst.config.model_dump(mode="json", exclude_none=True), **config}
    return Instance(
        root=inst.root,
        config=InstanceConfig.model_validate(data),
        pipelines=inst.pipelines,
        lock=inst.lock,
    )


# --- unchanged input: no call ------------------------------------------------------------------


def test_unchanged_input_makes_no_llm_call(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    first = _run(inst, store, llm)
    assert first.status == "ok"
    assert llm.calls == 1
    assert first.input_hash is not None
    entry = store.cache_get(first.input_hash, now=T0 + timedelta(minutes=1))
    assert entry is not None
    assert entry.skill == "pack/brief"
    assert entry.model == "fixture-model"
    assert entry.response["output"] == {"items": ["Review PR 42"]}
    assert entry.usage.tokens_out == 10
    ledger = store.usage_report()
    for _ in range(3):
        again = _run(inst, store, llm)
        assert again.status == "noop"
        assert again.input_hash == first.input_hash
        (call,) = again.llm
        assert call.cached is True
        assert call.usage.usd == 0.0
        assert again.cost.usd == 0.0
    assert llm.calls == 1
    # Charged zero: no ledger row for the cached runs.
    assert store.usage_report() == ledger


def test_any_earlier_input_is_reused_not_only_the_last(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    a, b = Plugins(), Plugins()
    b.handlers["fixture.items"] = lambda p, i: [{"title": "Other"}]
    _run(inst, store, llm, plugins=a)
    _run(inst, store, llm, plugins=b)
    assert llm.calls == 2
    again = _run(inst, store, llm, plugins=a)
    assert llm.calls == 2
    assert again.llm[0].cached is True


def test_unchanged_input_makes_no_call_across_processes(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")

    def child() -> dict[str, Any]:
        done = subprocess.run(  # noqa: S603 (this interpreter, a test fixture)
            [sys.executable, str(FIXTURES), "--brief", str(root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        result: dict[str, Any] = json.loads(done.stdout)
        return result

    first, second = child(), child()
    assert (first["status"], first["llm_calls"]) == ("ok", 1)
    assert (second["status"], second["llm_calls"]) == ("noop", 0)


# --- what makes a miss -------------------------------------------------------------------------


def test_changed_input_plugin_or_model_calls_the_llm(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    _run(inst, store, llm)
    changed = Plugins()
    changed.handlers["fixture.items"] = lambda p, i: [{"title": "Review PR 43"}]
    _run(inst, store, llm, plugins=changed)
    assert llm.calls == 2
    # A new version of the skill's plugin (part of the key, with the skill's version and
    # digest when a skill is loaded: tests/test_engine_skills.py).
    bumped = Plugins()
    bumped.add({**bumped.plugins["pack"].manifest.model_dump(), "version": "0.2.0"})
    _run(inst, store, llm, plugins=bumped)
    assert llm.calls == 3
    # Another concrete model behind the same alias.
    other = _with(inst, models={"default": {"backend": "claude-cli", "model": "opus"}})
    _run(other, store, llm)
    assert llm.calls == 4
    # Each of them is now cached.
    _run(other, store, llm)
    _run(inst, store, llm, plugins=bumped)
    assert llm.calls == 4


# --- expiry ------------------------------------------------------------------------------------


def test_entries_expire_after_their_ttl(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    clock = Clock()
    _run(inst, store, llm, clock=clock)
    clock.at += timedelta(days=7) - timedelta(minutes=1)
    _run(inst, store, llm, clock=clock)
    assert llm.calls == 1
    clock.at += timedelta(minutes=2)  # the default ttl_s (7 days) is over
    record = _run(inst, store, llm, clock=clock)
    assert llm.calls == 2
    assert record.llm[0].cached is False
    # The fresh output is cached again; a hit does not extend its life.
    _run(inst, store, llm, clock=clock)
    assert llm.calls == 2


def test_a_lower_ttl_applies_to_existing_entries(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    clock = Clock()
    _run(inst, store, llm, clock=clock)
    clock.at += timedelta(hours=2)
    _run(_with(inst, cache={"ttl_s": 3600}), store, llm, clock=clock)
    assert llm.calls == 2


def test_runs_purge_expired_and_surplus_entries(inst: Instance, store: Store) -> None:
    from horae.contracts import Usage

    usage = Usage(tokens_in=0, tokens_out=0, usd=0.0)
    for n in range(5):
        store.cache_put(
            f"{n:064x}",
            skill="pack/brief",
            skill_version="1",
            model="m",
            response={},
            usage=usage,
            at=T0 - timedelta(days=n * 3),
            expires_at=T0 + timedelta(days=30),
        )
    llm = CountingLlm()
    record = _run(_with(inst, cache={"max_entries": 2}), store, llm, clock=Clock())
    assert record.input_hash is not None
    # 3 and 4 were older than the ttl (9 and 12 days), then only 2 entries are kept: the new
    # one and the newest of the rest.
    now = T0 + timedelta(minutes=1)
    kept = [n for n in range(5) if store.cache_get(f"{n:064x}", now=now) is not None]
    assert kept == [0, 1]  # purged before the run, which then added its own
    assert store.cache_get(record.input_hash, now=now) is not None
    assert store.cache_trim(1) == 2
    assert store.cache_get(record.input_hash, now=now) is not None
    with pytest.raises(ValueError, match="max_entries"):
        store.cache_trim(-1)


# --- what is cached ----------------------------------------------------------------------------


def test_unaccepted_outputs_are_not_cached(inst: Instance, store: Store) -> None:
    failing = FakeLlm([llm_response({"x": 1}, schema_errors=["bad"])] * 2)
    degraded = _run(inst, store, failing)
    assert degraded.status == "degraded"
    llm = CountingLlm()
    _run(inst, store, llm)
    assert llm.calls == 1


def test_a_dry_run_reads_the_cache_but_never_writes_it(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    dry = _run(inst, store, llm, dry_run=True)
    assert llm.calls == 1
    assert dry.input_hash is not None
    assert store.cache_get(dry.input_hash, now=T0) is None
    _run(inst, store, llm)
    assert llm.calls == 2
    dry = _run(inst, store, llm, dry_run=True)
    assert llm.calls == 2
    assert dry.llm[0].cached is True


def test_a_response_holding_a_secret_is_not_cached(inst: Instance, store: Store) -> None:
    value = "synthetic-cache-secret-123456"  # synthetic
    redact.remember(Secret("CACHE_TOKEN", value))
    llm = CountingLlm(output={"items": [f"token {value}"]})
    first = _run(inst, store, llm)
    assert first.input_hash is not None
    assert store.cache_get(first.input_hash, now=T0 + timedelta(hours=1)) is None
    _run(inst, store, llm)
    assert llm.calls == 2


def test_accepted_entry_rules(inst: Instance, store: Store) -> None:
    record = _run(inst, store, CountingLlm())
    assert accepted_entry(record) is not None
    assert accepted_entry(record.model_copy(update={"status": "failed"})) is None
    assert accepted_entry(record.model_copy(update={"input_hash": None})) is None
    no_intents = record.llm[0].model_copy(update={"intents": None})
    assert accepted_entry(record.model_copy(update={"llm": [no_intents]})) is None
    reused = _run(inst, store, CountingLlm())
    assert accepted_entry(reused) is None
    assert accepted_entry(record.model_copy(update={"llm": []})) is None
    collect = asyncio.run(Runner(inst, store, Ports(Plugins(), Plugins())).run("collect"))
    assert accepted_entry(collect) is None


# --- --no-cache --------------------------------------------------------------------------------


def test_no_cache_calls_the_llm_and_refreshes_the_entry(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    clock = Clock()
    first = _run(inst, store, llm, clock=clock)
    assert first.input_hash is not None
    created = store.cache_get(first.input_hash, now=clock.at)
    llm.output = {"items": ["Reworded"]}
    forced = _run(inst, store, llm, clock=clock, cache=False)
    assert llm.calls == 2
    assert forced.llm[0].cached is False
    refreshed = store.cache_get(first.input_hash, now=clock.at)
    assert created is not None
    assert refreshed is not None
    assert refreshed.created_at > created.created_at
    assert refreshed.response["output"] == {"items": ["Reworded"]}
    reused = _run(inst, store, llm, clock=clock)
    assert llm.calls == 2
    assert reused.llm[0].response == {"items": ["Reworded"]}


def test_horae_run_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = write_instance(tmp_path / "instance")
    llm = CountingLlm()
    plugins = Plugins()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def live_ports(instance: Instance) -> Any:
        yield SimpleNamespace(ports=Ports(plugins, plugins, llm), sinks=StaticSinks())

    monkeypatch.setattr(cli_run, "live_ports", live_ports)
    runner = CliRunner()

    def status(*args: str) -> str:
        result = runner.invoke(app, ["run", "brief", "-i", str(root), "--json", *args])
        assert result.exit_code == 0, result.output
        value: str = json.loads(result.stdout)["run"]["status"]
        return value

    assert status() == "ok"
    assert status() == "noop"
    assert llm.calls == 1
    assert status("--no-cache") == "ok"
    assert llm.calls == 2


# --- failures are misses -----------------------------------------------------------------------


def test_store_failures_are_logged_misses(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("synthetic secret-looking detail")

    llm = CountingLlm()
    _run(inst, store, llm)
    for name in ("cache_get", "cache_put_safe", "cache_purge"):
        monkeypatch.setattr(store, name, broken)
    with caplog.at_level(logging.WARNING, "horae.runner"):
        record = _run(inst, store, llm)
    assert record.status == "ok"
    assert llm.calls == 2
    assert "LLM cache lookup failed (StoreError)" in caplog.text
    assert "LLM cache write failed (StoreError)" in caplog.text
    assert "LLM cache purge failed (StoreError)" in caplog.text
    assert "secret-looking" not in caplog.text


@pytest.mark.parametrize("answer", ["raise", "other-key", "not-an-entry"])
def test_the_engine_treats_a_broken_cache_as_a_miss(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    llm = CountingLlm()
    first = _run(inst, store, llm)
    assert first.input_hash is not None
    other = PreviousLlm("d" * 64, {"items": []}, [], "1", "m", "anthropic")

    def lookup(self: StoreLlmCache, key: str) -> Any:
        if answer == "raise":
            raise RuntimeError("synthetic")
        return other if answer == "other-key" else {"output": 1}

    monkeypatch.setattr(StoreLlmCache, "lookup", lookup)
    record = _run(inst, store, llm)
    assert llm.calls == 2
    assert record.status == "ok"
    assert record.llm[0].cached is False


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"intents": [], "backend": "anthropic", "model": "m", "skill_version": "1"},
        {"output": 1, "intents": {}, "backend": "anthropic", "model": "m", "skill_version": "1"},
        {"output": 1, "intents": [], "backend": "nope", "model": "m", "skill_version": "1"},
        {"output": 1, "intents": [], "backend": "anthropic", "model": 1, "skill_version": "1"},
        {"output": 1, "intents": [], "backend": "anthropic", "model": "m", "skill_version": 1},
    ],
)
def test_a_malformed_entry_is_a_miss(store: Store, response: Any) -> None:
    from horae.contracts import Usage

    key = "c" * 64
    store.cache_put(
        key,
        skill="pack/brief",
        skill_version="1",
        model="m",
        response=response,
        usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
        at=T0,
    )
    cache = StoreLlmCache(store, CacheConfig(), lambda: T0 + timedelta(seconds=1), read=True)
    assert cache.lookup(key) is None
    good = {"output": 1, "intents": [], "backend": "anthropic", "model": "m", "skill_version": "1"}
    store.cache_put(
        key,
        skill="pack/brief",
        skill_version="1",
        model="m",
        response=good,
        usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
        at=T0,
    )
    hit = cache.lookup(key)
    assert isinstance(hit, PreviousLlm)
    assert hit.input_hash == key
    off = StoreLlmCache(store, CacheConfig(), lambda: T0 + timedelta(seconds=1), read=False)
    assert off.lookup(key) is None


# --- replay ------------------------------------------------------------------------------------


def test_a_cached_run_replays_identically(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    first = _run(inst, store, llm)
    cached = _run(inst, store, llm)
    assert cached.llm[0].cached is True
    plugins = Plugins()
    for record in (first, cached):
        result = replay(
            record,
            inst.pipelines["brief"],
            plugins=plugins,
            lock_digest=inst.lock_digest(),
            config_digest=inst.config_digest(),
        )
        assert result.outcome == "identical", result
    assert llm.calls == 1


# --- the contract ------------------------------------------------------------------------------


def test_cache_config_defaults_and_bounds() -> None:
    config = InstanceConfig.model_validate({})
    assert config.cache == CacheConfig(ttl_s=7 * 86400, max_entries=1000)
    for bad in ({"ttl_s": 59}, {"ttl_s": 366 * 86400 + 1}, {"max_entries": 0}, {"ttl_s": 1.5}):
        with pytest.raises(ValueError, match="cache"):
            InstanceConfig.model_validate({"cache": bad})


def test_the_cache_section_loads_from_horae_yaml(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance", cache={"ttl_s": 3600, "max_entries": 10})
    loaded = load_instance(root)
    assert loaded.config.cache == CacheConfig(ttl_s=3600, max_entries=10)
