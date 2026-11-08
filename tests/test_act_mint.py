# SPDX-License-Identifier: Apache-2.0
"""Notification buttons (#38, schemas decision 54 A): `notify.push.actions` in the engine
(rule 4, the action guard), the task index the apply phase keeps, the links the core mints
when it sends a notification (and how the notifier receives them), the minter's conditions,
`horae serve`'s `/act` setup, `horae doctor`, and an end-to-end tap."""

import asyncio
import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from act_fixtures import KEY_VALUE, PUBLIC, Clock, key
from apply_fixtures import PIPELINE, RUN_ID, START, plugins, run_pipeline
from horae.api.act import ActRoute, LinkMinter, act_minter, tasks_route, verify
from horae.api.act import _mint as mint_module
from horae.api.act._service import ActService
from horae.apply import ActionLink, StaticSinks, apply_changes, plan_changes
from horae.apply.plan import dedupe_key
from horae.apply.testing import FixtureSink
from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, Pipeline, RunRecord
from horae.contracts.intents import NotifyAction, NotifyPush
from horae.engine.canonical import sha256_hex
from horae.engine.guard import check_intents
from horae.store import Store, TaskEntry

FP = "fp-old"  # collected by `items.today` in `apply_fixtures`
BUTTONS = [
    {"label": "Done", "kind": "task.done", "task": FP},
    {"label": "In 1 hour", "kind": "task.snooze", "task": FP, "duration": "1h"},
]
TASK_CREATE = {"type": "task.create", "title": "Old task", "fingerprint": FP, "due": "2026-10-28"}
NOTIFY = {"type": "notify.push", "title": "Due", "body": "Old task is due", "actions": BUTTONS}


def _record(intents: list[dict[str, Any]]) -> RunRecord:
    return asyncio.run(run_pipeline(dry_run=False, fixture=plugins(intents)))


def _apply(store: Store, record: RunRecord, sink: FixtureSink, minter: Any = None) -> Any:
    store.begin_run(record.id, record.pipeline, record.started_at)
    sinks = StaticSinks(default=sink)

    async def go() -> Any:
        planned = await plan_changes(record, sinks)
        return await apply_changes(
            record,
            planned,
            sinks,
            store,
            clock=lambda: START,
            minter=minter,
            timezone="Europe/Paris",
        )

    result = asyncio.run(go())
    store.save_run(result.record)
    return result


def _notifications(sink: FixtureSink) -> list[Any]:
    return [call for call in sink.applied if isinstance(call.intent, NotifyPush)]


# --- the engine --------------------------------------------------------------------------------


def test_buttons_route_only_for_a_task_the_run_collected() -> None:
    record = _record([TASK_CREATE, NOTIFY])
    assert record.status == "ok"
    assert [p.intent.type for p in record.intents] == [
        "task.create",
        "notify.push",
    ]
    guessed = {**NOTIFY, "actions": [{"label": "Done", "kind": "task.done", "task": "fp-guess"}]}
    refused = _record([guessed])
    assert refused.status == "failed"
    errors = [step.error for step in refused.steps if step.error is not None]
    assert errors
    assert errors[0].kind == "intent_unrouted"
    mixed = {**NOTIFY, "actions": [*BUTTONS, {"label": "x", "kind": "task.done", "task": "fp-x"}]}
    assert _record([mixed]).status == "failed"  # every button's task, not just one


def test_the_action_guard_scans_button_labels() -> None:
    def push(label: str) -> NotifyPush:
        action = NotifyAction(label=label, kind="task.done", task=FP)
        return NotifyPush(type="notify.push", body="x", actions=[action])

    assert check_intents([push("Done")], shown={"t": FP}, source="skill:x/y") is None
    for label in ("see https://evil.example.net", "[x](javascript:alert(1))", "a\u202eb"):
        refused = check_intents([push(label)], shown={"t": FP}, source="skill:x/y")
        assert refused is not None
        assert "`actions`" in refused


