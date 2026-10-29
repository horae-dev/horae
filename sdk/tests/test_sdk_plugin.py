# SPDX-License-Identifier: Apache-2.0
"""`Plugin` speaks the host's contract (#42): calls, results, errors and sink operations."""

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from horae_sdk import (
    Applied,
    ApplyRequest,
    Call,
    Notifier,
    Output,
    Paths,
    Permissions,
    Plugin,
    PluginError,
    Sink,
    SinkError,
    Source,
    Transform,
    intents,
)
from horae_sdk.protocol import MAX_NOTE_BYTES
from horae_sdk.sinks import decode, encode
from horae_sdk.testing import Replayed, ReplayMismatch, call, operation


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def demo() -> Plugin:
    plugin = Plugin("demo", permissions=Permissions(intents=["notify.push"], read=["/srv"]))

    @plugin.source("demo.echo")
    async def echo(call: Call, word: str, times: int = 1) -> Output:
        """Echo the parameters, the input and the paths."""
        data = {"word": word * times, "params": call.params, "input": call.input}
        return Output({**data, "read": list(call.paths.read)}, [intents.notify_push(word)])

    @plugin.source("demo.list")
    def listing(call: Call) -> list[int]:
        return [1, 2, 3]

    @plugin.source("demo.fail")
    def fail(call: Call, kind: str = "plugin") -> Any:
        if kind == "plugin":
            raise PluginError("upstream said no")
        if kind == "nan":
            return {"x": float("nan")}
        if kind == "undeclared":
            return Output({}, [intents.note_create("x")])
        raise RuntimeError("secret detail")

    class Upper(Transform):
        ref = "demo.upper"

        def run(self, call: Call) -> Any:
            return str(call.input).upper()

    plugin.add(Upper())
    return plugin


def test_calls_carry_params_input_and_locked_paths() -> None:
    plugin = demo()
    replayed = run(
        call(
            plugin,
            "demo.echo",
            {"word": "ab", "times": 2},
            input={"x": 1},
            paths={"read": ["/srv"]},
        )
    )
    assert replayed.data == {
        "word": "abab",
        "params": {"word": "ab", "times": 2},
        "input": {"x": 1},
        "read": ["/srv"],
    }
    assert replayed.intents == [{"type": "notify.push", "body": "ab"}]
    assert run(call(plugin, "demo.list")) == Replayed([1, 2, 3], [])
    assert run(call(plugin, "demo.upper", input="hi")).data == "HI"


def test_errors_are_tool_errors_without_exception_text() -> None:
    plugin = demo()
    assert run(call(plugin, "demo.fail")).error == "upstream said no"
    crashed = run(call(plugin, "demo.fail", {"kind": "boom"})).error
    assert crashed is not None
    assert "RuntimeError" in crashed
    assert "secret detail" not in crashed
    assert "not JSON" in (run(call(plugin, "demo.fail", {"kind": "nan"})).error or "")
    undeclared = run(call(plugin, "demo.fail", {"kind": "undeclared"})).error or ""
    assert "note.create" in undeclared
    assert "permissions.intents" in undeclared
    assert run(call(plugin, "demo.echo", {})).error is not None  # `word` is required


def test_the_manifest_lists_what_is_registered() -> None:
    assert demo().provides() == {
        "sources": ["demo.echo", "demo.list", "demo.fail"],
        "transforms": ["demo.upper"],
    }


def test_tool_definitions_are_checked() -> None:
    plugin = Plugin("bad")
    with pytest.raises(TypeError, match="first parameter"):
        plugin.source("bad.none")(lambda: {})
    with pytest.raises(TypeError, match="keyword"):
        plugin.source("bad.star")(lambda call, *rest: {})
    with pytest.raises(TypeError, match="reserved"):
        plugin.source("bad.ctx")(lambda call, horae_sdk_context=1: {})
    with pytest.raises(TypeError, match="needs a ref"):
        Source()
    with pytest.raises(TypeError, match="give a function"):
        Source("bad.norun")


class Notes(Sink):
    """A sink over a dictionary, for the operation contract."""

    def __init__(self) -> None:
        super().__init__(["notes.write_note"])
        self.files: dict[str, bytes] = {}

    def identity(self, paths: Paths) -> str:
        return f"memory:{','.join(paths.write)}"

    async def apply(self, request: ApplyRequest) -> Applied:
        assert request.content is not None
        before = self.files.get(request.intent["path"])
        digest = None if before is None else hashlib.sha256(before).hexdigest()
        if digest != request.expected_before_sha256:
            raise SinkError("target_changed", "changed")
        self.files[request.intent["path"]] = request.content
        return Applied.wrote(request.intent["path"], request.content, run=request.run_id)

    def read(self, paths: Paths, path: str) -> bytes | None:
        return self.files.get(path)

    def restore(
        self, paths: Paths, path: str, content: bytes | None, expected_sha256: str | None
    ) -> None:
        if content is None:
            self.files.pop(path, None)
        else:
            self.files[path] = content

    def parent_exists(self, paths: Paths, path: str) -> bool:
        return "/" not in path


