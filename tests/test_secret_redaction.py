# SPDX-License-Identifier: Apache-2.0
"""Secret redaction in logs and run records (#53, schemas decision 43).

Acceptance criterion: **seeded secrets never appear in logs or the store**. Every value is
synthetic. The secrets are resolved by a `SecretResolver` (which adds them to the process
registry, `horae.secrets.redact`), then handed to producers that do **not** redact (the
engine's fixture ports, a fake LLM provider, direct store calls), so what is checked is the
last line of defence: the store's secret gate, the plan-time refusal, the LLM layer, the log
record factory and the CLI output. The end-to-end test at the bottom runs the walking skeleton
with a fixture source that echoes the secrets in every encoding, through the real plugin host,
and checks the state with an **independent oracle** (hard-coded encoded forms, and a decoder
of every base64, hex and percent run found in the database bytes), not with the redactor's own
list of forms.
"""

import asyncio
import base64
import binascii
import datetime
import json
import logging
import re
import sqlite3
import sys
import threading
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote_to_bytes

import pytest
import yaml
from typer.testing import CliRunner

from apply_fixtures import plugins as brief_plugins
from apply_fixtures import record as brief_record
from apply_fixtures import run_pipeline
from horae.apply.plan import ChangeSet, HttpItem
from horae.apply.render import render_text, safe
from horae.apply.sink import HttpCall
from horae.cli import app
from horae.cli_output import echo, to_json
from horae.config import check_instance, load_instance
from horae.contracts import Pipeline, StepRecord, Usage
from horae.contracts.instance import EnvSecretRef, ModelAlias
from horae.contracts.run import StepError
from horae.engine import LlmError, LlmRequest, ToolResult, execute, make_context
from horae.engine.testing import FixturePlugins
from horae.llm import Completion, CompletionResult, ModelBackends, Prompt
from horae.plugins.host import logs as host_logs
from horae.plugins.host import redact as host_redact
from horae.runner import Ports, Runner
from horae.secrets import SecretError, SecretResolver, logs, redact
from horae.secrets.redact import encodings, token
from horae.store import SecretRefused, Store, TaskEntry
from runner_fixtures import Plugins, write_instance

LONG = "Sentinel-Long-Secret-0123456789"  # synthetic
PIN = "86420975"  # synthetic numeric secret (8 bytes, the minimum)
SPECIAL = "Pct Secret/with&chars=42"  # synthetic, its percent-encoding differs from it
EXEMPT = "Exempt-Note-Secret-9876543210"  # synthetic: only in the user's own note bytes
NAMED = "sentinel-named-secret"  # synthetic, valid as a plugin name or a fingerprint
AT = datetime.datetime(2026, 10, 28, 6, 0, tzinfo=datetime.UTC)

# The oracle's hard-coded encoded forms (computed once, by hand, not by the redactor).
LONG_B64 = "U2VudGluZWwtTG9uZy1TZWNyZXQtMDEyMzQ1Njc4OQ"
LONG_HEX = "53656e74696e656c2d4c6f6e672d5365637265742d30313233343536373839"
LONG_BASIC = "dTpTZW50aW5lbC1Mb25nLVNlY3JldC0wMTIzNDU2Nzg5"  # base64 of `u:<LONG>`
SPECIAL_PERCENT = "Pct%20Secret%2Fwith%26chars%3D42"
SPECIAL_B64 = "UGN0IFNlY3JldC93aXRoJmNoYXJzPTQy"


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
    values = {"LONG_TOKEN": LONG, "PIN_CODE": PIN, "NOTE_KEY": EXEMPT, "NAMED": NAMED}
    return _resolver(tmp_path, values)


def _forms(*values: str) -> list[bytes]:
    return sorted({form.encode() for value in values for form in encodings(value)})


def _state_bytes(path: Path) -> bytes:
    """Every byte of every file under `path` (the database, its `-wal` and `-shm`, locks)."""
    return b"".join(f.read_bytes() for f in sorted(path.rglob("*")) if f.is_file())


def _leaks(data: bytes, *values: str) -> list[bytes]:
    return [form for form in _forms(*values) if form in data]


def _decoded_runs(data: bytes) -> list[bytes]:
    """Every base64 (both alphabets, every alignment), hex and percent-encoded run of `data`,
    decoded (the oracle: independent of the redactor's list of forms)."""
    out: list[bytes] = []
    for run in re.findall(rb"[A-Za-z0-9+/_=-]{12,}", data):
        for start in range(4):
            part = run[start:].rstrip(b"=")
            if len(part) % 4 == 1:
                part = part[:-1]
            part += b"=" * (-len(part) % 4)
            for altchars in (b"+/", b"-_"):
                try:
                    out.append(base64.b64decode(part, altchars=altchars))
                except (binascii.Error, ValueError):
                    continue
    for run in re.findall(rb"[0-9A-Fa-f]{16,}", data):
        for start in (0, 1):
            part = run[start:]
            out.append(binascii.unhexlify(part[: len(part) - len(part) % 2]))
    out.extend(
        unquote_to_bytes(run)
        for run in re.findall(rb"[A-Za-z0-9._~%&=-]*%[0-9A-Fa-f]{2}[A-Za-z0-9._~%&=-]*", data)
    )
    return out


def _oracle(data: bytes, *values: str) -> list[str]:
    """What the independent oracle finds of `values` in `data`."""
    found = [value for value in values if value.encode() in data]
    hard = (LONG_B64, LONG_HEX, LONG_HEX.upper(), LONG_BASIC, SPECIAL_PERCENT, SPECIAL_B64)
    found += [form for form in hard if form.encode() in data]
    for decoded in _decoded_runs(data):
        found += [f"decoded {value[:4]}…" for value in values if value.encode() in decoded]
    return sorted(set(found))


