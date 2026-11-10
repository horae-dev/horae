# SPDX-License-Identifier: Apache-2.0
"""Approval-gated `pipeline.run` (#55, schemas decision 77): the dry run an MCP client asks
for, the user's approval at the terminal (`horae approval`), the binding, expiry, single use,
denial, revocation, flood limits and the approval journal. Fixture ports only (no plugin
process); synthetic data."""

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from mcp import Client
from typer.testing import CliRunner

import horae.mcp_server
from apply_fixtures import INBOX
from apply_fixtures import PIPELINE as DRY_BRIEF
from apply_fixtures import plugins as brief_plugins
from horae import approval as approval_module
from horae import cli_approval
from horae.apply import StaticSinks
from horae.apply.testing import FixtureSink
from horae.approval import (
    MAX_OPEN,
    MAX_PER_HOUR,
    ApprovalError,
    Digests,
    Requester,
    apply,
    binding_digest,
    preview,
    request_dry_run,
)
from horae.cli import app
from horae.cli_host import load
from horae.config import STORE_FILE, Instance
from horae.contracts.mcp import PipelineRun
from horae.engine.testing import FixturePlugins
from horae.mcp_server import PipelineRuns, Reader, build_server
from horae.runner import PipelineLock, Ports
from horae.store import ApprovalLimited, ApprovalRow, Store
from runner_fixtures import Plugins, write_instance

runner = CliRunner()
APPEND = {"type": "note.append_section", "path": INBOX, "heading": "B", "content": "approved"}
STDIO = Requester(None, "stdio")
BEFORE = b"# Inbox\n"


class Ports_:
    """What `horae.approval.live_ports` yields instead of the plugin host's ports."""

    def __init__(self) -> None:
        self.plugins: FixturePlugins = brief_plugins([APPEND])
        self.sink = FixtureSink(files={INBOX: BEFORE})
        self.opened = 0


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Ports_:
    holder = Ports_()

    @asynccontextmanager
    async def live_ports(instance: Instance) -> AsyncIterator[Any]:
        holder.opened += 1
        sinks = StaticSinks(default=holder.sink)
        ports = Ports(plugins=holder.plugins, tools=holder.plugins, sinks=sinks)
        yield SimpleNamespace(ports=ports, sinks=sinks)

    monkeypatch.setattr(approval_module, "live_ports", live_ports)
    return holder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance")
    definition = DRY_BRIEF.model_dump(mode="json", exclude_none=True)
    (root / "pipelines" / "dry-brief.yaml").write_text(yaml.safe_dump(definition), "utf-8")
    return root


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_approval, "_interactive", lambda: True)


def ask(root: Path, who: Requester = STDIO, pipeline: str = "dry-brief", **kw: Any) -> Any:
    return asyncio.run(request_dry_run(load(root), pipeline, who, **kw))


def row(root: Path, approval_id: str) -> ApprovalRow:
    with Store.open(root / "state" / STORE_FILE) as store:
        found = store.approval(approval_id)
    assert found is not None
    return found


def events(root: Path, approval_id: str) -> list[str]:
    with Store.open(root / "state" / STORE_FILE) as store:
        return [e.event for e in store.approval_events(approval_id)]


def journal(root: Path) -> list[Any]:
    with Store.open(root / "state" / STORE_FILE) as store:
        runs = store.list_runs(limit=100)
        return [entry for run in runs for entry in store.journal(run.id)]


def cli(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, ["approval", *args], input=input)
    return result.exit_code, result.output


# --- asking: a dry run, nothing applied ----------------------------------------------------


def test_pipeline_run_applies_nothing_without_approval(fake: Ports_, root: Path) -> None:
    done = ask(root)
    assert done.status == "pending"
    assert done.record.dry_run is True
    assert fake.sink.files == {INBOX: BEFORE}
    assert fake.sink.applied == []
    assert journal(root) == []
    found = row(root, done.approval_id)
    assert found.status == "pending"
    assert found.dry_run_id == done.record.id
    assert found.applied_run_id is None
    assert events(root, done.approval_id) == ["planning", "pending"]


def test_an_unapplicable_dry_run_gets_no_approval(fake: Ports_, root: Path) -> None:
    fake.plugins = Plugins()
    done = ask(root, pipeline="collect")  # proposes nothing
    assert (done.status, done.reason) == ("void", "nothing_to_apply")
    assert row(root, done.approval_id).status == "void"
    failed = ask(root, pipeline="broken")
    assert (failed.status, failed.reason) == ("void", "run_failed")
    with pytest.raises(ApprovalError) as unknown:
        ask(root, pipeline="nope")
    assert unknown.value.kind == "unknown_pipeline"


