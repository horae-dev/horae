# SPDX-License-Identifier: Apache-2.0
"""Secret redaction in logs and run records (#53, schemas decision 43).

Acceptance criterion: **seeded secrets never appear in logs or the store**. Every value is
synthetic. The secrets are resolved by a `SecretResolver` (which adds them to the process
registry, `horae.secrets.redact`), then handed to producers that do **not** redact (the
engine's fixture ports, a fake LLM provider, direct store calls), so what is checked is the
last line of defence: the store's secret gate, the plan-time refusal, the LLM layer, the log
record factory and the CLI output. The end-to-end test at the bottom runs the walking skeleton
with a fixture source that echoes the secrets in every encoding, through the real plugin host.
"""

import asyncio
import base64
import datetime
import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from typer.testing import CliRunner

from apply_fixtures import plugins as brief_plugins
from apply_fixtures import run_pipeline
from horae.cli import app
from horae.cli_output import echo, to_json
from horae.config import load_instance
from horae.contracts import Pipeline, StepRecord, Usage
from horae.contracts.instance import ModelAlias
from horae.contracts.run import StepError
from horae.engine import LlmError, LlmRequest, ToolResult, execute, make_context
from horae.engine.testing import FixturePlugins
from horae.llm import Completion, CompletionResult, ModelBackends, Prompt
from horae.plugins.host import logs as host_logs
from horae.plugins.host import redact as host_redact
from horae.runner import Ports, Runner
from horae.secrets import SecretResolver, logs, redact
from horae.secrets.redact import encodings, token
from horae.store import SecretRefused, Store, TaskEntry
from runner_fixtures import Plugins, write_instance

LONG = "Sentinel-Long-Secret-0123456789"  # synthetic
SHORT = "q#7Zp!"  # synthetic, under 8 bytes: matched raw and bounded only
EXEMPT = "Exempt-Note-Secret-9876543210"  # synthetic: only in the user's own note bytes
NAMED = "sentinel-named-secret"  # synthetic, valid as a plugin name
AT = datetime.datetime(2026, 10, 28, 6, 0, tzinfo=datetime.UTC)


def _resolver(tmp_path: Path, values: Mapping[str, str]) -> SecretResolver:
    folder = tmp_path / "secret-files"
    folder.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (folder / name).write_text(value, encoding="utf-8")
    resolver = SecretResolver({}, folder)
    for name in values:
        resolver.resolve(name)
    return resolver


@pytest.fixture
def seeded(tmp_path: Path) -> SecretResolver:
    values = {"LONG_TOKEN": LONG, "SHORT_PIN": SHORT, "NOTE_KEY": EXEMPT, "NAMED": NAMED}
    return _resolver(tmp_path, values)


def _forms(*values: str) -> list[bytes]:
    return sorted({form.encode() for value in values for form in encodings(value)})


def _state_bytes(path: Path) -> bytes:
    """Every byte of every file under `path` (the database, its `-wal` and `-shm`, locks)."""
    return b"".join(f.read_bytes() for f in sorted(path.rglob("*")) if f.is_file())


def _leaks(data: bytes, *values: str) -> list[bytes]:
    return [form for form in _forms(*values) if form in data]


# --- the shared redactor and the process registry -------------------------------------------


def test_the_host_modules_reexport_the_shared_redactor_and_scrubber() -> None:
    assert host_redact.Redactor is redact.Redactor
    assert host_redact.SecretInKey is redact.SecretInKey
    assert host_redact.token is redact.token
    assert host_logs.install is logs.install
    assert host_logs.scrub is logs.scrub


def test_every_resolver_adds_to_the_process_registry(tmp_path: Path) -> None:
    assert redact.known() == ()
    assert not redact.current()
    first = _resolver(tmp_path / "a", {"LONG_TOKEN": LONG})
    assert [s.name for s in redact.known()] == ["LONG_TOKEN"]
    before = redact.current()
    second = _resolver(tmp_path / "b", {"SHORT_PIN": SHORT})
    second.resolve("SHORT_PIN")  # twice: kept once
    assert [s.name for s in redact.known()] == ["LONG_TOKEN", "SHORT_PIN"]
    assert redact.current() is not before  # rebuilt when the set grew
    assert redact.current() is redact.current()  # cached otherwise
    assert first.resolved() != second.resolved()  # each resolver still has its own
    assert (
        redact.redact(f"a {LONG} b {SHORT}.") == "a [REDACTED:LONG_TOKEN] b [REDACTED:SHORT_PIN]."
    )
    redact.forget_all()
    assert redact.known() == ()


