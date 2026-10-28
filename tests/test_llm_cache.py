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
from horae.llm.deid import Deidentifier
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


def _cache(inst: Instance, store: Store, **kwargs: Any) -> StoreLlmCache:
    kwargs.setdefault("deid", Deidentifier(inst.config.models))  # as the runner builds it
    return StoreLlmCache(
        store, inst.config.cache, Clock(), read=True, models=inst.config.models, **kwargs
    )


def _entry(inst: Instance, store: Store, input_hash: str | None, now: datetime) -> Any:
    """The entry the runner stored for the engine key `input_hash` (alias `default`)."""
    assert input_hash is not None
    return store.cache_get(_cache(inst, store).stored_key(input_hash, "default"), now=now)


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
    entry = _entry(inst, store, first.input_hash, T0 + timedelta(minutes=1))
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
    assert _entry(inst, store, record.input_hash, now) is not None
    assert store.cache_trim(1) == 2
    assert _entry(inst, store, record.input_hash, now) is not None
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
    assert _entry(inst, store, dry.input_hash, T0) is None
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
    assert _entry(inst, store, first.input_hash, T0 + timedelta(hours=1)) is None
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
    created = _entry(inst, store, first.input_hash, clock.at)
    llm.output = {"items": ["Reworded"]}
    forced = _run(inst, store, llm, clock=clock, cache=False)
    assert llm.calls == 2
    assert forced.llm[0].cached is False
    refreshed = _entry(inst, store, first.input_hash, clock.at)
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

    async def lookup(self: StoreLlmCache, key: str, **kwargs: Any) -> Any:
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

    cache = StoreLlmCache(store, CacheConfig(), lambda: T0 + timedelta(seconds=1), read=True)
    key = "c" * 64
    stored = cache.stored_key(key, "default")

    def put(value: Any) -> None:
        store.cache_put(
            stored,
            skill="pack/brief",
            skill_version="1",
            model="m",
            response=value,
            usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
            at=T0,
        )

    def lookup(on: StoreLlmCache) -> PreviousLlm | None:
        return asyncio.run(on.lookup(key, model_alias="default", output_schema=None))

    put(response)
    assert lookup(cache) is None
    assert store.cache_get(stored, now=T0) is None  # a malformed entry is deleted
    good = {
        "output": 1,
        "intents": [],
        "backend": "anthropic",
        "model": "m",
        "skill_version": "1",
        "run_id": "r1",
    }
    put(good)
    hit = lookup(cache)
    assert isinstance(hit, PreviousLlm)
    assert hit.input_hash == key
    # No alias in `horae.yaml`: the stored backend and model; the producing run.
    assert (hit.backend, hit.model, hit.source_run) == ("anthropic", "m", "r1")
    off = StoreLlmCache(store, CacheConfig(), lambda: T0 + timedelta(seconds=1), read=False)
    assert lookup(off) is None


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


# --- review fixes (#32): provenance, stored key, re-validation, undo, purge ---------------------

PREAMBLE_SHA256 = "9e01dd06cf4c416bf0522fe4b6eda56d0477e7e494b6513cc2bdd48b6c1fa4a8"


def test_an_entry_records_its_producer_and_a_hit_names_it(inst: Instance, store: Store) -> None:
    llm = CountingLlm()
    first = _run(inst, store, llm)
    entry = _entry(inst, store, first.input_hash, T0 + timedelta(minutes=1))
    assert entry.response["run_id"] == first.id
    assert entry.response["input_hash"] == first.input_hash
    assert datetime.fromisoformat(entry.response["created_at"]) == entry.created_at
    again = _run(inst, store, llm)
    (call,) = again.llm
    assert call.request == {
        "skill": "pack/brief",
        "model": "default",
        "reused_input_hash": first.input_hash,
        "source_run": first.id,
    }
    # The record keeps the engine key, not the stored one.
    assert again.input_hash == first.input_hash
    assert (call.backend, call.model) == ("claude-cli", "sonnet")


def test_a_hit_names_the_current_backend_and_model(inst: Instance, store: Store) -> None:
    from horae.contracts import Usage

    cache = _cache(inst, store)
    key = "e" * 64
    store.cache_put(
        cache.stored_key(key, "default"),
        skill="pack/brief",
        skill_version="1",
        model="old",
        response={
            "output": {},
            "intents": [],
            "backend": "anthropic",
            "model": "old",
            "skill_version": "1",
        },
        usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
        at=T0 + timedelta(seconds=1),
    )
    cache.clock = lambda: T0 + timedelta(seconds=2)
    hit = asyncio.run(cache.lookup(key, model_alias="default", output_schema=None))
    assert hit is not None
    assert (hit.backend, hit.model, hit.source_run) == ("claude-cli", "sonnet", None)