def test_a_running_pipeline_is_refused_and_recorded(fake: Ports_, root: Path) -> None:
    lock = PipelineLock.try_acquire(root / "state", "dry-brief")
    assert lock is not None
    try:
        with pytest.raises(ApprovalError) as locked:
            ask(root)
    finally:
        lock.release()
    assert locked.value.kind == "pipeline_locked"
    with Store.open(root / "state" / STORE_FILE) as store:
        (only,) = store.approvals()
    assert (only.status, only.reason) == ("void", "pipeline_locked")


# --- approving at the terminal ---------------------------------------------------------------


def test_an_approved_plan_is_applied_once_as_a_new_run(fake: Ports_, root: Path, tty: None) -> None:
    done = ask(root)
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 0, out
    assert "## B" in out  # the change set was shown before the question
    assert b"approved" in fake.sink.files[INBOX]
    applied = row(root, done.approval_id)
    assert applied.status == "applied"
    assert applied.applied_run_id is not None
    assert applied.applied_run_id != done.record.id
    entries = journal(root)
    assert [(e.run_id, e.target, e.applied) for e in entries] == [
        (applied.applied_run_id, INBOX, True)
    ]
    with Store.open(root / "state" / STORE_FILE) as store:
        record = store.get_run(applied.applied_run_id)
    assert record is not None
    assert (record.dry_run, record.status, record.cost.usd, record.llm) == (False, "ok", 0.0, [])
    assert record.intents == done.record.intents
    # Single use: a second approval of the same id applies nothing more.
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 1
    assert "is applied" in out
    assert len(fake.sink.applied) == 1
    assert events(root, done.approval_id) == ["planning", "pending", "approving", "applied"]


def test_two_concurrent_approvals_of_one_id_apply_once(fake: Ports_, root: Path) -> None:
    done = ask(root)
    instance = load(root)
    seen = asyncio.run(preview(instance, done.approval_id))

    async def both() -> tuple[Any, ...]:
        return await asyncio.gather(
            apply(instance, seen), apply(instance, seen), return_exceptions=True
        )

    results = asyncio.run(both())
    records = [r for r in results if not isinstance(r, BaseException)]
    assert len(records) == 1
    assert len(fake.sink.applied) == 1
    assert row(root, done.approval_id).status == "applied"
    # The claim itself is a compare-and-set: a second one finds it no longer pending.
    with Store.open(root / "state" / STORE_FILE) as store:
        assert store.approval_claim(done.approval_id, at=datetime.now(UTC)) is False


def test_an_approval_expires(fake: Ports_, root: Path) -> None:
    t0 = datetime.now(UTC)
    done = ask(root, ttl=timedelta(minutes=1), now=lambda: t0)
    assert done.expires_at == t0 + timedelta(minutes=1)
    later = t0 + timedelta(minutes=1, seconds=1)
    with pytest.raises(ApprovalError) as expired:
        asyncio.run(preview(load(root), done.approval_id, now=lambda: later))
    assert expired.value.kind == "not_pending"
    assert row(root, done.approval_id).status == "expired"
    assert events(root, done.approval_id) == ["planning", "pending", "expired"]
    assert fake.sink.applied == []


def test_an_approval_expiring_after_the_review_applies_nothing(fake: Ports_, root: Path) -> None:
    t0 = datetime.now(UTC)
    done = ask(root, ttl=timedelta(minutes=1), now=lambda: t0)
    instance = load(root)
    seen = asyncio.run(preview(instance, done.approval_id, now=lambda: t0))
    later = t0 + timedelta(minutes=2)
    with pytest.raises(ApprovalError) as expired:
        asyncio.run(apply(instance, seen, now=lambda: later))
    assert expired.value.kind == "not_pending"
    assert fake.sink.applied == []
    assert row(root, done.approval_id).status == "expired"
    with Store.open(root / "state" / STORE_FILE) as store:
        assert [r.dry_run for r in store.list_runs(limit=10)] == [True]  # no run began


def test_a_changed_note_refuses_the_plan(fake: Ports_, root: Path, tty: None) -> None:
    done = ask(root)
    fake.sink.files[INBOX] = b"# Inbox\nedited by hand\n"
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 4, out
    assert "changed since the dry run" in out
    assert fake.sink.applied == []
    refused = row(root, done.approval_id)
    assert refused.status == "refused"
    assert refused.reason is not None
    assert refused.reason.startswith("approval_mismatch")
    assert events(root, done.approval_id) == ["planning", "pending", "refused"]


def test_a_changed_configuration_refuses_the_plan(fake: Ports_, root: Path, tty: None) -> None:
    done = ask(root)
    config = yaml.safe_load((root / "horae.yaml").read_text("utf-8"))
    config["models"]["default"]["model"] = "opus"
    (root / "horae.yaml").write_text(yaml.safe_dump(config), "utf-8")
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 4, out
    assert fake.sink.applied == []
    assert row(root, done.approval_id).status == "refused"


