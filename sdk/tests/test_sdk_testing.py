# SPDX-License-Identifier: Apache-2.0
"""The recorded-input harness replays recordings and checks outputs (#42 acceptance)."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult, TextContent

from horae_sdk import Call, Plugin, PluginError
from horae_sdk.testing import (
    RECORD_ENV,
    Replayed,
    ReplayMismatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = load_example(HELLO).plugin
    path = write(tmp_path / "new.json", {"tool": "hello.shout", "input": {"a": "b"}})
    monkeypatch.setenv(RECORD_ENV, "1")
    replay_all(plugin, tmp_path)
    assert load(path)["expect"] == {"data": {"a": "B"}, "intents": []}
    monkeypatch.delenv(RECORD_ENV)
    replay(plugin, path)


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