def test_another_backend_or_endpoint_behind_the_alias_is_a_miss(
    inst: Instance, store: Store
) -> None:
    llm = CountingLlm()
    _run(inst, store, llm)
    lan_a = {"backend": "ollama", "model": "sonnet", "base_url": "http://192.168.1.10:11434"}
    lan_b = {**lan_a, "base_url": "http://192.168.1.11:11434"}
    _run(_with(inst, models={"default": lan_a}), store, llm)  # another backend, same model
    assert llm.calls == 2
    _run(_with(inst, models={"default": lan_b}), store, llm)  # another endpoint
    assert llm.calls == 3
    _run(_with(inst, models={"default": lan_a}), store, llm)
    assert llm.calls == 3
    # A price is not part of the key: what the model answers does not depend on it.
    priced = {
        "backend": "claude-cli",
        "model": "sonnet",
        "price": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    }
    _run(_with(inst, models={"default": priced}), store, llm)
    assert llm.calls == 3


def test_the_key_version_follows_the_system_preamble(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from horae.llm.backends import _shared
    from horae.runner import _cache as cache_module

    # Changing Horae's own framing must bump LLM_KEY_VERSION (and this pin).
    assert hashlib.sha256(_shared.SYSTEM_PREAMBLE.encode()).hexdigest() == PREAMBLE_SHA256
    assert _shared.LLM_KEY_VERSION == 1
    cache = _cache(inst, store)
    before = cache.stored_key("a" * 64, "default")
    monkeypatch.setattr(cache_module, "LLM_KEY_VERSION", 2)
    assert cache.stored_key("a" * 64, "default") != before


def test_degraded_or_schema_invalid_outputs_are_not_cached(inst: Instance, store: Store) -> None:
    record = _run(inst, store, CountingLlm())
    assert accepted_entry(record) is not None
    assert accepted_entry(record.model_copy(update={"status": "noop"})) is not None
    for status in ("degraded", "failed", "undone"):
        assert accepted_entry(record.model_copy(update={"status": status})) is None
    invalid = record.llm[0].model_copy(update={"schema_valid": False})
    assert accepted_entry(record.model_copy(update={"llm": [invalid]})) is None


WRITER = """
import sys
from datetime import UTC, datetime
from horae.contracts import Usage
from horae.store import Store
with Store.open(sys.argv[1]) as s:
    for n in range(30):
        s.cache_put_safe(
            sys.argv[2], skill="pack/brief", skill_version="1", model="m",
            response={"writer": sys.argv[3], "n": n},
            usage=Usage(tokens_in=0, tokens_out=0, usd=0.0), at=datetime.now(UTC),
        )
"""


def test_two_processes_writing_the_same_key(inst: Instance, store: Store) -> None:
    key = "f" * 64
    children = [
        subprocess.Popen(  # noqa: S603 (this interpreter, a synthetic writer)
            [sys.executable, "-c", WRITER, str(inst.store_path), key, str(n)],
            stderr=subprocess.PIPE,
        )
        for n in range(2)
    ]
    for child in children:
        _, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
    assert store.cache_count() == 1
    entry = store.cache_get(key, now=datetime.now(UTC))
    assert entry is not None
    assert entry.response["writer"] in {"0", "1"}
    assert entry.response["n"] == 29


# --- cross-pipeline reuse: routing is re-checked --------------------------------------------

NOTE_A, NOTE_B = "Notes/a.assistant.md", "Notes/b.assistant.md"
INBOX = "Inbox.assistant.md"


def _pipe(name: str, path: str) -> Any:
    from horae.contracts import Pipeline

    llm = {"skill": "pack/brief", "model": "default", "budget": {"usd": 0.05, "tokens_out": 99}}
    return Pipeline.model_validate(
        {
            "name": name,
            "steps": [
                {"collect": ["fixture.items"]},
                {"llm": llm},
                {"act": [{"vault.write_note": {"path": path, "mode": "append-section"}}]},
            ],
        }
    )


def _routed_instance(root: Path) -> Instance:
    from horae.contracts import Lockfile

    names = {"to-a": NOTE_A, "also-a": NOTE_A, "to-b": NOTE_B, "inbox": INBOX}
    return Instance(
        root=root,
        config=InstanceConfig.model_validate(
            {"models": {"default": {"backend": "claude-cli", "model": "sonnet"}}}
        ),
        pipelines={name: _pipe(name, path) for name, path in names.items()},
        lock=Lockfile(schema_version=1),
    )


def _routed_plugins() -> Plugins:
    plugins = Plugins()
    for name, provides in (
        ("pack", {"skills": ["pack/brief"]}),
        ("vault", {"sinks": ["vault.write_note"]}),
    ):
        plugins.add(
            {
                "name": name,
                "version": "0.1.0",
                "horae": ">=0.1,<0.2",
                "provides": provides,
                "permissions": {"intents": ["note.append_section"]},
            }
        )
    return plugins


def _append(path: str) -> dict[str, Any]:
    return {"type": "note.append_section", "path": path, "heading": "Brief", "content": "2 items"}


def test_cross_pipeline_reuse_rechecks_the_bindings(tmp_path: Path, store: Store) -> None:
    routed = _routed_instance(tmp_path)
    plugins = _routed_plugins()
    llm = CountingLlm(intents=[_append(NOTE_A)])

    def run(name: str) -> RunRecord:
        runner = Runner(routed, store, Ports(plugins, plugins, llm), clock=Clock())
        return asyncio.run(runner.run(name))

    first = run("to-a")
    assert (first.status, llm.calls) == ("ok", 1)
    # Same skill, model and viewed input in another pipeline bound to the same note: reused.
    same = run("also-a")
    assert (same.status, llm.calls) == ("ok", 1)
    assert same.llm[0].cached is True
    assert same.input_hash == first.input_hash
    # Bound to another note: the reused explicit target no longer routes, so it is a miss
    # (the model is called; its answer names the same note, so this run fails to route).
    other = run("to-b")
    assert llm.calls == 2
    assert other.status == "failed"
    assert not any(call.cached for call in other.llm)


# --- undo invalidates (decision 45 B) --------------------------------------------------------


def test_undo_invalidates_the_entry_of_the_run(tmp_path: Path, store: Store) -> None:
    from horae.apply import StaticSinks
    from horae.apply.testing import FixtureSink
    from horae.apply.undo import undo_run

    inbox = _routed_instance(tmp_path)
    plugins = _routed_plugins()
    sink = FixtureSink(files={INBOX: b"# Inbox\n"})
    llm = CountingLlm(intents=[_append(INBOX)])
    clock = Clock()

    def run() -> RunRecord:
        sinks = StaticSinks(default=sink)
        runner = Runner(inbox, store, Ports(plugins, plugins, llm, sinks), clock=clock)
        return asyncio.run(runner.run("inbox"))

    def undo(record: RunRecord) -> None:
        result = asyncio.run(undo_run(store, record.id, StaticSinks(default=sink), clock=clock))
        assert result.marked_undone, result

    first = run()
    assert (first.status, llm.calls) == ("ok", 1)
    assert _entry(inbox, store, first.input_hash, clock.at) is not None
    undo(first)
    assert _entry(inbox, store, first.input_hash, clock.at) is None
    # Run -> undo -> run again asks the model again.
    second = run()
    assert (second.llm[0].cached, llm.calls) == (False, 2)
    # Undoing a run that reused the entry also invalidates it.
    hit = run()
    assert (hit.llm[0].cached, llm.calls, hit.status) == (True, 2, "ok")
    undo(hit)
    assert _entry(inbox, store, first.input_hash, clock.at) is None
    assert run().llm[0].cached is False
    assert llm.calls == 3


def test_store_cache_delete_helpers(store: Store) -> None:
    from horae.contracts import Usage

    usage = Usage(tokens_in=0, tokens_out=0, usd=0.0)
    responses: list[Any] = [
        {"input_hash": "a" * 64},
        {"input_hash": "a" * 64, "other": 1},
        {"input_hash": "b" * 64},
        ["a" * 64],
        "a" * 64,
    ]
    for n, response in enumerate(responses):
        key = f"{n + 1:064x}"
        store.cache_put(
            key, skill="s", skill_version="1", model="m", response=response, usage=usage, at=T0
        )
    assert store.cache_count() == 5
    assert store.cache_delete_input("a" * 64) == 2
    assert store.cache_count() == 3
    assert store.cache_delete(f"{3:064x}") is True
    assert store.cache_delete(f"{3:064x}") is False
    assert store.cache_count() == 2
    with pytest.raises(ValueError, match="sha256"):
        store.cache_delete("nope")


def test_the_created_at_index_exists(inst: Instance, store: Store) -> None:
    import sqlite3

    with sqlite3.connect(inst.store_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    assert ("llm_cache_created",) in rows


# --- re-validation on read (decision 45 F) ---------------------------------------------------


def _skill_run(cache: StoreLlmCache, llm: Any) -> RunRecord:
    from horae.engine import execute
    from horae.skills import DirectorySkills
    from test_engine_skills import CONTEXT, FIXTURE, START, _pipeline, _plugins

    plugins = _plugins()
    return asyncio.run(
        execute(
            _pipeline(),
            run_id="20261027T053000Z-brief-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=DirectorySkills({"pack-daily-brief": FIXTURE}),
            cache=cache,
            models={"default": "sonnet"},
        )
    )


@pytest.mark.parametrize(
    "tamper",
    [
        {"output": {"summary": 5, "ranked": []}},  # the output no longer fits
        {"intents": [{"type": "note.create", "content": "x", "extra": 1}]},  # nor its intents
        {"output": "not an object"},
    ],
)
def test_a_cached_output_is_validated_again_on_read(
    inst: Instance, store: Store, tamper: dict[str, Any]
) -> None:
    from test_engine_skills import NOTE, OUTPUT

    cache = _cache(inst, store)
    llm = CountingLlm(output=OUTPUT, intents=[NOTE])
    first = _skill_run(cache, llm)
    assert (first.status, llm.calls) == ("ok", 1)
    assert cache.remember(first)
    stored = cache.stored_key(first.input_hash or "", "default")
    hit = _skill_run(cache, llm)
    assert (hit.llm[0].cached, llm.calls) == (True, 1)  # valid: reused
    entry = store.cache_get(stored, now=cache.clock())
    assert entry is not None
    store.cache_put(
        stored,
        skill=entry.skill,
        skill_version=entry.skill_version,
        model=entry.model,
        response={**entry.response, **tamper},
        usage=entry.usage,
        at=entry.created_at,
        expires_at=entry.expires_at,
    )
    again = _skill_run(cache, llm)
    assert llm.calls == 2  # invalid: a miss, the model is called
    assert again.llm[0].cached is False
    assert store.cache_get(stored, now=cache.clock()) is None  # and the entry is deleted


def test_the_cache_validator_may_be_async_and_fails_closed(inst: Instance, store: Store) -> None:
    from test_engine_skills import NOTE, OUTPUT

    seen: list[Any] = []

    async def strict(output: Any, schema: Any, *, timeout_s: float) -> list[str]:
        seen.append(output)
        return ["/: synthetic"]

    def broken(output: Any, schema: Any, *, timeout_s: float) -> list[str]:
        raise RuntimeError("synthetic")

    llm = CountingLlm(output=OUTPUT, intents=[NOTE])
    first = _skill_run(_cache(inst, store), llm)
    assert _cache(inst, store).remember(first)
    for validate in (strict, broken):
        cache = _cache(inst, store, validate=validate)
        assert _cache(inst, store).remember(first)
        assert _skill_run(cache, llm).llm[0].cached is False
    # The full output: the reserved `intents` put back.
    assert seen == [{**OUTPUT, "intents": [NOTE]}]


def test_the_cache_uses_the_backend_paths_validator(inst: Instance, store: Store) -> None:
    from horae.llm.validate import validate_output
    from horae.runner import _cache as cache_module

    # #31's killable worker, never a synchronous validation on the event loop.
    assert cache_module.default_validator() is validate_output
    assert _cache(inst, store).validate is validate_output
    assert not hasattr(cache_module, "check_output")


def _cached_skill_entry(inst: Instance, store: Store) -> tuple[StoreLlmCache, str, Any]:
    from test_engine_skills import NOTE, OUTPUT

    cache = _cache(inst, store)
    llm = CountingLlm(output=OUTPUT, intents=[NOTE])
    first = _skill_run(cache, llm)
    assert cache.remember(first)
    return cache, cache.stored_key(first.input_hash or "", "default"), llm


def test_a_cached_entry_the_validator_refuses_is_a_miss_and_deleted(
    inst: Instance, store: Store
) -> None:
    # An entry over a constraint only the full schema has (`maxLength` of `summary`, which
    # structured outputs drop): #31's validator refuses it, so it is never reused.
    cache, stored, llm = _cached_skill_entry(inst, store)
    entry = store.cache_get(stored, now=cache.clock())
    assert entry is not None
    long = {**entry.response["output"], "summary": "x" * 501}
    store.cache_put(
        stored,
        skill=entry.skill,
        skill_version=entry.skill_version,
        model=entry.model,
        response={**entry.response, "output": long},
        usage=entry.usage,
        at=entry.created_at,
        expires_at=entry.expires_at,
    )
    again = _skill_run(cache, llm)
    assert (again.status, again.llm[0].cached, llm.calls) == ("ok", False, 2)
    assert store.cache_get(stored, now=cache.clock()) is None


def test_a_cache_read_the_validator_cannot_check_is_a_miss_not_a_degraded_step(
    inst: Instance, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.llm import validate as validate_module

    cache, stored, llm = _cached_skill_entry(inst, store)
    worker = tmp_path / "worker.py"
    worker.write_text("import sys\nsys.stdin.read()\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(validate_module, "WORKER", worker)
    again = _skill_run(cache, llm)  # the fake backend validates nothing itself
    assert again.status == "ok", again.steps
    assert (again.llm[0].cached, llm.calls) == (False, 2)
    assert store.cache_get(stored, now=cache.clock()) is None


def test_full_output_puts_the_intents_back() -> None:
    from horae.runner._cache import full_output

    declared = {"properties": {"intents": {"type": "array"}}}
    assert full_output({"a": 1}, [], {}) == {"a": 1}
    assert full_output({"a": 1}, [], declared) == {"a": 1, "intents": []}
    assert full_output({"a": 1}, [{"t": 1}], {}) == {"a": 1, "intents": [{"t": 1}]}
    assert full_output(3, [{"t": 1}], declared) == {"intents": [{"t": 1}], "output": 3}


# --- horae cache purge (decision 45 E) -------------------------------------------------------


def test_horae_cache_purge(tmp_path: Path) -> None:
    from horae.contracts import Usage

    root = write_instance(tmp_path / "instance")
    runner = CliRunner()

    def purge(*args: str) -> Any:
        return runner.invoke(app, ["cache", "purge", "-i", str(root), *args])

    # No store: nothing purged, and none created.
    done = purge("--all", "--json")
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["deleted"] == 0
    loaded = load_instance(root)
    assert not loaded.store_path.exists()
    now = datetime.now(UTC)
    with Store.open(loaded.store_path) as store:
        for n, (skill, age) in enumerate([("a/x", 1), ("a/x", 10), ("b/y", 10), ("b/y", 1)]):
            store.cache_put(
                f"{n + 1:064x}",
                skill=skill,
                skill_version="1",
                model="m",
                response={},
                usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
                at=now - timedelta(days=age),
            )
    for bad in ((), ("--all", "--older-than", "3"), ("--older-than", "-1")):
        refused = purge(*bad, "--json")
        assert refused.exit_code == 2, refused.output
        assert json.loads(refused.stdout)["error"]["kind"] == "usage"
    done = purge("--older-than", "5", "--skill", "a/x", "--json")
    assert done.exit_code == 0, done.output
    report = json.loads(done.stdout)
    assert (report["deleted"], report["remaining"], report["skill"]) == (1, 3, "a/x")
    assert report["older_than_days"] == 5
    done = purge("--older-than", "5")
    assert (done.exit_code, done.stdout) == (0, "deleted 1 cache entry; 2 left\n")
    done = purge("--skill", "b/y")
    assert (done.exit_code, done.stdout) == (0, "deleted 1 cache entry of skill b/y; 1 left\n")
    done = purge("--all")
    assert (done.exit_code, done.stdout) == (0, "deleted 1 cache entry; 0 left\n")


def test_horae_cache_purge_problems(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "instance"
    root.mkdir()
    (root / "horae.yaml").write_text("cache: {ttl_s: 1}\n", encoding="utf-8")
    done = runner.invoke(app, ["cache", "purge", "-i", str(root), "--all", "--json"])
    assert done.exit_code == 1
    assert json.loads(done.stdout)["error"]["kind"] == "config_invalid"
    good = write_instance(tmp_path / "good")
    path = load_instance(good).store_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database")
    done = runner.invoke(app, ["cache", "purge", "-i", str(good), "--all", "--json"])
    assert done.exit_code == 1
    assert json.loads(done.stdout)["error"]["kind"] == "store_error"