def test_a_change_after_the_review_is_refused_under_the_apply_lock(
    fake: Ports_, root: Path
) -> None:
    done = ask(root)
    instance = load(root)
    seen = asyncio.run(preview(instance, done.approval_id))
    fake.sink.files[INBOX] = b"# Inbox\nchanged meanwhile\n"
    record = asyncio.run(apply(instance, seen))
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.startswith("approval_mismatch")
    assert record.changes == []
    assert fake.sink.applied == []
    assert journal(root) == []
    refused = row(root, done.approval_id)
    assert (refused.status, refused.applied_run_id) == ("refused", record.id)
    assert events(root, done.approval_id) == ["planning", "pending", "approving", "refused"]


def test_the_binding_covers_the_instance_digests(fake: Ports_, root: Path) -> None:
    done = ask(root)
    base = Digests.of_record(done.record)
    digest = binding_digest(done.record, done.plan, base)
    assert digest == row(root, done.approval_id).binding_sha256
    for changed in (
        Digests("0" * 64, base.config, base.lock),
        Digests(base.pipeline, "0" * 64, base.lock),
        Digests(base.pipeline, base.config, "0" * 64),
    ):
        assert binding_digest(done.record, done.plan, changed) != digest
    assert Digests.of_instance(load(root), "dry-brief") == base


def test_approving_needs_an_interactive_terminal(fake: Ports_, root: Path) -> None:
    done = ask(root)
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 1
    assert "needs an interactive terminal" in out
    assert row(root, done.approval_id).status == "pending"
    assert fake.sink.applied == []


def test_an_unconfirmed_approval_stays_pending(fake: Ports_, root: Path, tty: None) -> None:
    done = ask(root)
    code, out = cli("approve", done.approval_id, "-i", str(root), input="n\n")
    assert code == 1
    assert "nothing applied" in out
    assert row(root, done.approval_id).status == "pending"
    assert fake.sink.applied == []


def test_an_unknown_approval_is_refused(fake: Ports_, root: Path, tty: None) -> None:
    ask(root)
    code, out = cli("approve", "apr-aaaaaaaaaaaa", "-i", str(root), input="y\n")
    assert code == 3
    assert "no approval" in out


# --- denial and revocation -------------------------------------------------------------------


def test_a_denied_approval_applies_nothing(fake: Ports_, root: Path, tty: None) -> None:
    done = ask(root)
    code, out = cli("deny", done.approval_id, "-i", str(root))
    assert code == 0, out
    assert row(root, done.approval_id).status == "denied"
    code, out = cli("approve", done.approval_id, "-i", str(root), input="y\n")
    assert code == 1
    assert "denied" in out
    assert fake.sink.applied == []
    code, _ = cli("deny", done.approval_id, "-i", str(root))
    assert code == 1  # no longer pending
    assert events(root, done.approval_id) == ["planning", "pending", "denied"]


def test_revoke_all_ends_every_pending_approval(fake: Ports_, root: Path) -> None:
    first, second = ask(root), ask(root)
    code, out = cli("revoke", "-i", str(root))
    assert code == 2  # --all is required
    code, out = cli("revoke", "--all", "-i", str(root), "--json")
    assert code == 0, out
    assert sorted(json.loads(out)["revoked"]) == sorted([first.approval_id, second.approval_id])
    assert {row(root, a.approval_id).status for a in (first, second)} == {"revoked"}


def test_a_revoked_token_revokes_its_pending_approvals(fake: Ports_, root: Path) -> None:
    store_path = root / "state" / STORE_FILE
    with Store.open(store_path) as store:
        token_id = store.mcp_token_add(
            "agent", b"\x01" * 32, ("pipeline.run",), at=datetime.now(UTC), expires_at=None
        )
    done = ask(root, Requester(token_id, "agent"))
    with Store.open(store_path) as store:
        assert store.mcp_token_revoke("agent", at=datetime.now(UTC))
    with pytest.raises(ApprovalError) as revoked:
        asyncio.run(preview(load(root), done.approval_id))
    assert revoked.value.kind == "revoked"
    assert row(root, done.approval_id).status == "revoked"
    assert fake.sink.applied == []


# --- flood limits --------------------------------------------------------------------------


def test_a_requester_has_a_bounded_number_of_pending_approvals(fake: Ports_, root: Path) -> None:
    asked = [ask(root) for _ in range(MAX_OPEN)]
    with pytest.raises(ApprovalError) as full:
        ask(root)
    assert full.value.kind == "too_many_pending"
    cli("deny", asked[0].approval_id, "-i", str(root))
    assert ask(root).status == "pending"  # room again