def test_the_oracle_finds_what_it_must() -> None:
    blob = b"x" + LONG_BASIC.encode() + b" " + SPECIAL_PERCENT.encode() + b" " + LONG_HEX.encode()
    assert _oracle(blob, LONG, SPECIAL)
    shifted = base64.b64encode(b"ab:" + LONG.encode() + b"!").decode()
    assert _oracle(f"[{shifted}]".encode(), LONG) == [f"decoded {LONG[:4]}…"]
    assert _oracle(b"nothing to see here 0123456789abcdef", LONG, SPECIAL) == []


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
    second = _resolver(tmp_path / "b", {"PIN_CODE": PIN})
    second.resolve("PIN_CODE")  # twice: kept once
    assert [s.name for s in redact.known()] == ["LONG_TOKEN", "PIN_CODE"]
    assert redact.current() is not before  # rebuilt when the set grew
    assert redact.current() is redact.current()  # cached otherwise
    assert first.resolved() != second.resolved()  # each resolver still has its own
    assert redact.redact(f"a {LONG} b {PIN}.") == "a [REDACTED:LONG_TOKEN] b [REDACTED:PIN_CODE]."
    redact.forget_all()
    assert redact.known() == ()


def test_prime_resolves_what_it_can_and_skips_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir()
    (folder / "LONG_TOKEN").write_text(LONG, encoding="utf-8")
    (folder / "TINY").write_text("q#7Zp!", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="horae.secrets")
    redact.prime(SecretResolver({}, folder), ["LONG_TOKEN", "MISSING_ONE", "TINY", "LONG_TOKEN"])
    assert [s.name for s in redact.known()] == ["LONG_TOKEN"]
    messages = [r.getMessage() for r in caplog.records if r.name == "horae.secrets"]
    assert messages == [
        "secret TINY is too short for reliable redaction (min 8 bytes): refused, so it is not "
        "primed and what needs it fails"
    ]
    assert "q#7Zp!" not in caplog.text


def test_scrub_rewrites_keys_flags_numbers_and_never_loses_a_colliding_key(
    seeded: SecretResolver,
) -> None:
    value = {LONG: [1, f"x{LONG}x", {"n": int(PIN), "s": PIN}], "ok": True, "none": None}
    scrubbed, names = redact.current().scrub_data(value)  # the CLI's: numbers redacted
    assert names == {"LONG_TOKEN", "PIN_CODE"}
    assert scrubbed == {
        token("LONG_TOKEN"): [
            1,
            f"x{token('LONG_TOKEN')}x",
            {"n": token("PIN_CODE"), "s": token("PIN_CODE")},
        ],
        "ok": True,
        "none": None,
    }
    flagged = redact.current().scrub({"n": int(PIN), "f": 1.5}, numbers="flag")  # the store's
    assert (flagged.value, flagged.flagged, flagged.redacted) == (
        {"n": int(PIN), "f": 1.5},
        frozenset({"PIN_CODE"}),
        frozenset(),
    )
    with pytest.raises(redact.SecretInKey):  # the host's variant still refuses keys
        redact.current().redact_data(value)
    # Decision 43 G: two keys redacting to the same text are both kept.
    colliding = {f"{LONG}": 1, token("LONG_TOKEN"): 2, base64.b64encode(LONG.encode()).decode(): 3}
    result = redact.current().scrub(colliding)
    assert result.value == {
        f"{token('LONG_TOKEN')}#2": 1,
        token("LONG_TOKEN"): 2,
        f"{token('LONG_TOKEN')}#3": 3,
    }
    assert result.collisions == {"LONG_TOKEN"}


def test_find_sees_strings_keys_and_whole_numbers(seeded: SecretResolver) -> None:
    finder = redact.current()
    assert finder.find({"a": [{"b": int(PIN)}]}) == {"PIN_CODE"}
    assert finder.find({LONG: 1}) == {"LONG_TOKEN"}
    assert finder.find([float(PIN) + 0.5, int(PIN) + 1, True, None]) == set()
    assert finder.find(f"x{PIN}y") == {"PIN_CODE"}


# --- short secrets: refused (maintainer decision, decision 43 H) --------------------------------


def test_a_short_secret_is_refused_at_resolution_without_its_value(tmp_path: Path) -> None:
    (tmp_path / "TINY").write_text("abc1234\n", encoding="utf-8")  # 7 bytes
    refs = {"ENV_TINY": EnvSecretRef(env="TINY_VAR")}
    resolver = SecretResolver(refs, tmp_path, {"TINY_VAR": "é1234"})  # 6 bytes of UTF-8
    for name, value in (("TINY", "abc1234"), ("ENV_TINY", "é1234")):
        with pytest.raises(SecretError) as raised:
            resolver.resolve(name)
        message = str(raised.value)
        assert message.endswith("is too short for reliable redaction (min 8 bytes)")
        assert value not in message
    assert resolver.resolved() == ()
    assert redact.known() == ()
    (tmp_path / "EIGHT").write_text("é123456", encoding="utf-8")  # 8 bytes of UTF-8
    assert resolver.resolve("EIGHT").reveal() == "é123456"


def test_doctor_reports_a_short_secret_as_an_error(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "inst", secrets={"dir": "secrets"})
    config = yaml.safe_load((root / "horae.yaml").read_text(encoding="utf-8"))
    config["models"]["default"]["secret"] = "TINY_KEY"  # noqa: S105 (a name)
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "TINY_KEY").write_text("tiny7ab", encoding="utf-8")
    problems = [p for p in check_instance(root).problems if "TINY_KEY" in str(p)]
    assert len(problems) == 1
    assert problems[0].severity == "error"
    assert "too short for reliable redaction (min 8 bytes)" in str(problems[0])
    assert "tiny7ab" not in str(problems[0])