def test_a_notification_without_buttons_keeps_its_dedupe_key() -> None:
    record = _record([{"type": "notify.push", "title": "Brief", "body": "2 items today"}])
    index = next(i for i, p in enumerate(record.intents) if p.intent.type == "notify.push")
    proposed = record.intents[index]
    # The key as it was computed before `actions` existed: the dump without the field.
    intent = proposed.intent.model_dump(mode="json")
    del intent["actions"]
    old = sha256_hex(
        {
            "pipeline": record.pipeline,
            **({"input_hash": record.input_hash} if record.input_hash else {"run_id": record.id}),
            "applied_by": proposed.applied_by,
            "params": {"when": "07:30"},
            "intent": intent,
            "occurrence": 0,
        }
    )
    assert dedupe_key(record, index) == old
    with_buttons = _record([TASK_CREATE, NOTIFY])
    assert dedupe_key(with_buttons, 1) != old


# --- the task index ----------------------------------------------------------------------------


def test_applied_task_changes_keep_the_task_index(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        _apply(store, _record([TASK_CREATE]), FixtureSink())
        task = store.get_task(FP)
        assert task is not None
        assert (task.title, task.status, task.due, task.source, task.run_id) == (
            "Old task",
            "todo",
            dt.date(2026, 10, 28),
            "vault.tasks",
            RUN_ID,
        )
        store.set_task_status(FP, "done", at=START)
        # A re-proposed task.create (an unchanged input) never reopens a done task.
        rerun = _record([{**TASK_CREATE, "title": "Old task, renamed", "due": "2026-10-30"}])
        rerun = rerun.model_copy(update={"id": "20261027T053001Z-dry-brief-02"})
        _apply(store, rerun, FixtureSink())
        task = store.get_task(FP)
        assert task is not None
        assert (task.status, task.title, task.due) == (
            "done",
            "Old task, renamed",
            dt.date(2026, 10, 30),
        )


def test_a_status_update_changes_the_index_and_snoozes_in_the_instance_zone(
    tmp_path: Path,
) -> None:
    with Store.open(tmp_path / "s.db") as store:
        store.upsert_task(TaskEntry(fingerprint=FP, title="Old task", status="todo"), at=START)
        update = {
            "type": "task.update_status",
            "task": FP,
            "status": "snoozed",
            "until": "2026-11-02",
        }
        _apply(store, _record([update]), FixtureSink())
        task = store.get_task(FP)
        assert task is not None
        assert task.status == "snoozed"
        assert task.snooze_tz == "Europe/Paris"
        assert task.wake_at == dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)