def test_a_requester_is_rate_limited_per_hour(root: Path) -> None:
    store_path = root / "state" / STORE_FILE
    t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    with Store.open(store_path) as store:
        for n in range(MAX_PER_HOUR):
            at = t0 + timedelta(minutes=n)
            store.approval_open(
                f"apr-{n:012d}", "p", token_id=None, requested_by="stdio", at=at,
                max_open=100, max_per_hour=MAX_PER_HOUR,
            )  # fmt: skip
            store.approval_void(f"apr-{n:012d}", at=at, reason="nothing_to_apply")
        with pytest.raises(ApprovalLimited) as limited:
            store.approval_open(
                "apr-x", "p", token_id=None, requested_by="stdio", at=t0 + timedelta(minutes=30),
                max_open=100, max_per_hour=MAX_PER_HOUR,
            )  # fmt: skip
        assert limited.value.kind == "rate_limited"
        store.approval_open(
            "apr-y", "p", token_id=None, requested_by="stdio", at=t0 + timedelta(minutes=61),
            max_open=100, max_per_hour=MAX_PER_HOUR,
        )  # fmt: skip


# --- the journal and the listing -----------------------------------------------------------


def test_list_and_show_report_every_outcome(fake: Ports_, root: Path, tty: None) -> None:
    applied = ask(root)
    assert cli("approve", applied.approval_id, "-i", str(root), input="y\n")[0] == 0
    denied = ask(root)
    cli("deny", denied.approval_id, "-i", str(root))
    void = ask(root, pipeline="collect")
    code, out = cli("list", "-i", str(root), "--json")
    assert code == 0, out
    statuses = {a["id"]: a["status"] for a in json.loads(out)["approvals"]}
    assert statuses == {
        applied.approval_id: "applied",
        denied.approval_id: "denied",
        void.approval_id: "void",
    }
    code, out = cli("list", "-i", str(root))
    assert code == 0
    assert "3 approvals, 0 pending" in out
    code, out = cli("show", applied.approval_id, "-i", str(root), "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert [e["event"] for e in shown["events"]] == ["planning", "pending", "approving", "applied"]
    assert shown["events"][-1]["run_id"] == shown["approval"]["applied_run_id"]
    code, out = cli("show", applied.approval_id, "-i", str(root))
    assert code == 0
    assert "applied" in out
    assert cli("show", "apr-bbbbbbbbbbbb", "-i", str(root))[0] == 3


# --- the MCP side ----------------------------------------------------------------------------


def reader(root: Path) -> Reader:
    state = root / "state"
    return Reader(state, state / STORE_FILE, "Europe/Paris", "daily-brief")


def mcp_call(root: Path, name: str, args: dict[str, Any]) -> Any:
    async def go() -> Any:
        server = build_server(reader(root), stdio=True, runs=PipelineRuns(root))
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return asyncio.run(go())


def test_the_mcp_tool_returns_a_summary_and_an_approval_id(fake: Ports_, root: Path) -> None:
    result = mcp_call(root, "pipeline.run", {"pipeline": "dry-brief"})
    assert not result.is_error
    out = PipelineRun.model_validate(result.structured_content)
    assert out.approval is not None
    assert out.approval.approve_with == f"horae approval approve {out.approval.id}"
    assert out.counts.notes == 1
    assert [(c.kind, c.action, c.target) for c in out.changes] == [("note", "modify", INBOX)]
    assert "approved" not in json.dumps(result.structured_content)  # never the content
    assert fake.sink.applied == []
    assert row(root, out.approval.id).requested_by == "stdio"


def test_the_mcp_tool_reports_errors_without_detail(fake: Ports_, root: Path) -> None:
    result = mcp_call(root, "pipeline.run", {"pipeline": "Not A Name"})
    assert result.is_error
    assert "unknown_pipeline" in json.dumps([c.model_dump() for c in result.content])
    result = mcp_call(root, "pipeline.run", {"pipeline": "nope"})
    assert result.is_error


def test_the_mcp_surface_exposes_no_approve_path(fake: Ports_, root: Path) -> None:
    async def go() -> tuple[set[str], Any]:
        server = build_server(reader(root), stdio=True, runs=PipelineRuns(root))
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            tried = await client.call_tool("approval.approve", {"id": "apr-aaaaaaaaaaaa"})
            return names, tried

    names, tried = asyncio.run(go())
    assert names == {"tasks.today", "brief.latest", "runs.recent", "pipeline.run"}
    assert not any(w in n for n in names for w in ("approve", "deny", "revoke", "apply"))
    assert tried.is_error
    # Nothing that approves is even imported by the server module.
    source = inspect.getsource(horae.mcp_server)
    for name in ("approval_claim", "approval_decide", "approval_finish", "apply_planned"):
        assert name not in source
    for name in ("preview", "apply", "deny"):
        assert getattr(horae.mcp_server, name, None) is not getattr(approval_module, name)