def test_sink_operations_follow_the_host_contract() -> None:
    plugin = Plugin("notes", permissions=Permissions(intents=["note.create"]))
    sink = Notes()
    plugin.add(sink)
    paths = {"read": [], "write": ["/vault"]}
    content = base64.b64encode(b"# x\n").decode()
    assert run(operation(plugin, "notes.identity", {"paths": paths})) == {
        "identity": "memory:/vault"
    }
    intent = {"type": "note.create", "path": "x.md", "content": "# x\n"}
    arguments = {"paths": paths, "intent": intent, "content": content, "run_id": "r1", "index": 0}
    applied = run(operation(plugin, "notes.apply", {**arguments, "expected_before_sha256": None}))
    assert applied == {
        "target": "x.md",
        "after_sha256": hashlib.sha256(b"# x\n").hexdigest(),
        "details": {"run": "r1"},
    }
    again = run(operation(plugin, "notes.apply", arguments))
    assert again == {"error": {"kind": "target_changed", "message": "changed"}}
    assert run(operation(plugin, "notes.read", {"paths": paths, "path": "x.md"})) == {
        "content": content
    }
    assert run(operation(plugin, "notes.parent_exists", {"paths": paths, "path": "a/x.md"})) == {
        "exists": False
    }
    assert (
        run(operation(plugin, "notes.restore", {"paths": paths, "path": "x.md", "content": None}))
        == {}
    )
    assert run(operation(plugin, "notes.read", {"paths": paths, "path": "x.md"})) == {
        "content": None
    }
    other = {"paths": paths, "intent": {"type": "notify.push", "body": "x"}}
    assert run(operation(plugin, "notes.apply", other))["error"]["kind"] == "not_supported"
    bad = run(operation(plugin, "notes.apply", {**arguments, "content": "not base64!"}))
    assert bad["error"]["kind"] == "content_invalid"
    assert plugin.provides() == {"sinks": ["notes.write_note"]}
    with pytest.raises(ReplayMismatch, match="not a source"):
        run(call(plugin, "notes.read"))
    with pytest.raises(ReplayMismatch, match="sink namespace"):
        run(operation(plugin, "other.read", {}))
    with pytest.raises(ValueError, match="already has a sink"):
        plugin.add(Notes())


class Push(Notifier):
    def identity(self, paths: Paths) -> str:
        return "push:test"

    def apply(self, request: ApplyRequest) -> Any:
        if request.intent.get("body") == "broken":
            return "not applied"
        if request.intent.get("body") == "crash":
            raise OSError("socket detail")
        return Applied("push:test", details={"key": request.dedupe_key})


def test_a_notifier_answers_not_supported_for_notes_and_reports_failures() -> None:
    plugin = Plugin("push", permissions=Permissions(intents=["notify.push"]))
    plugin.add(Push(["push.send"]))
    paths: dict[str, list[str]] = {"read": [], "write": []}
    for op, arguments in (
        ("read", {"path": "x.md"}),
        ("parent_exists", {"path": "x.md"}),
        ("restore", {"path": "x.md"}),
    ):
        result = run(operation(plugin, f"push.{op}", {"paths": paths, **arguments}))
        assert result["error"]["kind"] == "not_supported"
    sent = run(
        operation(
            plugin,
            "push.apply",
            {"paths": paths, "intent": {"type": "notify.push", "body": "hi"}, "dedupe_key": "k"},
        )
    )
    assert sent == {"target": "push:test", "after_sha256": None, "details": {"key": "k"}}
    broken = run(
        operation(
            plugin,
            "push.apply",
            {"paths": paths, "intent": {"type": "notify.push", "body": "broken"}},
        )
    )
    assert broken["error"]["kind"] == "sink_failed"
    crash = run(
        operation(
            plugin,
            "push.apply",
            {"paths": paths, "intent": {"type": "notify.push", "body": "crash"}},
        )
    )
    assert crash["error"] == {"kind": "sink_failed", "message": "OSError"}
    assert plugin.provides() == {"notifiers": ["push.send"]}


def test_sink_definitions_and_errors_are_checked() -> None:
    for tools in ([], ["nodot"], ["a.x", "b.y"]):
        with pytest.raises(ValueError, match="sink"):
            Push(tools)
    with pytest.raises(ValueError, match="kind"):
        SinkError("Bad Kind", "x")
    plugin = Plugin("clash")
    plugin.source("push.read")(lambda call: {})
    with pytest.raises(ValueError, match="sink operation"):
        plugin.add(Push(["push.send"]))


def test_note_bytes_are_capped_both_ways() -> None:
    with pytest.raises(SinkError, match="larger"):
        encode(b"x" * (MAX_NOTE_BYTES + 1))
    with pytest.raises(SinkError, match="larger"):
        decode("A" * (4 * MAX_NOTE_BYTES))
    with pytest.raises(SinkError, match="base64"):
        decode(1)
    assert encode(None) is None
    assert decode(None) is None


def test_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (vault / "a" / "link").symlink_to(tmp_path / "out")
    paths = Paths.from_wire({"read": [str(tmp_path / "ro")], "write": [str(vault), "relative"]})
    assert paths.write == (str(vault),)
    assert paths.allowed(str(vault / "a" / "x.md"), write=True) == str(
        (vault / "a" / "x.md").resolve()
    )
    assert paths.allowed(str(vault / "a" / "link" / "x.md")) is None  # a link out of the vault
    assert paths.allowed(str(tmp_path / "ro" / "x")) is not None
    assert paths.allowed(str(tmp_path / "ro" / "x"), write=True) is None
    assert paths.allowed("x.md") is None
    assert Paths.from_wire("junk") == Paths()
    assert paths.to_wire() == {"read": [str(tmp_path / "ro")], "write": [str(vault)]}