def test_prime_resolves_what_it_can_and_skips_the_rest(tmp_path: Path) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir()
    (folder / "LONG_TOKEN").write_text(LONG, encoding="utf-8")
    redact.prime(SecretResolver({}, folder), ["LONG_TOKEN", "MISSING_ONE", "LONG_TOKEN"])
    assert [s.name for s in redact.known()] == ["LONG_TOKEN"]


def test_scrub_data_rewrites_keys_and_keeps_numbers(seeded: SecretResolver) -> None:
    value = {LONG: [1, f"x{LONG}x", {"n": 1234567, "s": SHORT}], "ok": True, "none": None}
    scrubbed, names = redact.current().scrub_data(value)
    assert names == {"LONG_TOKEN", "SHORT_PIN"}
    assert scrubbed == {
        token("LONG_TOKEN"): [
            1,
            f"x{token('LONG_TOKEN')}x",
            {"n": 1234567, "s": token("SHORT_PIN")},
        ],
        "ok": True,
        "none": None,
    }
    with pytest.raises(redact.SecretInKey):  # the host's variant still refuses keys
        redact.current().redact_data(value)


# --- logs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", [*logs.LOGGERS, "horae.store", "horae.runner", "mcp.client.stdio", "httpx2._client"]
)
def test_every_listed_logger_is_scrubbed_with_no_host_open(
    seeded: SecretResolver, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    logs.install()
    logger = logging.getLogger(name)
    caplog.set_level(logging.DEBUG, logger=name)
    try:
        raise ValueError(f"boom {LONG}")
    except ValueError:
        logger.error("got %s and %r", LONG, {"pin": SHORT}, exc_info=True, stack_info=True)
    (record,) = [r for r in caplog.records if r.name == name]
    assert record.getMessage() == (
        "got [REDACTED:LONG_TOKEN] and {'pin': '[REDACTED:SHORT_PIN]'} "
        "(ValueError, details withheld)"
    )
    assert (record.args, record.exc_info, record.stack_info) == (None, None, None)
    assert not _leaks(caplog.text.encode(), LONG, SHORT)


def test_other_loggers_are_untouched(
    seeded: SecretResolver, caplog: pytest.LogCaptureFixture
) -> None:
    logs.install()
    caplog.set_level(logging.INFO, logger="someone.else")
    logging.getLogger("someone.else").info("keep %s", "this")
    (record,) = [r for r in caplog.records if r.name == "someone.else"]
    assert record.args == ("this",)


def test_install_pins_the_http_loggers_at_warning() -> None:
    for name in logs.HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    child = logging.getLogger("httpcore2.connection")
    child.setLevel(logging.DEBUG)
    logs.install()
    for name in logs.HTTP_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    assert child.level == logging.WARNING
    assert logs.covered("anthropic._base_client")
    assert not logs.covered("horaex")


def test_the_cli_installs_the_scrubber(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(logs, "install", lambda: calls.append("install"))
    CliRunner().invoke(app, ["runs", "-i", str(tmp_path), "--json"])
    assert calls == ["install"]


# --- the store's secret gate, every table ------------------------------------------------------


def _forgetful_record() -> Any:
    """A run of `apply_fixtures` whose producers (fixture ports) echo the secrets everywhere
    and redact nothing (a producer that forgot)."""
    intents = [
        {
            "type": "note.replace_owned_block",
            "path": "Daily/2026-10-27.assistant.md",
            "block": "horae-brief",
            "content": f"- [ ] token {LONG} pin {SHORT}\n",
        },
        {"type": "task.create", "title": f"Rotate {SHORT} now", "fingerprint": "fp-new"},
        {
            "type": "notify.push",
            "title": "Brief",
            "body": f"key={base64.b64encode(LONG.encode()).decode()}",
        },
    ]
    fixture = brief_plugins(intents)
    fixture.handlers["items.today"] = lambda p, i: [{"title": LONG, LONG: SHORT, "n": 3}]
    return asyncio.run(run_pipeline(dry_run=False, fixture=fixture))


def test_every_table_is_redacted_and_flagged(
    seeded: SecretResolver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logs.install()
    record = _forgetful_record()
    state = tmp_path / "state"
    note = f"Daily/{LONG}.md"
    with Store.open(state / "horae.db") as store:
        store.begin_run(record.id, record.pipeline, record.started_at, host=f"box-{LONG}")
        leaky = StepRecord(
            kind="collect",
            status="degraded",
            error=StepError(kind="tool_error", message=f"src.read: bad {LONG}"),
            stderr=f"debug: pin={SHORT}\n",
        )
        store.record_step(record.id, 0, leaky)  # while running: flagged for the record
        (row,) = store.steps(record.id)
        assert row.error is not None
        assert row.error.message == "src.read: bad [REDACTED:LONG_TOKEN]"
        assert row.stderr == "debug: pin=[REDACTED:SHORT_PIN]\n"
        entry = store.journal_append(
            record.id,
            intent=0,
            intent_type="note.replace_owned_block",
            target=note,
            before=f"my own note {EXEMPT}\n".encode(),  # exempt: restored byte for byte
            after=f"horae wrote {EXEMPT}\n".encode(),  # exempt, flagged
            applied_by=f"vault.write_note {SHORT}",
            sink_identity=f"mcp:vault:{LONG}",
            at=AT,
        )
        store.journal_mark_applied(entry.id, actual_after=f"sink wrote {EXEMPT}".encode())
        store.upsert_task(
            TaskEntry(
                fingerprint="fp-1",
                title=f"Pay {SHORT} now",
                status="todo",
                target=f"T/{LONG}",
                run_id=record.id,
            ),
            at=AT,
        )
        store.record_notification(f"dedupe-{LONG}", at=AT, run_id=record.id)
        usage = store.reserve_usage(
            record.id,
            step=1,
            attempt=1,
            alias=f"a-{SHORT}",
            at=AT,
            day=AT.date(),
            usd=0.1,
            tokens_out=10,
            backend="anthropic",
            model=f"m-{LONG}",
        )
        store.settle_usage(
            usage, Usage(tokens_in=1, tokens_out=1, usd=0.0), at=AT, model=f"m2-{LONG}"
        )
        store.save_run(record)

        store.begin_run("crashed-run", "other", AT)
        store.abandon_run("crashed-run", at=AT, error=f"crashed with {LONG}")

        with pytest.raises(SecretRefused, match="LONG_TOKEN"):
            store.cache_put(
                "a" * 64,
                skill="pack/brief",
                skill_version="1",
                model="m",
                response={"x": LONG},
                usage=Usage(tokens_in=0, tokens_out=0, usd=0.0),
                at=AT,
            )
        assert store.cache_get("a" * 64, now=AT) is None
        with pytest.raises(SecretRefused, match="a task fingerprint holds secret LONG_TOKEN"):
            store.upsert_task(TaskEntry(fingerprint=f"x-{LONG}", title="t", status="todo"), at=AT)
        _refuse_source(store)

        saved = store.get_run(record.id)
        assert saved is not None
        details = [v.detail for v in saved.violations if v.kind == "secret_in_output"]
        assert (
            "the store redacted secret LONG_TOKEN, SHORT_PIN in the run record "
            "(a producer did not redact it)" in details
        )
        assert (
            "the store redacted secret LONG_TOKEN, SHORT_PIN in step 0 "
            "(a producer did not redact it)" in details
        )
        assert any("found secret NOTE_KEY in the content Horae wrote to" in d for d in details)
        assert any("found secret NOTE_KEY in the content a sink wrote to" in d for d in details)
        assert any("secret LONG_TOKEN in a journal target" in d for d in details)
        assert any("a notification key" in d for d in details)
        assert any("a task title" in d for d in details)
        # what was redacted reads back redacted, what is exempt reads back byte for byte
        (journalled,) = store.journal(record.id)
        assert journalled.target == "Daily/[REDACTED:LONG_TOKEN].md"
        assert journalled.sink_identity == "mcp:vault:[REDACTED:LONG_TOKEN]"
        assert journalled.before_sha256 is not None
        assert journalled.after_sha256 is not None
        assert store.blob(journalled.before_sha256) == f"my own note {EXEMPT}\n".encode()
        assert store.blob(journalled.after_sha256) == f"horae wrote {EXEMPT}\n".encode()
        task = store.get_task("fp-1")
        assert task is not None
        assert task.title == "Pay [REDACTED:SHORT_PIN] now"
        assert store.get_notification("dedupe-[REDACTED:LONG_TOKEN]") is not None
        (group,) = store.usage_report()
        assert (group.alias, group.model) == ("a-[REDACTED:SHORT_PIN]", "m2-[REDACTED:LONG_TOKEN]")
        crashed = store.run_summary("crashed-run")
        assert crashed is not None
        assert crashed.error == "crashed with [REDACTED:LONG_TOKEN]"
        assert store.run_summary(record.id).host == "box-[REDACTED:LONG_TOKEN]"  # type: ignore[union-attr]
        assert saved.inputs[0].data[0]["title"] == token("LONG_TOKEN")
        assert saved.inputs[0].data[0][token("LONG_TOKEN")] == token("SHORT_PIN")

    data = _state_bytes(state)
    assert data, "the state directory holds the database"
    assert _leaks(data, LONG, SHORT) == []
    assert EXEMPT.encode() in data  # the documented exemption: the note bytes are kept
    assert _leaks(caplog.text.encode(), LONG, SHORT, EXEMPT) == []
    assert "secret NOTE_KEY found in the content Horae wrote" in caplog.text


def _refuse_source(store: Store) -> None:
    with pytest.raises(SecretRefused, match="a task source"):
        store.upsert_task(
            TaskEntry(fingerprint="fp-2", title="t", status="todo", source=f"{NAMED}.x"),
            at=AT,
        )


@pytest.mark.parametrize("form", sorted(encodings(LONG)))
def test_every_encoding_is_redacted_in_the_store(tmp_path: Path, form: str) -> None:
    _resolver(tmp_path, {"LONG_TOKEN": LONG})
    with Store.open(tmp_path / "state" / "horae.db") as store:
        store.record_notification(f"<{form}>", at=AT)
        assert store.get_notification("<[REDACTED:LONG_TOKEN]>") is not None
    assert _leaks(_state_bytes(tmp_path / "state"), LONG) == []


def test_a_record_that_cannot_stay_valid_once_redacted_is_refused(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    record = asyncio.run(run_pipeline(dry_run=False))
    plugins = dict(record.model_dump(mode="json")["plugins"])
    plugins[NAMED] = {"version": "1.0.0"}  # a plugin name: rewriting makes it invalid
    bad = record.model_validate({**record.model_dump(mode="json"), "plugins": plugins})
    with Store.open(tmp_path / "horae.db") as store:
        with pytest.raises(SecretRefused, match=f"run record {record.id!r} holds secret"):
            store.save_run(bad)
        assert store.get_run(record.id) is None


def test_without_a_known_secret_the_record_is_stored_as_given(tmp_path: Path) -> None:
    record = asyncio.run(run_pipeline(dry_run=False))
    with Store.open(tmp_path / "horae.db") as store:
        store.save_run(record)
        assert store.get_run(record.id) == record
    conn = sqlite3.connect(tmp_path / "horae.db")
    (stored,) = conn.execute("SELECT record FROM runs").fetchone()
    conn.close()
    assert stored == record.model_dump_json()


def test_journal_before_bytes_restore_byte_exact(seeded: SecretResolver, tmp_path: Path) -> None:
    before = f"# Note\r\n{LONG} {SHORT}\r\n".encode()
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run("r1", "p", AT)
        entry = store.journal_append(
            "r1", intent=0, intent_type="note.create", target="N.md", before=before, at=AT
        )
        store.journal_mark_undone(entry.id, at=AT, overwrote=before + b"edited")
        assert entry.before_sha256 is not None
        assert store.blob(entry.before_sha256) == before
        (marked,) = store.journal("r1")
        assert marked.undo_overwrote_sha256 is not None
        assert store.blob(marked.undo_overwrote_sha256) == before + b"edited"


# --- the runner: priming, a forgetful producer, plan-time refusal ------------------------------


def _secret_instance(root: Path, **config: Any) -> Path:
    write_instance(
        root, secrets={"dir": "secrets", "refs": {"LONG_TOKEN": {"file": "long"}}}, **config
    )
    (root / "secrets").mkdir()
    (root / "secrets" / "long").write_text(LONG + "\n", encoding="utf-8")
    return root


class _Echo(Plugins):
    """The runner's fixture plugins, `fixture.items` echoing the secret (never redacted)."""

    def __init__(self) -> None:
        super().__init__()
        self.handlers["fixture.items"] = lambda p, i: ToolResult(
            data=[{"title": f"call {LONG}", "b64": base64.b64encode(LONG.encode()).decode()}]
        )


def test_the_runner_primes_the_registry_and_the_store_catches_what_a_producer_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    instance = load_instance(_secret_instance(tmp_path / "inst"))
    plugins = _Echo()
    assert redact.known() == ()
    with Store.open(instance.store_path) as store:
        record = asyncio.run(Runner(instance, store, Ports(plugins, plugins)).run("collect"))
        saved = store.get_run(record.id)
    assert [s.name for s in redact.known()] == ["LONG_TOKEN"]  # primed by the runner
    assert saved is not None
    assert saved.status == "ok"
    assert [v.detail for v in saved.violations] == [
        "the store redacted secret LONG_TOKEN in the run record (a producer did not redact it)",
    ]
    assert _leaks(_state_bytes(instance.state_dir), LONG) == []
    assert _leaks(caplog.text.encode(), LONG) == []


def test_a_var_holding_a_secret_refuses_the_run_at_plan_time(tmp_path: Path) -> None:
    instance = load_instance(_secret_instance(tmp_path / "inst"))
    plugins = Plugins()
    with Store.open(instance.store_path) as store:
        runner = Runner(instance, store, Ports(plugins, plugins))
        record = asyncio.run(runner.run("collect", vars={"who": f"me:{LONG}"}))
    assert record.status == "failed"
    assert plugins.calls == []  # nothing called
    error = record.steps[0].error
    assert error is not None
    assert (error.kind, error.message) == (
        "params_invalid",
        "the variable who holds secret LONG_TOKEN (never a parameter)",
    )
    assert record.context.vars["who"] == "me:[REDACTED:LONG_TOKEN]"
    assert record.violations == []  # the engine redacted it: nothing left for the store
    assert _leaks(_state_bytes(instance.state_dir), LONG) == []


@pytest.mark.parametrize(
    "param",
    [LONG, base64.b64encode(LONG.encode()).decode(), LONG.encode().hex(), f"x={SHORT}"],
)
def test_a_parameter_holding_a_secret_refuses_the_run(seeded: SecretResolver, param: str) -> None:
    fixture = FixturePlugins()
    fixture.add(
        {
            "name": "src",
            "version": "0.1.0",
            "horae": ">=0.1,<0.2",
            "provides": {"sources": ["src.read"]},
            "permissions": {},
        },
        {"src.read": lambda p, i: []},
    )
    pipeline = Pipeline.model_validate(
        {"name": "p", "steps": [{"collect": [{"src.read": {"q": param, "n": 2}}]}]}
    )
    record = asyncio.run(
        execute(
            pipeline,
            run_id="r1",
            context=make_context(AT, "UTC"),
            started_at=AT,
            plugins=fixture,
            tools=fixture,
        )
    )
    assert fixture.calls == []
    error = record.steps[0].error
    assert error is not None
    assert error.kind == "params_invalid"
    assert error.message.startswith("src.read: the parameters hold secret ")
    assert LONG not in error.message
    assert SHORT not in error.message


# --- the LLM layer -----------------------------------------------------------------------------


class _Provider:
    """A provider backend that echoes the secret in what it returns (or raises)."""

    name: Literal["ollama"] = "ollama"
    may_write_cache = False

    def __init__(self, fail: LlmError | None = None) -> None:
        self.fail = fail
        self.sent = 0

    def render(self, completion: Completion) -> dict[str, Any]:
        return {
            "model": completion.model,
            "system": completion.system,
            "max_tokens": completion.max_tokens,
        }

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.sent += 1
        if self.fail is not None:
            raise self.fail
        output = {
            "summary": f"key {LONG}",
            LONG: 1,
            "intents": [{"type": "notify.push", "title": SHORT}],
        }
        return CompletionResult(output=output, usage=Usage(tokens_in=1, tokens_out=1, usd=0.0))

    async def aclose(self) -> None:
        return None


class _Prompts:
    def __init__(self, system: str = "Summarise.") -> None:
        self.system = system

    def prompt(self, request: LlmRequest) -> Prompt:
        return Prompt(self.system, request.input, {"type": "object"}, "1")


def _backends(provider: _Provider, prompts: _Prompts, tmp_path: Path) -> ModelBackends:
    spec = ModelAlias.model_validate(
        {"backend": "ollama", "model": "fixture-model", "base_url": "http://localhost:11434"}
    )
    return ModelBackends(
        {"default": spec},
        SecretResolver({}, tmp_path),
        {"ollama": lambda alias, spec, secrets: provider},
        prompts,
        validate=lambda output, schema: [f"bad value {LONG}"],
    )


def _request() -> LlmRequest:
    from horae.contracts.pipeline import Budget

    return LlmRequest("pack/brief", "default", {"x": 1}, Budget(usd=1.0, tokens_out=100), 10.0, 1)


def test_an_llm_response_is_redacted(
    seeded: SecretResolver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = asyncio.run(_backends(_Provider(), _Prompts(), tmp_path).complete(_request()))
    assert response.output == {"summary": "key [REDACTED:LONG_TOKEN]", token("LONG_TOKEN"): 1}
    assert response.intents == [{"type": "notify.push", "title": token("SHORT_PIN")}]
    assert response.schema_errors == ("bad value [REDACTED:LONG_TOKEN]",)
    assert LONG not in json.dumps(response.request)
    assert "secret LONG_TOKEN, SHORT_PIN found in a response of model alias default" in caplog.text
    assert _leaks(caplog.text.encode(), LONG, SHORT) == []


def test_an_llm_error_is_redacted_with_no_context(seeded: SecretResolver, tmp_path: Path) -> None:
    failure = LlmError("auth_failed", f"bad key {LONG}", stderr=f"x-api-key: {LONG}", usage=None)
    provider = _Provider(failure)
    with pytest.raises(LlmError) as raised:
        asyncio.run(_backends(provider, _Prompts(), tmp_path).complete(_request()))
    error = raised.value
    assert (error.kind, error.message, error.stderr) == (
        "auth_failed",
        "bad key [REDACTED:LONG_TOKEN]",
        "x-api-key: [REDACTED:LONG_TOKEN]",
    )
    assert error.__context__ is None
    assert error.__cause__ is None


def test_a_skill_prompt_holding_a_secret_is_refused_before_anything_is_sent(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    provider = _Provider()
    with pytest.raises(LlmError) as raised:
        asyncio.run(_backends(provider, _Prompts(f"Use {LONG}."), tmp_path).complete(_request()))
    assert (raised.value.kind, raised.value.message) == (
        "params_invalid",
        "the prompt of skill pack/brief holds secret LONG_TOKEN (never a parameter)",
    )
    assert provider.sent == 0


def test_the_engine_redacts_an_llm_error_from_any_backend(seeded: SecretResolver) -> None:
    from horae.engine.testing import FakeLlm

    fixture = FixturePlugins()
    fixture.add(
        {
            "name": "pack",
            "version": "0.1.0",
            "horae": ">=0.1,<0.2",
            "provides": {"skills": ["pack/brief"]},
            "permissions": {},
        }
    )
    pipeline = Pipeline.model_validate(
        {
            "name": "p",
            "steps": [
                {
                    "llm": {
                        "skill": "pack/brief",
                        "model": "default",
                        "budget": {"usd": 1.0, "tokens_out": 10},
                    }
                }
            ],
        }
    )
    llm = FakeLlm([LlmError("backend_error", f"leaked {LONG}")])
    record = asyncio.run(
        execute(
            pipeline,
            run_id="r1",
            context=make_context(AT, "UTC"),
            started_at=AT,
            plugins=fixture,
            tools=fixture,
            llm=llm,
        )
    )
    assert record.steps[0].error is not None
    assert record.steps[0].error.message == "'leaked [REDACTED:LONG_TOKEN]'"


# --- CLI output --------------------------------------------------------------------------------


def test_echo_and_to_json_redact(
    seeded: SecretResolver, capsys: pytest.CaptureFixture[str]
) -> None:
    echo(f"text {LONG}")
    echo(f"err {SHORT}", err=True)
    echo(to_json({LONG: [f"v {LONG}", "é" + LONG]}))
    out, err = capsys.readouterr()
    assert _leaks(out.encode() + err.encode(), LONG, SHORT) == []
    assert "text [REDACTED:LONG_TOKEN]" in out
    assert "err [REDACTED:SHORT_PIN]" in err
    assert json.loads(out[out.index("{") :]) == {
        token("LONG_TOKEN"): ["v [REDACTED:LONG_TOKEN]", "é[REDACTED:LONG_TOKEN]"]
    }


@pytest.mark.parametrize("command", ["diff", "replay", "undo"])
def test_a_command_echoing_a_secret_prints_it_redacted(tmp_path: Path, command: str) -> None:
    root = _secret_instance(tmp_path / "inst")
    for json_flag in ([], ["--json"]):
        result = CliRunner().invoke(app, [command, LONG, "-i", str(root), *json_flag])
        assert result.exit_code != 0
        printed = (result.stdout + result.stderr).encode()
        assert _leaks(printed, LONG) == [], printed
        assert b"[REDACTED:LONG_TOKEN]" in printed


# --- end to end: the walking skeleton with a fixture source that echoes the secrets -------------


def _echoing_fixture(path: Path, rng_seed: int) -> None:
    """A `fixture-source` file whose data and proposed note content hold both secrets in
    every encoding the redactor knows, at pseudo-random places (seeded: reproducible)."""
    import random

    rng = random.Random(rng_seed)  # noqa: S311 (reproducible placement, not cryptography)
    forms = [*sorted(encodings(LONG)), SHORT]
    words = ["alpha", "beta", "gamma", "delta"]
    items = []
    for index, form in enumerate(forms):
        left, right = rng.choice(words), rng.choice(words)
        items.append(
            {"id": f"fx-{index}", "title": f"{left} {form} {right}", "note": [form, {"k": form}]}
        )
    lines = "".join(f"- [ ] {item['title']} ({item['id']})\n" for item in items)
    document = {
        "data": {
            "items": items,
            "header": f"Authorization: Basic {base64.b64encode(f'u:{LONG}'.encode()).decode()}",
        },
        "intents": [{"type": "note.replace_owned_block", "block": "fixture", "content": lines}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def _cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, [*args])
    return result.exit_code, result.stdout + result.stderr


@pytest.mark.parametrize("seed", [53, 2026])
def test_the_walking_skeleton_never_stores_logs_or_prints_an_echoed_secret(
    tmp_path: Path, seed: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "instance"
    code, out = _cli("init", str(root), "--json")
    assert code == 0, out
    created = json.loads(out)
    root, vault = Path(created["instance"]), Path(created["vault"])
    feed = tmp_path / "feed"
    feed.mkdir()
    _echoing_fixture(feed / "data.json", seed)
    # The secrets: one by an explicit file reference, one found by its name.
    secrets = root / "secrets"
    secrets.mkdir()
    (secrets / "long-token").write_text(LONG + "\n", encoding="utf-8")
    (secrets / "SHORT_PIN").write_text(SHORT, encoding="utf-8")
    config = yaml.safe_load((root / "horae.yaml").read_text(encoding="utf-8"))
    config["secrets"] = {"dir": "secrets", "refs": {"LONG_TOKEN": {"file": "long-token"}}}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    lock = yaml.safe_load((root / "horae.lock").read_text(encoding="utf-8"))
    lock["plugins"]["fixture-source"]["permissions"]["paths"] = {"read": [str(feed)]}
    lock["plugins"]["fixture-source"]["permissions"]["secrets"] = ["SHORT_PIN"]
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    pipeline_file = root / "pipelines" / "daily-fixture.yaml"
    pipeline = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    pipeline["steps"][0]["collect"] = [{"fixture-source.serve": {"file": str(feed / "data.json")}}]
    pipeline_file.write_text(yaml.safe_dump(pipeline), encoding="utf-8")

    printed: list[str] = []
    code, out = _cli("doctor", "-i", str(root), "--json")
    printed.append(out)
    assert code == 0, out
    code, out = _cli("run", "daily-fixture", "-i", str(root), "--dry-run", "--json")
    printed.append(out)
    assert code == 0, out
    dry = json.loads(out)
    code, out = _cli("run", "daily-fixture", "-i", str(root), "--json")
    printed.append(out)
    assert code == 0, out
    run = json.loads(out)
    assert run["run"]["status"] == "ok"
    assert set(run["run"]["violations"]) == {
        "secret_in_output"
    }  # the host redacted and flagged the echo
    for args in (
        ("runs", "-i", str(root)),
        ("runs", "-i", str(root), "--json"),
        ("diff", dry["run"]["id"], "-i", str(root)),
        ("diff", dry["run"]["id"], "-i", str(root), "--json"),
        ("replay", run["run"]["id"], "-i", str(root), "--json"),
        ("run", "daily-fixture", "-i", str(root)),
        ("usage", "-i", str(root), "--json"),
        ("undo", run["run"]["id"], "-i", str(root), "--json"),
        ("runs", "-i", str(root), "--json"),
    ):
        code, out = _cli(*args)
        printed.append(out)
        assert code in (0, 3, 4), (args, out)

    vault_bytes = _state_bytes(vault)
    state_bytes = _state_bytes(root / "state")
    assert (root / "state" / "horae.db").exists()
    assert _leaks(state_bytes, LONG, SHORT) == []  # the database, its -wal and -shm
    assert b"[REDACTED:LONG_TOKEN]" in state_bytes  # the echoed data was stored, redacted
    assert _leaks(vault_bytes, LONG, SHORT) == []  # what Horae wrote (then undid)
    assert _leaks("".join(printed).encode(), LONG, SHORT) == []
    assert _leaks(caplog.text.encode(), LONG, SHORT) == []
    for record in caplog.records:
        assert _leaks(record.getMessage().encode(), LONG, SHORT) == []
    assert "[REDACTED:LONG_TOKEN]" in "".join(printed)


def test_a_step_that_cannot_stay_valid_once_redacted_is_refused(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    step = StepRecord(kind="collect", status="failed", error=StepError(kind=NAMED, message="x"))
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run("r1", "p", AT)
        with pytest.raises(SecretRefused, match="step 0 of run 'r1' holds secret NAMED"):
            store.record_step("r1", 0, step)
        assert store.steps("r1") == []


def test_a_malformed_log_record_shows_its_format_only(
    seeded: SecretResolver, caplog: pytest.LogCaptureFixture
) -> None:
    logs.install()
    caplog.set_level(logging.INFO, logger="horae.test")
    logging.getLogger("horae.test").info("value %s %s", LONG)  # one argument missing
    (record,) = [r for r in caplog.records if r.name == "horae.test"]
    assert (record.getMessage(), record.args) == ("value %s %s", None)