# --- logs, warnings and uncaught exceptions ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "horae",
        "horae.store",
        "mcp.client.stdio",
        "client",
        "httpx2._client",
        "anthropic._base_client",
        "asyncio",
        "urllib3.connectionpool",
        "py.warnings",
        "uvicorn.error",
        "openai._base_client",
        "someone.else",
        "root",
    ],
)
def test_every_logger_is_scrubbed_with_no_host_open(
    seeded: SecretResolver, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    logs.install()
    logger = logging.getLogger(None if name == "root" else name)
    caplog.set_level(logging.DEBUG, logger=None if name == "root" else name)
    try:
        raise ValueError(f"boom {LONG}")
    except ValueError:
        logger.error("got %s and %r", LONG, {"pin": PIN}, exc_info=True, stack_info=True)
    (record,) = [r for r in caplog.records if r.name == name]
    assert record.getMessage() == (
        "got [REDACTED:LONG_TOKEN] and {'pin': '[REDACTED:PIN_CODE]'} "
        "(ValueError, details withheld)"
    )
    assert (record.args, record.exc_info, record.stack_info) == (None, None, None)
    assert not _leaks(caplog.text.encode(), LONG, PIN)


def test_warnings_are_captured_and_scrubbed(
    seeded: SecretResolver, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="py.warnings")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        logs.install()
        warnings.warn(f"deprecated {LONG}", UserWarning, stacklevel=1)
    (record,) = [r for r in caplog.records if r.name == "py.warnings"]
    assert "[REDACTED:LONG_TOKEN]" in record.getMessage()
    assert not _leaks(caplog.text.encode(), LONG)


def test_uncaught_exceptions_are_reported_redacted(
    seeded: SecretResolver, capsys: pytest.CaptureFixture[str]
) -> None:
    logs.install()
    assert sys.excepthook is logs.excepthook
    assert threading.excepthook is logs.thread_excepthook
    try:
        raise RuntimeError(f"crashed with {LONG} and {PIN}")
    except RuntimeError as error:
        logs.excepthook(RuntimeError, error, error.__traceback__)
        args = threading.ExceptHookArgs(
            (RuntimeError, error, error.__traceback__, threading.current_thread())
        )
        logs.thread_excepthook(args)
    logs.thread_excepthook(threading.ExceptHookArgs((SystemExit, SystemExit(), None, None)))
    err = capsys.readouterr().err
    assert (
        err.count("RuntimeError: crashed with [REDACTED:LONG_TOKEN] and [REDACTED:PIN_CODE]") == 2
    )
    assert "Exception in thread MainThread:" in err
    assert "Traceback" in err
    assert not _leaks(err.encode(), LONG, PIN)


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
    assert logs.covered("anything.at.all")


def test_the_cli_installs_the_scrubber(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(logs, "install", lambda: calls.append("install"))
    CliRunner().invoke(app, ["runs", "-i", str(tmp_path), "--json"])
    assert calls == ["install"]


def test_a_malformed_log_record_shows_its_format_only(
    seeded: SecretResolver, caplog: pytest.LogCaptureFixture
) -> None:
    logs.install()
    caplog.set_level(logging.INFO, logger="horae.test")
    logging.getLogger("horae.test").info("value %s %s", LONG)  # one argument missing
    (record,) = [r for r in caplog.records if r.name == "horae.test"]
    assert (record.getMessage(), record.args) == ("value %s %s", None)


# --- the store's secret gate, every table ------------------------------------------------------


def _forgetful_record() -> Any:
    """A run of `apply_fixtures` whose producers (fixture ports) echo the secrets everywhere
    in free text and redact nothing (a producer that forgot)."""
    intents = [
        {
            "type": "note.replace_owned_block",
            "path": "Daily/2026-10-27.assistant.md",
            "block": "horae-brief",
            "content": f"- [ ] token {LONG} pin {PIN}\n",
        },
        {"type": "task.create", "title": f"Rotate {PIN} now", "fingerprint": "fp-new"},
        {
            "type": "notify.push",
            "title": "Brief",
            "body": f"key={base64.b64encode(LONG.encode()).decode()}",
        },
    ]
    fixture = brief_plugins(intents)
    fixture.handlers["items.today"] = lambda p, i: [
        {"title": LONG, LONG: PIN, "n": int(PIN), "m": 3}
    ]
    return asyncio.run(run_pipeline(dry_run=False, fixture=fixture))


def _flags(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT run_id, detail FROM secret_flags ORDER BY id").fetchall()
    finally:
        conn.close()


def test_every_table_redacts_free_text_and_flags_identifiers(
    seeded: SecretResolver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logs.install()
    record = _forgetful_record()
    state = tmp_path / "state"
    note = f"Daily/{NAMED}.md"  # an identifier: kept as written, flagged
    with Store.open(state / "horae.db") as store:
        store.begin_run(record.id, record.pipeline, record.started_at, host=f"box-{NAMED}")
        leaky = StepRecord(
            kind="collect",
            status="degraded",
            error=StepError(kind="tool_error", message=f"src.read: bad {LONG}"),
            stderr=f"debug: pin={PIN}\n",
        )
        store.record_step(record.id, 0, leaky)  # while running: flagged for the record
        (row,) = store.steps(record.id)
        assert row.error is not None
        assert row.error.message == "src.read: bad [REDACTED:LONG_TOKEN]"
        assert row.stderr == "debug: pin=[REDACTED:PIN_CODE]\n"
        entry = store.journal_append(
            record.id,
            intent=0,
            intent_type="note.replace_owned_block",
            target=note,
            before=f"my own note {EXEMPT}\n".encode(),  # exempt: restored byte for byte
            after=f"horae wrote {EXEMPT} and {EXEMPT}\n".encode(),  # one more: flagged
            applied_by=f"vault.write_note-{NAMED}",
            sink_identity=f"mcp:vault:{NAMED}",
            at=AT,
        )
        store.journal_mark_applied(entry.id, actual_after=f"sink wrote {EXEMPT}".encode())
        store.upsert_task(
            TaskEntry(
                fingerprint=f"fp-{NAMED}",
                title=f"Pay {PIN} now",
                status="todo",
                target=f"T/{NAMED}",
                source=f"{NAMED}.x",
                run_id=record.id,
            ),
            at=AT,
        )
        store.record_notification(f"dedupe-{NAMED}", at=AT, run_id=record.id)
        usage = store.reserve_usage(
            record.id,
            step=1,
            attempt=1,
            alias=f"a-{NAMED}",
            at=AT,
            day=AT.date(),
            usd=0.1,
            tokens_out=10,
            backend="anthropic",
            model=f"m-{NAMED}",
        )
        store.settle_usage(
            usage, Usage(tokens_in=1, tokens_out=1, usd=0.0), at=AT, model=f"m2-{NAMED}"
        )
        assert _flags(state / "horae.db"), "the findings are kept in the database meanwhile"
        store.save_run(record)
        assert _flags(state / "horae.db") == []  # turned into violations, then deleted

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

        saved = store.get_run(record.id)
        assert saved is not None
        details = [v.detail for v in saved.violations if v.kind == "secret_in_output"]
        assert (
            "the store redacted secret LONG_TOKEN, PIN_CODE in the run record "
            "(a producer did not redact it)" in details
        )
        assert (
            "the store redacted secret LONG_TOKEN, PIN_CODE in step 0 "
            "(a producer did not redact it)" in details
        )
        assert (
            "the store found secret PIN_CODE in the run record (kept as written: "
            "identifiers and numbers are exempt)" in details
        )
        assert any(
            "found secret NOTE_KEY in the content Horae wrote to Daily/[REDACTED:NAMED].md, "
            "not in the note before" in d
            for d in details
        )
        assert not any("the content a sink wrote" in d for d in details)  # not new
        for where in (
            "a journal target",
            "a journal entry's sink identity",
            "a task fingerprint",
            "a task source",
            "a task target",
            "a notification key",
            "the usage ledger",
            "the run's host",
        ):
            assert (
                f"the store found secret NAMED in {where} (kept as written: identifiers and "
                "numbers are exempt)" in details
            ), where
        assert "a task title" in " ".join(details)
        # What is free text reads back redacted; identifiers and exempt bytes as written.
        (journalled,) = store.journal(record.id)
        assert journalled.target == note
        assert journalled.sink_identity == f"mcp:vault:{NAMED}"
        assert journalled.before_sha256 is not None
        assert journalled.after_sha256 is not None
        assert store.blob(journalled.before_sha256) == f"my own note {EXEMPT}\n".encode()
        task = store.get_task(f"fp-{NAMED}")
        assert task is not None
        assert task.title == "Pay [REDACTED:PIN_CODE] now"
        assert store.get_notification(f"dedupe-{NAMED}") is not None
        (group,) = store.usage_report()
        assert (group.alias, group.model) == (f"a-{NAMED}", f"m2-{NAMED}")
        crashed = store.run_summary("crashed-run")
        assert crashed is not None
        assert crashed.error == "crashed with [REDACTED:LONG_TOKEN]"
        data = saved.inputs[0].data[0]
        assert data["title"] == token("LONG_TOKEN")
        assert data[token("LONG_TOKEN")] == token("PIN_CODE")
        assert data["n"] == int(PIN)  # a number keeps its type: flagged, not rewritten

    data_bytes = _state_bytes(state)
    assert data_bytes, "the state directory holds the database"
    assert _leaks(data_bytes, LONG) == []
    assert _oracle(data_bytes, LONG) == []
    assert EXEMPT.encode() in data_bytes  # the documented exemption: the note bytes are kept
    assert NAMED.encode() in data_bytes  # identifiers are exempt (flagged)
    assert _leaks(caplog.text.encode(), LONG, PIN, EXEMPT, NAMED) == []
    assert "secret NOTE_KEY in the content Horae wrote" in caplog.text


@pytest.mark.parametrize("form", sorted(encodings(LONG)))
def test_every_encoding_is_redacted_in_the_store(tmp_path: Path, form: str) -> None:
    _resolver(tmp_path, {"LONG_TOKEN": LONG})
    with Store.open(tmp_path / "state" / "horae.db") as store:
        store.begin_run("r1", "p", AT)
        store.record_step("r1", 0, StepRecord(kind="collect", status="ok", output=f"<{form}>"))
        (step,) = store.steps("r1")
        assert step.output == "<[REDACTED:LONG_TOKEN]>"
    assert _leaks(_state_bytes(tmp_path / "state"), LONG) == []


def test_a_record_that_cannot_stay_valid_once_redacted_is_refused(tmp_path: Path) -> None:
    """The residual refusal: a token longer than the room its text field has left."""
    short_name_long_token = "A_VERY_LONG_SECRET_NAME_THAT_EXPANDS_ITS_TOKEN"  # noqa: S105
    _resolver(tmp_path, {short_name_long_token: "Ab3$Ab3$"})
    step = StepRecord(
        kind="collect",
        status="failed",
        error=StepError(kind="tool_error", message="x" * 4088 + "Ab3$Ab3$"),
    )
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run("r1", "p", AT)
        with pytest.raises(
            SecretRefused, match=f"step 0 of run 'r1' holds secret {short_name_long_token}"
        ):
            store.record_step("r1", 0, step)
        assert store.steps("r1") == []


def test_without_a_known_secret_the_record_is_stored_as_given(tmp_path: Path) -> None:
    record = asyncio.run(run_pipeline(dry_run=False))
    with Store.open(tmp_path / "horae.db") as store:
        store.save_run(record)
        assert store.get_run(record.id) == record
    conn = sqlite3.connect(tmp_path / "horae.db")
    (stored,) = conn.execute("SELECT record FROM runs").fetchone()
    (secure_delete,) = conn.execute("PRAGMA secure_delete").fetchone()
    conn.close()
    assert stored == record.model_dump_json()
    assert secure_delete == 0  # a fresh connection has the default: the store sets its own


def test_every_writable_store_connection_sets_secure_delete(tmp_path: Path) -> None:
    with Store.open(tmp_path / "horae.db") as store:
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 2  # FAST
        store.begin_run("r1", "p", AT)
        store.heartbeat("r1", AT)
        assert store._beat is not None
        assert store._beat.execute("PRAGMA secure_delete").fetchone()[0] == 2


def test_journal_before_bytes_restore_byte_exact(seeded: SecretResolver, tmp_path: Path) -> None:
    before = f"# Note\r\n{LONG} {PIN}\r\n".encode()
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


@pytest.mark.parametrize(
    ("before", "after", "flagged"),
    [
        (f"mine {EXEMPT}\n", f"mine {EXEMPT}\n- [ ] added\n", False),  # the user's own
        (f"mine {EXEMPT}\n", f"mine {EXEMPT}\nhorae {EXEMPT}\n", True),  # one more: new
        (None, f"new note {EXEMPT}\n", True),
        ("nothing here\n", f"now {base64.b64encode(EXEMPT.encode()).decode()}\n", True),
    ],
)
def test_journal_after_is_flagged_only_for_a_secret_new_since_before(
    seeded: SecretResolver, tmp_path: Path, before: str | None, after: str, flagged: bool
) -> None:
    record = brief_record(dry_run=False)
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run(record.id, record.pipeline, record.started_at)
        entry = store.journal_append(
            record.id,
            intent=0,
            intent_type="note.create" if before is None else "note.replace_owned_block",
            target="N.md",
            before=None if before is None else before.encode(),
            after=after.encode(),
            at=AT,
        )
        store.journal_mark_applied(entry.id, actual_after=after.encode())
        store.save_run(record)
        saved = store.get_run(record.id)
        assert saved is not None
        details = [v.detail for v in saved.violations]
        found = [d for d in details if "NOTE_KEY" in d]
        if flagged:
            assert found == [
                "the store found secret NOTE_KEY in the content Horae wrote to N.md, not in "
                "the note before (kept byte for byte: the journalled content is exempt)",
                "the store found secret NOTE_KEY in the content a sink wrote to N.md, not in "
                "the note before (kept byte for byte: the journalled content is exempt)",
            ]
        else:
            assert found == []
        assert entry.after_sha256 is not None
        assert store.blob(entry.after_sha256) == after.encode()  # byte for byte


def test_findings_survive_another_store_instance_and_abandon_drops_them(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    record = brief_record(dry_run=False)
    path = tmp_path / "horae.db"
    leaky = StepRecord(kind="collect", status="ok", output={"echo": LONG})
    with Store.open(path) as writer:
        writer.begin_run(record.id, record.pipeline, record.started_at)
        writer.record_step(record.id, 0, leaky)
        writer.begin_run("crashed", "other", AT)
        writer.record_step("crashed", 0, leaky)
    assert {run for run, _detail in _flags(path)} == {record.id, "crashed"}
    with Store.open(path) as other:  # another instance (another process, `horae undo`...)
        other.abandon_run("crashed", at=AT, error="lost")
        assert {run for run, _detail in _flags(path)} == {record.id}
        other.save_run(record)
        saved = other.get_run(record.id)
    assert saved is not None
    assert [v.detail for v in saved.violations] == [
        "the store redacted secret LONG_TOKEN in step 0 (a producer did not redact it)"
    ]
    assert _flags(path) == []


def test_findings_are_aggregated_when_the_violations_are_full(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    from horae.contracts.types import MAX_ITEMS

    record = brief_record(dry_run=False)
    data = record.model_dump(mode="json")
    data["violations"] = [
        {"kind": "secret_in_output", "detail": f"earlier finding {index}"}
        for index in range(MAX_ITEMS - 2)
    ]
    data["error"] = f"failed with {LONG}"
    data["status"] = "failed"
    full = type(record).model_validate(data)
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run(full.id, full.pipeline, full.started_at)
        for index in range(3):
            step = StepRecord(kind="collect", status="ok", output=[LONG, index])
            store.record_step(full.id, index, step)
        store.save_run(full)
        saved = store.get_run(full.id)
    assert saved is not None
    assert len(saved.violations) == MAX_ITEMS
    assert saved.violations[-2].detail == (
        "the store redacted secret LONG_TOKEN in step 0 (a producer did not redact it)"
    )
    assert saved.violations[-1].detail == (
        "the store's secret gate made 3 more findings for this run (names only, in the "
        "horae.store log)"
    )


def test_a_colliding_key_is_kept_unique_and_flagged_in_the_store(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    output = {LONG: "first", token("LONG_TOKEN"): "second"}
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run("r1", "p", AT)
        store.record_step("r1", 0, StepRecord(kind="collect", status="ok", output=output))
        (step,) = store.steps("r1")
    assert step.output == {f"{token('LONG_TOKEN')}#2": "first", token("LONG_TOKEN"): "second"}
    details = [detail for _run, detail in _flags(tmp_path / "horae.db")]
    assert (
        "redacting secret LONG_TOKEN made two object keys equal in step 0: the later one was "
        "renamed with a #2 (or next) suffix" in details
    )


def test_an_llm_cache_entry_holding_a_secret_is_not_cached(
    seeded: SecretResolver, tmp_path: Path
) -> None:
    usage = Usage(tokens_in=0, tokens_out=0, usd=0.0)
    kwargs: dict[str, Any] = {"skill": "pack/brief", "skill_version": "1", "model": "m"}
    with Store.open(tmp_path / "horae.db") as store:
        for response in ({"pin": int(PIN)}, {LONG: 1}, [f"x{PIN}"]):
            with pytest.raises(SecretRefused):
                store.cache_put("b" * 64, response=response, usage=usage, at=AT, **kwargs)
            assert not store.cache_put_safe(
                "b" * 64, response=response, usage=usage, at=AT, **kwargs
            )
            assert store.cache_get("b" * 64, now=AT) is None
        assert store.cache_put_safe("b" * 64, response={"n": 1}, usage=usage, at=AT, **kwargs)
        assert store.cache_get("b" * 64, now=AT) is not None


# --- identifiers equal to a secret: runs succeed, records are saved, undo works ----------------


def test_secrets_equal_to_structural_words_never_break_a_run_or_undo(tmp_path: Path) -> None:
    """Finding 1 (over-matching): secrets equal to the pipeline name, the plugin name, a
    year-shaped value inside every run id and an intent type. The run succeeds, its record is
    saved (identifiers kept as written, flagged), and undo finds the change again."""
    root, vault = _init(tmp_path)
    today = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    values = {
        "PIPE_WORD": "daily-fixture",
        "PLUGIN_WORD": "fixture-source",
        "YEAR_WORD": today,
        "KIND_WORD": "secret_in_output",
    }
    _add_secrets(root, values)
    code, out = _cli("run", "daily-fixture", "-i", str(root), "--json")
    assert code == 0, out
    assert json.loads(out)["run"]["status"] == "ok"
    with Store.open(root / "state" / "horae.db", read_only=True) as store:
        (summary,) = store.list_runs()
        record = store.get_run(summary.id)  # the CLI printed the id redacted: it holds one
        assert record is not None
        assert record.pipeline == "daily-fixture"
        assert today in record.id
        assert "fixture-source" in record.plugins
        details = " ".join(v.detail for v in record.violations)
        assert "kept as written" in details
        assert record.pipeline_definition is None  # the pipeline file holds a secret
        (change,) = record.changes
        assert (vault / change.target).exists()
    code, out = _cli("undo", record.id, "-i", str(root), "--json")
    assert code == 0, out
    assert not (vault / change.target).exists()
    with Store.open(root / "state" / "horae.db", read_only=True) as store:
        undone = store.get_run(record.id)
    assert undone is not None
    assert undone.status == "undone"


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


def _src_fixture() -> FixturePlugins:
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
    return fixture


@pytest.mark.parametrize(
    "params",
    [
        {"q": LONG},
        {"q": base64.b64encode(LONG.encode()).decode()},
        {"q": LONG.encode().hex()},
        {"q": f"x={PIN}"},
        {LONG: 1},
        {"pin": int(PIN)},
        yaml.safe_load(f"pin: {PIN}"),  # YAML reads an int
        {"nested": [{"deep": [int(PIN)]}]},
    ],
)
def test_a_parameter_holding_a_secret_refuses_the_run(
    seeded: SecretResolver, params: dict[str, Any]
) -> None:
    fixture = _src_fixture()
    pipeline = Pipeline.model_validate(
        {"name": "p", "steps": [{"collect": [{"src.read": params}]}]}
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
    assert PIN not in error.message
    assert record.pipeline_definition is None  # the definition holding it is not recorded


def test_a_number_that_is_not_a_secret_is_a_fine_parameter(seeded: SecretResolver) -> None:
    fixture = _src_fixture()
    params = {"pin": int(PIN) + 1, "q": "fine"}
    pipeline = Pipeline.model_validate(
        {"name": "p", "steps": [{"collect": [{"src.read": params}]}]}
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
    assert record.status == "ok"
    assert record.pipeline_definition is not None


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
            "intents": [{"type": "notify.push", "title": PIN}],
        }
        return CompletionResult(output=output, usage=Usage(tokens_in=1, tokens_out=1, usd=0.0))

    async def aclose(self) -> None:
        return None


class _Prompts:
    def __init__(self, system: str = "Summarise.", schema: dict[str, Any] | None = None) -> None:
        self.system = system
        self.schema = schema or {"type": "object"}

    def prompt(self, request: LlmRequest) -> Prompt:
        return Prompt(self.system, request.input, self.schema, "1")


def _backends(
    provider: _Provider, prompts: _Prompts, tmp_path: Path, *, valid: bool = False
) -> ModelBackends:
    spec = ModelAlias.model_validate(
        {"backend": "ollama", "model": "fixture-model", "base_url": "http://localhost:11434"}
    )
    return ModelBackends(
        {"default": spec},
        SecretResolver({}, tmp_path),
        {"ollama": lambda alias, spec, secrets: provider},
        prompts,
        validate=lambda output, schema: [] if valid else [f"bad value {LONG}"],
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
    assert response.intents == []  # an invalid output proposes nothing (decision 44)
    assert response.schema_errors == ("bad value [REDACTED:LONG_TOKEN]",)
    valid = _backends(_Provider(), _Prompts(), tmp_path, valid=True)
    accepted = asyncio.run(valid.complete(_request()))
    assert accepted.intents == [{"type": "notify.push", "title": token("PIN_CODE")}]
    assert accepted.schema_errors == ()
    assert LONG not in json.dumps(response.request)
    assert "secret LONG_TOKEN, PIN_CODE found in a response of model alias default" in caplog.text
    assert _leaks(caplog.text.encode(), LONG, PIN) == []


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


@pytest.mark.parametrize(
    "prompts",
    [
        _Prompts(f"Use {LONG}."),
        _Prompts(schema={"type": "object", "properties": {"pin": {"const": int(PIN)}}}),
        _Prompts(schema={"type": "object", "properties": {LONG: {"type": "string"}}}),
    ],
)
def test_a_skill_prompt_or_schema_holding_a_secret_is_refused_before_anything_is_sent(
    seeded: SecretResolver, tmp_path: Path, prompts: _Prompts
) -> None:
    provider = _Provider()
    with pytest.raises(LlmError) as raised:
        asyncio.run(_backends(provider, prompts, tmp_path).complete(_request()))
    assert raised.value.kind == "params_invalid"
    assert raised.value.message.startswith(
        "the prompt or output schema of skill pack/brief holds secret "
    )
    assert LONG not in raised.value.message
    assert PIN not in raised.value.message
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
    echo(f"err {PIN}", err=True)
    echo(to_json({LONG: [f"v {LONG}", "é" + LONG, int(PIN)]}))
    out, err = capsys.readouterr()
    assert _leaks(out.encode() + err.encode(), LONG, PIN) == []
    assert "text [REDACTED:LONG_TOKEN]" in out
    assert "err [REDACTED:PIN_CODE]" in err
    assert json.loads(out[out.index("{") :]) == {
        token("LONG_TOKEN"): [
            "v [REDACTED:LONG_TOKEN]",
            "é[REDACTED:LONG_TOKEN]",
            token("PIN_CODE"),
        ]
    }


@pytest.mark.parametrize("secret", ["Del\x7fSecret-Value-42", "Zero\u200bWidth-Secret-42"])
def test_safe_redacts_before_it_escapes_and_before_text_is_cut(
    tmp_path: Path, secret: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _resolver(tmp_path, {"ODD_TOKEN": secret})
    assert safe(f"got {secret}!") == "got [REDACTED:ODD_TOKEN]!"
    echo(f"horae run: {safe(f'failed: {secret}')}", err=True)
    assert capsys.readouterr().err == "horae run: failed: [REDACTED:ODD_TOKEN]\n"
    # An HTTP call's body is cut at 200 characters, after redaction: no half secret.
    body = {"pad": "p" * 170, "token": secret}  # the secret straddles the cut
    changes = ChangeSet(
        run_id="r1",
        pipeline="p",
        run_status="ok",
        dry_run=True,
        http_calls=(HttpItem(0, "hook.post", HttpCall("https://hooks.example.org/x", body)),),
    )
    text = render_text(changes)
    assert secret[:4] not in text.replace("[REDACTED:ODD_TOKEN]", "")
    assert '"token":"[REDACTED:' in text
    assert "\\u200b" not in text
    assert "\\x7f" not in text


@pytest.mark.parametrize("command", ["diff", "replay", "undo"])
def test_a_command_echoing_a_secret_prints_it_redacted(tmp_path: Path, command: str) -> None:
    root = _secret_instance(tmp_path / "inst")
    for json_flag in ([], ["--json"]):
        result = CliRunner().invoke(app, [command, LONG, "-i", str(root), *json_flag])
        assert result.exit_code != 0
        printed = (result.stdout + result.stderr).encode()
        assert _leaks(printed, LONG) == [], printed
        assert b"[REDACTED:LONG_TOKEN]" in printed


PLUGIN_SECRET = "Sentinel-Plugin-Granted-Secret-42"  # noqa: S105 (synthetic)


def _instance_with_a_plugin_secret(tmp_path: Path) -> Path:
    """An instance whose only knowledge of `PLUGIN_TOKEN` is `horae.lock` (no `secrets.refs`,
    no model alias): the secret is granted to a plugin, read from `secrets/PLUGIN_TOKEN`."""
    root = tmp_path / "inst"
    (root / "pipelines").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "secrets" / "PLUGIN_TOKEN").write_text(PLUGIN_SECRET, encoding="utf-8")
    config = {"timezone": "UTC", "state_dir": "state", "secrets": {"dir": "secrets"}}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    pipeline = {"name": "feed", "steps": [{"collect": [{"fixture-source.serve": {}}]}]}
    (root / "pipelines" / "feed.yaml").write_text(yaml.safe_dump(pipeline), encoding="utf-8")
    entry = {
        "version": "0.1.0",
        "repo": "https://github.com/horae-dev/horae.git",
        "commit": "0" * 40,
        "permissions": {"network": [], "secrets": ["PLUGIN_TOKEN"], "paths": {}, "intents": []},
    }
    lock = {"schema_version": 1, "plugins": {"fixture-source": entry}}
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    return root


def test_a_plugin_secret_is_redacted_by_every_command_that_reads_the_store(
    tmp_path: Path,
) -> None:
    root = _instance_with_a_plugin_secret(tmp_path)
    assert load_instance(root).redaction_names() == ("PLUGIN_TOKEN",)
    # A record written by a process that did not know the secret (no gate could catch it).
    with Store.open(root / "state" / "horae.db") as store:
        store.begin_run("r1", "feed", AT)
        store.abandon_run("r1", at=AT, error=f"plugin said {PLUGIN_SECRET}")
    assert redact.known() == ()
    for args in (
        ["runs", "-i", str(root), "--json"],
        ["runs", "-i", str(root)],
        ["usage", "-i", str(root), "--json"],
        ["diff", PLUGIN_SECRET, "-i", str(root), "--json"],
        ["replay", PLUGIN_SECRET, "-i", str(root), "--json"],
        ["undo", PLUGIN_SECRET, "-i", str(root), "--json"],
    ):
        redact.forget_all()  # each command primes the registry itself
        result = CliRunner().invoke(app, args)
        printed = (result.stdout + result.stderr).encode()
        assert _leaks(printed, PLUGIN_SECRET) == [], (args, printed)
        if args[0] != "usage":
            assert b"[REDACTED:PLUGIN_TOKEN]" in printed, (args, printed)


def test_a_lock_that_cannot_be_read_primes_nothing_from_it(tmp_path: Path) -> None:
    from horae.config.loader import read_lock

    root = _instance_with_a_plugin_secret(tmp_path)
    (root / "horae.lock").write_text("plugins: [not, a, mapping", encoding="utf-8")
    assert read_lock(root) is None
    assert read_lock(tmp_path / "nowhere") is None
    result = CliRunner().invoke(app, ["runs", "-i", str(root), "--json"])
    assert result.exit_code == 0, result.stdout
    assert redact.known() == ()


# --- end to end: the walking skeleton with a fixture source that echoes the secrets -------------


def _cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, [*args])
    return result.exit_code, result.stdout + result.stderr


def _init(tmp_path: Path) -> tuple[Path, Path]:
    code, out = _cli("init", str(tmp_path / "instance"), "--json")
    assert code == 0, out
    created = json.loads(out)
    return Path(created["instance"]), Path(created["vault"])


def _add_secrets(root: Path, values: Mapping[str, str]) -> None:
    """`values` as `secrets.refs` file references of the instance."""
    secrets = root / "secrets"
    secrets.mkdir(exist_ok=True)
    refs = {}
    for name, value in values.items():
        (secrets / name.lower()).write_text(value + "\n", encoding="utf-8")
        refs[name] = {"file": name.lower()}
    config = yaml.safe_load((root / "horae.yaml").read_text(encoding="utf-8"))
    config["secrets"] = {"dir": "secrets", "refs": refs}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _echoing_fixture(path: Path, rng_seed: int) -> None:
    """A `fixture-source` file whose data and proposed note content hold the secrets in every
    encoding the redactor knows and in tricky placements (next to separators, inside JSON
    strings, as numbers), at pseudo-random places (seeded: reproducible)."""
    import random

    rng = random.Random(rng_seed)  # noqa: S311 (reproducible placement, not cryptography)
    forms = [*sorted(encodings(LONG)), *sorted(encodings(SPECIAL)), PIN]
    words = ["alpha", "beta", "gamma", "delta", "", "x"]
    separators = [" ", ",", "/", ":", "=", ";", "|", "\t", "(", ")", '"', "'"]
    items: list[Any] = []
    for index, form in enumerate(forms):
        left = rng.choice(words) + rng.choice(separators)
        right = rng.choice(separators) + rng.choice(words)
        items.append(
            {
                "id": f"fx-{index}",
                "title": f"{left}{form}{right}",
                "note": [form, {"k": form}, json.dumps({"inner": form})],
            }
        )
    items.append({"id": "fx-num", "title": "numbers", "pin": int(PIN), "list": [int(PIN), 1]})
    lines = "".join(f"- [ ] {item['title']} ({item['id']})\n" for item in items)
    rng.shuffle(items)
    document = {
        "data": {
            "items": items,
            "header": f"Authorization: Basic {base64.b64encode(f'u:{LONG}'.encode()).decode()}",
        },
        "intents": [{"type": "note.replace_owned_block", "block": "fixture", "content": lines}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("seed", [53, 2026, 7, 4242])
def test_the_walking_skeleton_never_stores_logs_or_prints_an_echoed_secret(
    tmp_path: Path, seed: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    root, vault = _init(tmp_path)
    feed = tmp_path / "feed"
    feed.mkdir()
    _echoing_fixture(feed / "data.json", seed)
    # LONG and SPECIAL by explicit file references, PIN found by its name (granted).
    _add_secrets(root, {"LONG_TOKEN": LONG, "SPECIAL_TOKEN": SPECIAL})
    (root / "secrets" / "PIN_CODE").write_text(PIN, encoding="utf-8")
    lock = yaml.safe_load((root / "horae.lock").read_text(encoding="utf-8"))
    lock["plugins"]["fixture-source"]["permissions"]["paths"] = {"read": [str(feed)]}
    lock["plugins"]["fixture-source"]["permissions"]["secrets"] = ["PIN_CODE"]
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
    assert set(run["run"]["violations"]) == {"secret_in_output"}  # the host flagged the echo
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
    assert _leaks(state_bytes, LONG, SPECIAL, PIN) == []  # the database, its -wal and -shm
    assert _oracle(state_bytes, LONG, SPECIAL, PIN) == []  # the independent oracle
    assert b"[REDACTED:LONG_TOKEN]" in state_bytes  # the echoed data was stored, redacted
    assert _leaks(vault_bytes, LONG, SPECIAL, PIN) == []  # what Horae wrote (then undid)
    assert _oracle(vault_bytes, LONG, SPECIAL, PIN) == []
    shown = "".join(printed).encode()
    assert _leaks(shown, LONG, SPECIAL, PIN) == []
    assert _oracle(shown, LONG, SPECIAL, PIN) == []
    assert _leaks(caplog.text.encode(), LONG, SPECIAL, PIN) == []
    for record in caplog.records:
        assert _leaks(record.getMessage().encode(), LONG, SPECIAL, PIN) == []
    assert "[REDACTED:LONG_TOKEN]" in "".join(printed)
