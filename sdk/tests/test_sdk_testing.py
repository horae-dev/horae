# SPDX-License-Identifier: Apache-2.0
"""The recorded-input harness replays recordings and checks outputs (#42 acceptance)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult, TextContent

from horae_sdk import Call, Plugin, PluginError
from horae_sdk.testing import (
    CI_ENVS,
    RECORD_ENV,
    RecordingRefused,
    Replayed,
    ReplayMismatch,
    areplay,
    areplay_all,
    check,
    load,
    read_result,
    replay,
    replay_all,
)
from sdk_helpers import HELLO, load_example

RECORDINGS = HELLO.parent / "recordings"


def write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_example_replays_its_recordings() -> None:
    plugin = load_example(HELLO).plugin
    replayed = replay_all(plugin, RECORDINGS)
    assert [path.name for path in replayed] == [
        "greet-ada.json",
        "greet-hostile-name.json",
        "shout-hostile-subject.json",
        "shout.json",
    ]


def test_a_changed_output_is_a_mismatch_with_a_diff(tmp_path: Path) -> None:
    plugin = load_example(HELLO).plugin
    document = json.loads((RECORDINGS / "greet-ada.json").read_text(encoding="utf-8"))
    document["expect"]["data"]["greeting"] = "Hi, Ada!"
    with pytest.raises(ReplayMismatch, match="the output differs") as raised:
        replay(plugin, write(tmp_path / "changed.json", document))
    assert '-    "greeting": "Hi, Ada!"' in str(raised.value)
    assert '+    "greeting": "Hello, Ada!"' in str(raised.value)
    del document["expect"]
    with pytest.raises(ReplayMismatch, match="no `expect`"):
        replay(plugin, write(tmp_path / "missing.json", document))


def test_record_mode_writes_the_current_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = load_example(HELLO).plugin
    path = write(tmp_path / "new.json", {"tool": "hello.shout", "input": {"a": "b"}})
    for name in CI_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(RECORD_ENV, "1")
    replay_all(plugin, tmp_path)
    assert load(path)["expect"] == {"data": {"a": "B"}, "intents": []}
    assert f"horae-sdk: recorded {path}" in capsys.readouterr().err
    assert path.stat().st_mode & 0o777 == 0o644
    assert [item.name for item in tmp_path.iterdir()] == ["new.json"]  # no temporary left
    monkeypatch.delenv(RECORD_ENV)
    replay(plugin, path)


@pytest.mark.parametrize("name", CI_ENVS)
def test_record_mode_is_refused_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    plugin = load_example(HELLO).plugin
    document = {"tool": "hello.shout", "input": {"a": "b"}}
    path = write(tmp_path / "new.json", document)
    for other in CI_ENVS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "true")
    with pytest.raises(RecordingRefused, match=f"refused in CI.*{name}"):
        replay(plugin, path, record=True)
    monkeypatch.setenv(RECORD_ENV, "1")
    with pytest.raises(RecordingRefused, match="refused in CI"):
        replay_all(plugin, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == document  # untouched


def test_record_mode_never_writes_through_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CI_ENVS:
        monkeypatch.delenv(name, raising=False)
    plugin = load_example(HELLO).plugin
    target = write(tmp_path / "target.json", {"tool": "hello.shout", "input": {}})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(RecordingRefused, match="symbolic link"):
        replay(plugin, link, record=True)
    assert "expect" not in load(target)


def test_expected_errors(tmp_path: Path) -> None:
    plugin = Plugin("err")

    @plugin.source("err.fail")
    def fail(call: Call) -> Any:
        raise PluginError("rate limited by upstream")

    replay(
        plugin,
        write(tmp_path / "e.json", {"tool": "err.fail", "expect": {"error": "rate limited"}}),
    )
    with pytest.raises(ReplayMismatch, match="expected a tool error"):
        replay(
            plugin, write(tmp_path / "e.json", {"tool": "err.fail", "expect": {"error": "timeout"}})
        )
    with pytest.raises(ReplayMismatch, match="expected a tool error"):
        check({"error": "x"}, Replayed({}, []))


def test_bad_recordings_and_directories(tmp_path: Path) -> None:
    plugin = load_example(HELLO).plugin
    with pytest.raises(ReplayMismatch, match="no recording"):
        replay_all(plugin, tmp_path)
    documents: list[Any] = [[], {"params": {}}, {"tool": "hello.greet", "extra": 1}]
    for document in documents:
        with pytest.raises(ValueError, match=r"recording|unknown"):
            load(write(tmp_path / "bad.json", document))
    with pytest.raises(ReplayMismatch, match="not a source"):
        replay(plugin, write(tmp_path / "bad.json", {"tool": "hello.nothing", "expect": {}}))


def test_results_are_read_as_the_host_reads_them() -> None:
    text = CallToolResult(content=[TextContent(text="[1]")])
    assert read_result(text) == Replayed([1], [])
    with pytest.raises(ReplayMismatch, match="single JSON text block"):
        read_result(CallToolResult(content=[]))
    with pytest.raises(ReplayMismatch, match="not a list"):
        read_result(
            CallToolResult(content=[], structured_content={}, _meta={"dev.horae/intents": {}})
        )
    assert (
        read_result(CallToolResult(content=[], is_error=True)).error == "the tool reported an error"
    )


def test_replaying_inside_a_running_event_loop() -> None:
    plugin = load_example(HELLO).plugin

    async def scenario() -> None:
        assert replay(plugin, RECORDINGS / "greet-ada.json").data == {"greeting": "Hello, Ada!"}
        assert (await areplay(plugin, RECORDINGS / "shout.json")).intents == []
        assert len(await areplay_all(plugin, RECORDINGS)) == 4
        assert len(replay_all(plugin, RECORDINGS)) == 4

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "expect",
    [
        {"data": {}, "intent": []},
        {"error": "x", "data": {}},
        {"error": "x", "intents": []},
        {"datta": {}},
        [],
    ],
)
def test_expect_keys_are_checked(expect: Any) -> None:
    with pytest.raises(ValueError, match="expect"):
        check(expect, Replayed({}, []))