def test_an_update_of_an_unindexed_task_is_left_out(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        update = {"type": "task.update_status", "task": FP, "status": "done"}
        result = _apply(store, _record([update]), FixtureSink())
        assert result.failure is None
        assert store.get_task(FP) is None


# --- minting -----------------------------------------------------------------------------------


def test_the_core_mints_the_buttons_and_passes_them_to_the_notifier(tmp_path: Path) -> None:
    sink = FixtureSink()
    minter = LinkMinter(key(), PUBLIC, clock=lambda: START)
    with Store.open(tmp_path / "s.db") as store:
        _apply(store, _record([TASK_CREATE, NOTIFY]), sink, minter)
        task = store.get_task(FP)
    assert task is not None
    (call,) = _notifications(sink)
    assert [link.label for link in call.actions] == ["Done", "In 1 hour"]
    for link, button in zip(call.actions, BUTTONS, strict=True):
        assert isinstance(link, ActionLink)
        assert link.method == "POST"
        assert link.url.startswith(f"{PUBLIC}/act/")
        parsed = verify(key(), link.url.rsplit("/", 1)[1], int(START.timestamp()))
        assert parsed is not None
        assert (parsed.kind, parsed.task, parsed.duration) == (
            button["kind"],
            FP,
            button.get("duration"),
        )
        assert link.to_wire() == {
            "action": "http",
            "label": button["label"],
            "url": link.url,
            "method": "POST",
        }
    # The proposal itself never holds a URL; the record and the journal hold no link.
    assert "act/" not in call.intent.model_dump_json()


def test_the_journal_and_the_record_hold_no_minted_link(tmp_path: Path) -> None:
    sink = FixtureSink()
    minter = LinkMinter(key(), PUBLIC, clock=lambda: START)
    path = tmp_path / "s.db"
    with Store.open(path) as store:
        _apply(store, _record([TASK_CREATE, NOTIFY]), sink, minter)
    (call,) = _notifications(sink)
    data = b"".join(p.read_bytes() for p in tmp_path.glob("s.db*"))
    for link in call.actions:
        token = link.url.rsplit("/", 1)[1]
        assert token.encode() not in data
        assert b"/act/" not in data


def test_a_button_for_an_unindexed_task_is_left_out(tmp_path: Path) -> None:
    sink = FixtureSink()
    minter = LinkMinter(key(), PUBLIC, clock=lambda: START)
    with Store.open(tmp_path / "s.db") as store:
        _apply(store, _record([NOTIFY]), sink, minter)  # no task.create: not in the index
    (call,) = _notifications(sink)
    assert call.actions == ()
    assert sink.sent  # the notification itself is sent


def test_without_a_minter_the_notification_is_sent_without_buttons(tmp_path: Path) -> None:
    sink = FixtureSink()
    with Store.open(tmp_path / "s.db") as store:
        _apply(store, _record([TASK_CREATE, NOTIFY]), sink)
    (call,) = _notifications(sink)
    assert call.actions == ()
    assert len(sink.sent) == 1


def test_a_failing_minter_never_fails_the_notification(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def mint(self, actions: Any, store: Store) -> Any:
            raise RuntimeError(f"boom {KEY_VALUE}")

    sink = FixtureSink()
    with Store.open(tmp_path / "s.db") as store:
        result = _apply(store, _record([TASK_CREATE, NOTIFY]), sink, Broken())
    assert result.failure is None
    (call,) = _notifications(sink)
    assert call.actions == ()
    assert "buttons not minted (RuntimeError)" in caplog.text
    assert KEY_VALUE not in caplog.text


# --- the minter's conditions ----------------------------------------------------------------


def _instance(tmp_path: Path, *, http: dict[str, Any] | None = None, **extra: Any) -> Instance:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    config = {"secrets": {"dir": str(secrets)}, "http": http or {}, **extra}
    pipelines = {"dry-brief": PIPELINE}
    return Instance(
        root=tmp_path,
        config=InstanceConfig.model_validate(config),
        pipelines=pipelines,
        lock=Lockfile(schema_version=1),
    )


def _set_key(tmp_path: Path, value: str = KEY_VALUE) -> None:
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "HORAE_ACT_KEY").write_text(value + "\n")


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HORAE_ACT_KEY", raising=False)
    monkeypatch.setattr(mint_module, "_said", set())


def test_the_minter_needs_a_public_url_a_key_and_a_route(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    http = {"public_url": PUBLIC}
    assert act_minter(_instance(tmp_path)) is None  # links not configured: silent
    assert act_minter(_instance(tmp_path, http=http)) is None
    assert "HORAE_ACT_KEY is not set" in caplog.text
    _set_key(tmp_path, "short-key-value")
    assert act_minter(_instance(tmp_path, http=http)) is None
    assert "too short" in caplog.text
    assert "short-key-value" not in caplog.text
    _set_key(tmp_path)
    minter = act_minter(_instance(tmp_path, http=http))
    assert isinstance(minter, LinkMinter)
    assert minter.public_url == PUBLIC
    assert minter.key.kid == key().kid


def test_the_reasons_are_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        act_minter(_instance(tmp_path, http={"public_url": PUBLIC}))
    assert caplog.text.count("HORAE_ACT_KEY is not set") == 1


def _pipeline(*entries: dict[str, Any]) -> Pipeline:
    return Pipeline.model_validate(
        {"name": "p", "steps": [{"collect": ["items.today"]}, {"act": list(entries)}]}
    )


def test_the_route_is_the_one_tasks_entry() -> None:
    tasks = {"vault.tasks": {"mode": "tasks"}}
    assert tasks_route({"a": PIPELINE}) == ActRoute("vault.tasks", {"mode": "tasks"})
    assert tasks_route({"a": _pipeline(tasks), "b": _pipeline(tasks)}) == ActRoute(
        "vault.tasks", {"mode": "tasks"}
    )  # the same entry in two pipelines
    other = {"todo.tasks": {"mode": "tasks"}}
    assert tasks_route({"a": _pipeline(tasks), "b": _pipeline(other)}) is None  # ambiguous
    assert (
        tasks_route({"a": _pipeline({"vault.write_note": {"mode": "create", "path": "x.md"}})})
        is None
    )
    templated = {"vault.tasks": {"mode": "tasks", "list": "{{date}}"}}
    assert tasks_route({"a": _pipeline(templated)}) is None


# --- end to end: notification, tap -----------------------------------------------------------


def test_a_minted_button_applies_once_end_to_end(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from act_fixtures import HOST, IDLE, sinks_of
    from horae.api import ApiContext, create_app
    from horae.api.act._limit import RateLimiter
    from horae.api.act._routes import ActSetup

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = state_dir / "horae.db"
    notifier, tasks = FixtureSink(), FixtureSink()
    with Store.open(path) as store:
        _apply(
            store,
            _record([TASK_CREATE, NOTIFY]),
            notifier,
            LinkMinter(key(), PUBLIC, lambda: START),
        )
    (call,) = _notifications(notifier)
    done, later = (link.url for link in call.actions)
    clock = Clock(START + dt.timedelta(minutes=5))
    service = ActService(
        key(),
        path,
        state_dir,
        tasks_route({"a": PIPELINE}) or ActRoute("x.y"),
        sinks_of(tasks),
        timezone="Europe/Paris",
        clock=clock,
    )
    setup = ActSetup(service, PUBLIC, RateLimiter(per_ip_burst=20))
    app = create_app(ApiContext(path, state_dir, lambda: IDLE, frozenset({HOST}), setup))
    with TestClient(app, base_url=PUBLIC) as client:
        assert client.get(done.removeprefix(PUBLIC)).status_code == 200
        assert client.post(done.removeprefix(PUBLIC)).status_code == 200
        assert client.post(done.removeprefix(PUBLIC)).status_code == 404
        # The other button of the same notification is stale now: the task is done.
        assert client.post(later.removeprefix(PUBLIC)).status_code == 409
    assert tasks.tasks == {FP: "done"}
    with Store.open(path) as store:
        task = store.get_task(FP)
        assert task is not None
        assert task.status == "done"


# --- horae serve and horae doctor ---------------------------------------------------------------


def test_serve_turns_act_on_only_when_everything_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.api.act import _route
    from horae.cli_output import CommandFailed
    from horae.cli_serve import build_act

    hosts = frozenset({"localhost", "horae.example.org"})
    setup, status = build_act(_instance(tmp_path), hosts)
    assert setup.service is None
    assert "HORAE_ACT_KEY is not set" in status
    _set_key(tmp_path)
    setup, status = build_act(_instance(tmp_path), hosts)
    assert setup.service is None
    assert "http.public_url" in status
    http = {"public_url": PUBLIC, "trusted_proxies": ["172.18.0.0/16"]}
    setup, status = build_act(_instance(tmp_path, http=http), hosts)
    assert setup.service is None
    assert "may not apply task.update_status" in status
    monkeypatch.setattr("horae.api.act.route_permitted", lambda route, intents_of: True)
    monkeypatch.setattr(_route, "route_permitted", lambda route, intents_of: True)
    setup, status = build_act(_instance(tmp_path, http=http), hosts)
    assert setup.service is not None
    assert status == "/act links on"
    assert setup.public_url == PUBLIC
    assert [str(n) for n in setup.trusted_proxies] == ["172.18.0.0/16"]
    _set_key(tmp_path, "x" * 20)
    with pytest.raises(CommandFailed) as refused:
        build_act(_instance(tmp_path, http=http), hosts)
    assert refused.value.kind == "config_invalid"
    assert "too short" in refused.value.message


def test_serve_warns_when_the_public_host_is_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from horae.cli_serve import build_act

    monkeypatch.setattr("horae.api.act.route_permitted", lambda route, intents_of: True)
    _set_key(tmp_path)
    build_act(_instance(tmp_path, http={"public_url": PUBLIC}), frozenset({"localhost"}))
    assert "http.public_url's host is not an allowed host" in caplog.text


def test_doctor_reports_an_unusable_key(tmp_path: Path) -> None:
    from horae.cli_host import _act_problems

    assert _act_problems(_instance(tmp_path)) == []
    _set_key(tmp_path, "x" * 20)
    (problem,) = _act_problems(_instance(tmp_path))
    assert problem.severity == "error"
    assert "too short" in problem.message
    _set_key(tmp_path)
    (warning,) = _act_problems(_instance(tmp_path))
    assert warning.severity == "warning"
    assert "http.public_url" in warning.message
    assert _act_problems(_instance(tmp_path, http={"public_url": PUBLIC})) == []


def test_the_runner_and_the_cli_load_no_web_framework() -> None:
    code = (
        "import sys, horae.runner, horae.cli_run, horae.api.act\n"
        "assert 'fastapi' not in sys.modules, 'fastapi loaded'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603 (this interpreter)
