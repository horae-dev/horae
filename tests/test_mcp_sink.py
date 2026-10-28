# SPDX-License-Identifier: Apache-2.0
"""The MCP-backed sink adapter (`horae.plugins.host.sink`, #25) against a scripted host."""

import asyncio
import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apply_fixtures import DAILY, START, plugins, run_pipeline
from horae.apply import (
    RestorableSink,
    SinkCall,
    SinkError,
    apply_changes,
    plan_changes,
    sink_identity,
    undo_run,
)
from horae.apply.sink import MAX_IDENTITY
from horae.contracts import PluginManifest, Violation
from horae.contracts.intents import NoteCreate, NoteReplaceOwnedBlock, NotifyPush
from horae.contracts.plugin import Provides
from horae.engine.ports import ToolResult
from horae.plugins.host import HostRegistry, McpSink, McpSinks, PluginCallError, RegistryError
from horae.plugins.host.sink import (
    MAX_MCP_NOTE_BYTES,
    MAX_SINK_MESSAGE,
    clean_message,
    decode,
    encode,
)
from horae.store import Store
from plugin_fixtures import host, lock_for, probe_manifest

Reply = Callable[[str, dict[str, Any]], Any]
NOTE = "Daily/x.assistant.md"


class ScriptedHost:
    """An `OperationCaller` answering each operation with `replies[tool](arguments)`."""

    def __init__(self, **replies: Reply) -> None:
        self.replies = {"identity": lambda _t, _a: {"identity": "local:1:2:/vault"}, **replies}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_operation(
        self, plugin: str, tool: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> ToolResult:
        self.calls.append((plugin, tool, arguments))
        operation = tool.split(".", 1)[1]
        reply = self.replies[operation](tool, arguments)
        if isinstance(reply, Exception):
            raise reply
        return ToolResult(data=reply)


def _manifest(**provides: list[str]) -> dict[str, Any]:
    return {
        "name": "notes",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": provides or {"sinks": ["notes.write"]},
        "permissions": {
            "intents": ["note.create", "note.replace_owned_block", "notify.push"],
            "paths": {"write": ["/vault"]},
        },
    }


def _registry(**provides: list[str]) -> HostRegistry:
    data = _manifest(**provides)
    return HostRegistry(lock_for(data), {"notes": PluginManifest.model_validate(data)})


def _sink(scripted: ScriptedHost) -> McpSink:
    sink = McpSinks(_registry(), scripted).mcp_sink_for("notes.write")
    assert sink is not None
    return sink


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _call(intent: Any) -> SinkCall:
    return SinkCall("run-1", 0, "notes.write", {"path": NOTE}, intent, "hash", None)


def _replace(content: str = "hello\n") -> NoteReplaceOwnedBlock:
    return NoteReplaceOwnedBlock(
        type="note.replace_owned_block", path=NOTE, block="b", content=content
    )


def test_registry_resolves_sinks_and_notifiers_once_per_namespace() -> None:
    scripted = ScriptedHost()
    sinks = McpSinks(
        _registry(sinks=["notes.write", "notes.other"], sources=["notes.read_all"]), scripted
    )
    first = sinks.sink_for("notes.write")
    assert first is not None
    assert sinks.sink_for("notes.other") is first  # one sink per (plugin, namespace)
    assert sinks.sink_for("notes.read_all") is None  # a source applies nothing
    assert sinks.sink_for("nobody.write") is None
    assert sinks.tools() == ("notes.other", "notes.write")
    assert isinstance(first, RestorableSink)


def test_read_decodes_and_passes_the_locked_paths() -> None:
    scripted = ScriptedHost(read=lambda _t, a: {"content": _b64(b"note\n")})
    sink = _sink(scripted)
    assert asyncio.run(sink.read(_call(_replace()))) == b"note\n"
    tools = [tool for _, tool, _ in scripted.calls]
    assert tools == ["notes.identity", "notes.read"]  # identity first, once
    arguments = scripted.calls[1][2]
    assert arguments == {"paths": {"read": [], "write": ["/vault"]}, "path": NOTE}
    assert sink_identity(sink) == "mcp:notes:local:1:2:/vault"
    assert asyncio.run(sink.read_target(NOTE)) == b"note\n"
    assert [tool for _, tool, _ in scripted.calls].count("notes.identity") == 1


def test_read_refuses_other_intents_and_maps_errors() -> None:
    sink = _sink(ScriptedHost(read=lambda _t, _a: {"content": None}))
    notify = NotifyPush(type="notify.push", body="hi")
    with pytest.raises(SinkError) as caught:
        asyncio.run(sink.read(_call(notify)))
    assert caught.value.kind == "not_supported"
    assert asyncio.run(sink.read_target(NOTE)) is None


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ({"error": {"kind": "symlink", "message": "a link\nhere"}}, "symlink"),
        ({"error": {"kind": "Bad-Kind", "message": "x"}}, "host_error"),
        ({"error": "no"}, "output_invalid"),
        (["not", "an", "object"], "output_invalid"),
        ({"content": 3}, "output_invalid"),
        ({"content": "!!not base64!!"}, "output_invalid"),
        ({"content": "é"}, "output_invalid"),
        ({"content": "A" * (4 * (MAX_MCP_NOTE_BYTES // 3 + 1) + 4)}, "note_too_large"),
        (PluginCallError("timeout", "notes.read: took too long"), "timeout"),
    ],
)
def test_read_failures(reply: Any, kind: str) -> None:
    sink = _sink(ScriptedHost(read=lambda _t, _a: reply))
    with pytest.raises(SinkError) as caught:
        asyncio.run(sink.read_target(NOTE))
    assert caught.value.kind == kind
    assert "\n" not in caught.value.message


def test_apply_sends_the_core_bytes_with_the_compare_and_swap() -> None:
    before = b"# Note\n"
    after = b"# Note\n\n<!-- horae:begin b -->\nhello\n<!-- horae:end b -->\n"
    digest = hashlib.sha256(after).hexdigest()
    scripted = ScriptedHost(
        read=lambda _t, _a: {"content": _b64(before)},
        apply=lambda _t, a: {"target": NOTE, "after_sha256": digest},
    )
    sink = _sink(scripted)
    expected = hashlib.sha256(before).hexdigest()
    applied = asyncio.run(sink.apply(_call(_replace()), expected_before_sha256=expected))
    assert (applied.target, applied.before, applied.after) == (NOTE, before, after)
    arguments = scripted.calls[-1][2]
    assert base64.b64decode(arguments["content"]) == after
    assert arguments["expected_before_sha256"] == expected
    assert arguments["intent"]["block"] == "b"
    assert arguments["tool"] == "notes.write"
    assert arguments["params"] == {"path": NOTE}


def test_apply_checks_the_note_and_what_the_sink_wrote() -> None:
    read: Reply = lambda _t, _a: {"content": _b64(b"x\n")}  # noqa: E731
    changed = _sink(ScriptedHost(read=read))
    with pytest.raises(SinkError) as caught:
        asyncio.run(changed.apply(_call(_replace()), expected_before_sha256=None))
    assert caught.value.kind == "target_changed"
    mismatch = _sink(ScriptedHost(read=read, apply=lambda _t, _a: {"after_sha256": "0" * 64}))
    expected = hashlib.sha256(b"x\n").hexdigest()
    with pytest.raises(SinkError) as caught:
        asyncio.run(mismatch.apply(_call(_replace()), expected_before_sha256=expected))
    assert caught.value.kind == "apply_mismatch"
    owned = _sink(ScriptedHost(read=lambda _t, _a: {"content": None}))
    other = NoteCreate(type="note.create", path="Daily/x.md", content="x")
    with pytest.raises(SinkError) as caught:
        asyncio.run(owned.apply(_call(other), expected_before_sha256=None))
    assert caught.value.kind == "not_owned"  # the core's conventions, before any write


def test_apply_passes_other_intents_through() -> None:
    scripted = ScriptedHost(apply=lambda _t, a: {"target": "ntfy", "details": {"id": 1}})
    sink = _sink(scripted)
    notify = NotifyPush(type="notify.push", body="hi")
    applied = asyncio.run(sink.apply(_call(notify), expected_before_sha256=None))
    assert (applied.target, applied.details) == ("ntfy", {"id": 1})
    assert scripted.calls[-1][2]["content"] is None
    bare = _sink(ScriptedHost(apply=lambda _t, a: {}))
    applied = asyncio.run(bare.apply(_call(notify), expected_before_sha256=None))
    assert (applied.target, applied.details) == ("notes.write", {})


def test_restore_and_parent_exists() -> None:
    scripted = ScriptedHost(
        restore=lambda _t, a: {}, parent_exists=lambda _t, a: {"exists": a["path"] == NOTE}
    )
    sink = _sink(scripted)
    asyncio.run(sink.restore(NOTE, b"old\n", expected_sha256="a" * 64))
    assert scripted.calls[-1][2]["content"] == _b64(b"old\n")
    asyncio.run(sink.restore(NOTE, None, expected_sha256="a" * 64))
    assert scripted.calls[-1][2]["content"] is None
    assert asyncio.run(sink.parent_exists(NOTE)) is True
    assert asyncio.run(sink.parent_exists("Other/y.md")) is False
    broken = _sink(ScriptedHost(parent_exists=lambda _t, a: {"exists": "yes"}))
    with pytest.raises(SinkError) as caught:
        asyncio.run(broken.parent_exists(NOTE))
    assert caught.value.kind == "output_invalid"


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ({"identity": ""}, "identity_invalid"),
        ({"identity": "a\nb"}, "identity_invalid"),
        ({"identity": 3}, "identity_invalid"),
        ({"error": {"kind": "vault_unset", "message": "none"}}, "vault_unset"),
    ],
)
def test_identity_failures_are_kept(reply: Any, kind: str) -> None:
    sink = _sink(ScriptedHost(identity=lambda _t, _a: reply))
    with pytest.raises(SinkError) as caught:
        sink_identity(sink)
    assert caught.value.kind == "identity_unknown"  # not fetched yet
    asyncio.run(sink.prepare())
    asyncio.run(sink.prepare())  # cached: asked once
    for _ in range(2):
        with pytest.raises(SinkError) as caught:
            sink_identity(sink)
        assert caught.value.kind == kind


def test_a_long_identity_is_hashed_and_prepare_covers_every_sink() -> None:
    long = "local:1:2:/" + "v" * MAX_IDENTITY
    scripted = ScriptedHost(identity=lambda _t, _a: {"identity": long})
    sinks = McpSinks(_registry(sinks=["notes.write"], notifiers=["notes.push"]), scripted)
    asyncio.run(sinks.prepare(sinks.tools()))
    sink = sinks.mcp_sink_for("notes.write")
    assert sink is not None
    identity = sink_identity(sink)
    assert identity is not None
    assert identity.startswith("mcp:notes:sha256:")
    assert len(identity) <= MAX_IDENTITY
    assert [tool for _, tool, _ in scripted.calls] == ["notes.identity"]


def test_base64_and_messages() -> None:
    assert decode(encode(b"\x00\xff")) == b"\x00\xff"
    assert decode(None) is None
    with pytest.raises(SinkError) as caught:
        encode(b"x" * (MAX_MCP_NOTE_BYTES + 1))
    assert caught.value.kind == "note_too_large"
    assert clean_message("a\u202eb\tc\n") == "ab c"
    assert clean_message(None) == "the sink reported an error"
    assert clean_message("\x00") == "the sink reported an error"
    assert len(clean_message("x" * 2000)) == MAX_SINK_MESSAGE


def test_call_operation_reaches_sink_operations_only(tmp_path: Path) -> None:
    """`call_operation` calls `<ns>.<operation>` of a sink namespace of the plugin, nothing else
    (not a provided tool, not another unprovided one); every call carries the locked paths."""
    manifest = probe_manifest(
        provides={"sources": ["probe.echo"], "sinks": ["probe.write"]},
        permissions={"secrets": ["PROBE_TOKEN"], "paths": {"read": ["/srv/data"]}},
    )

    async def go() -> None:
        async with host(tmp_path, manifest) as live:
            result = await live.call_operation("probe", "probe.identity", {"paths": "arg"})
            assert result.data["identity"] == "probe"
            assert result.data["paths"] == "arg"
            assert result.data["meta_paths"] == {"read": ["/srv/data"], "write": []}
            echoed = await live.call("probe.echo", {"value": 1}, None)
            assert echoed.data["value"] == 1
            for plugin, tool in [
                ("probe", "probe.echo"),  # provided: a step's tool, never an operation
                ("probe", "probe.text"),  # listed, unprovided, not an operation name
                ("probe", "other.identity"),  # not one of its sink namespaces
                ("probe", "probe"),
                ("nobody", "nobody.read"),
            ]:
                with pytest.raises(PluginCallError) as caught:
                    await live.call_operation(plugin, tool, {})
                assert caught.value.kind == "tool_missing", tool
            with pytest.raises(PluginCallError) as caught:
                await live.call_operation("probe", "probe.read", {})  # not in tools/list
            assert caught.value.kind == "tool_missing"

    asyncio.run(go())


def test_sink_operation_names_are_reserved() -> None:
    clash = _manifest(sinks=["notes.write"], sources=["notes.read"])
    with pytest.raises(ValidationError, match="sink operation"):
        PluginManifest.model_validate(clash)
    notifier = _manifest(notifiers=["notes.push"], transforms=["notes.identity"])
    with pytest.raises(ValidationError, match="sink operation"):
        PluginManifest.model_validate(notifier)
    PluginManifest.model_validate(_manifest(sinks=["notes.write"], sources=["x.read"]))
    # Across plugins: another plugin may not provide `notes.read` either.
    other = {**_manifest(sources=["notes.read"]), "name": "other"}
    with pytest.raises(RegistryError, match="sink operation"):
        HostRegistry(
            lock_for(_manifest(), other),
            {
                "notes": PluginManifest.model_validate(_manifest()),
                "other": PluginManifest.model_validate(other),
            },
        )
    # A manifest built without validation is refused by the registry too.
    unchecked = PluginManifest.model_validate(_manifest()).model_copy(
        update={"provides": Provides(sinks=["notes.write"], sources=["notes.apply"])}
    )
    with pytest.raises(RegistryError, match="sink operation"):
        HostRegistry(lock_for(_manifest()), {"notes": unchecked})


def test_transient_identity_failures_are_retried_once() -> None:
    answers: list[Any] = [
        PluginCallError("timeout", "notes.identity: took too long"),
        PluginCallError("plugin_crashed", "gone"),
        {"identity": "local:1:2:/vault"},
    ]
    scripted = ScriptedHost(identity=lambda _t, _a: answers.pop(0))
    sink = _sink(scripted)
    asyncio.run(sink.prepare())
    with pytest.raises(SinkError) as caught:
        sink_identity(sink)
    assert caught.value.kind == "timeout"  # the last failure, not cached yet
    asyncio.run(sink.prepare())  # retried once
    with pytest.raises(SinkError) as caught:
        sink_identity(sink)
    assert caught.value.kind == "plugin_crashed"
    asyncio.run(sink.prepare())  # cached now: not asked again
    assert [tool for _, tool, _ in scripted.calls] == ["notes.identity"] * 2
    with pytest.raises(SinkError):
        sink_identity(sink)
    later: list[Any] = [PluginCallError("timeout", "slow"), {"identity": "local:1:2:/vault"}]
    recovered = _sink(ScriptedHost(identity=lambda _t, _a: later.pop(0)))
    asyncio.run(recovered.prepare())
    asyncio.run(recovered.prepare())
    assert sink_identity(recovered) == "mcp:notes:local:1:2:/vault"


# --- apply_changes and undo_run through McpSinks ---------------------------------------------


def _apply_registry() -> HostRegistry:
    """The sinks of `apply_fixtures.PIPELINE` as plugins of the host."""
    manifests = [
        {
            "name": "vault",
            "version": "0.1.0",
            "horae": ">=0",
            "provides": {"sinks": ["vault.write_note", "vault.tasks"]},
            "permissions": {"intents": ["note.replace_owned_block", "task.create"]},
        },
        {
            "name": "ntfy",
            "version": "0.1.0",
            "horae": ">=0",
            "provides": {"notifiers": ["ntfy.push"]},
            "permissions": {"intents": ["notify.push"]},
        },
    ]
    models = {str(data["name"]): PluginManifest.model_validate(data) for data in manifests}
    return HostRegistry(lock_for(*manifests), models)


def test_apply_changes_prepares_the_runs_sinks_and_records_violations(tmp_path: Path) -> None:
    """task.create and notify.push through McpSinks: nothing reads their target first, so the
    core prepares the identities (`prepare_sinks`) before journalling."""
    leaked = Violation(kind="secret_in_output", detail="vault.apply returned secret TOKEN")
    applied: list[dict[str, Any]] = []

    def apply(_t: str, a: dict[str, Any]) -> Any:
        applied.append(a)
        return {"target": "done"}

    class Leaky(ScriptedHost):
        async def call_operation(
            self,
            plugin: str,
            tool: str,
            arguments: dict[str, Any],
            *,
            timeout_s: float | None = None,
        ) -> ToolResult:
            result = await super().call_operation(plugin, tool, arguments)
            if tool.endswith(".apply"):
                return ToolResult(data=result.data, violations=(leaked,))
            return result

    scripted = Leaky(apply=apply)
    sinks = McpSinks(_apply_registry(), scripted)
    intents = [
        {"type": "task.create", "title": "Water the plants", "fingerprint": "fp-1"},
        {"type": "notify.push", "title": "Brief", "body": "hi"},
    ]
    run = asyncio.run(run_pipeline(dry_run=False, fixture=plugins(intents)))
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run(run.id, run.pipeline, run.started_at)

        async def go() -> Any:
            planned = await plan_changes(run, sinks)
            return await apply_changes(run, planned, sinks, store, clock=lambda: START)

        result = asyncio.run(go())
        assert result.failure is None, result.failure
        journal = store.journal(run.id)
    assert [change.intent for change in result.applied] == [0, 1]
    assert [entry.sink_identity for entry in journal] == [
        "mcp:vault:local:1:2:/vault",
        "mcp:ntfy:local:1:2:/vault",
    ]
    tools = [tool for _, tool, _ in scripted.calls]
    assert tools[:2] == ["ntfy.identity", "vault.identity"]  # the run's sinks, first, once
    assert tools.count("vault.identity") == tools.count("ntfy.identity") == 1
    assert [a["intent"]["type"] for a in applied] == ["task.create", "notify.push"]
    assert result.record.violations == [leaked]  # deduplicated, kept on the run
    assert sinks.take_violations() == ()


def test_sink_violations_outside_a_run_are_logged_by_name(caplog: pytest.LogCaptureFixture) -> None:
    leaked = Violation(kind="secret_in_output", detail="notes.read returned secret TOKEN")

    class Leaky(ScriptedHost):
        async def call_operation(
            self,
            plugin: str,
            tool: str,
            arguments: dict[str, Any],
            *,
            timeout_s: float | None = None,
        ) -> ToolResult:
            result = await super().call_operation(plugin, tool, arguments)
            return ToolResult(data=result.data, violations=(leaked,))

    sinks = McpSinks(_registry(), Leaky(read=lambda _t, _a: {"content": None}))
    sink = sinks.mcp_sink_for("notes.write")
    assert sink is not None
    with caplog.at_level("WARNING", logger="horae.plugins.host.sink"):
        asyncio.run(sink.read_target(NOTE))
    assert "returned secret TOKEN" in caplog.text
    assert sinks.take_violations() == (leaked,)


def test_undo_prepares_its_sinks_itself(tmp_path: Path) -> None:
    """`undo_run` through McpSinks with no manual `prepare`."""
    state: dict[str, bytes | None] = {"note": None}

    def read(_t: str, _a: dict[str, Any]) -> Any:
        note = state["note"]
        return {"content": None if note is None else _b64(note)}

    def apply(_t: str, a: dict[str, Any]) -> Any:
        written = base64.b64decode(a["content"])
        state["note"] = written
        return {"target": NOTE, "after_sha256": hashlib.sha256(written).hexdigest()}

    def restore(_t: str, a: dict[str, Any]) -> Any:
        state["note"] = None if a["content"] is None else base64.b64decode(a["content"])
        return {}

    scripted = ScriptedHost(
        read=read, apply=apply, restore=restore, parent_exists=lambda _t, _a: {"exists": True}
    )
    intents = [{"type": "note.replace_owned_block", "path": DAILY, "block": "b", "content": "x"}]
    run = asyncio.run(run_pipeline(dry_run=False, fixture=plugins(intents)))
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run(run.id, run.pipeline, run.started_at)
        sinks = McpSinks(_apply_registry(), scripted)

        async def go() -> Any:
            planned = await plan_changes(run, sinks)
            return await apply_changes(run, planned, sinks, store, clock=lambda: START)

        result = asyncio.run(go())
        assert result.failure is None, result.failure
        store.save_run(result.record)
        assert state["note"] is not None
        fresh = McpSinks(_apply_registry(), scripted)  # as `horae undo` would build it
        undone = asyncio.run(undo_run(store, run.id, fresh, clock=lambda: START))
    assert undone.refused is None, undone.refused
    assert undone.complete
    assert state["note"] is None
