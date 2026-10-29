# SPDX-License-Identifier: Apache-2.0
"""The core's real plugin host runs SDK plugins over stdio (#42 acceptance).

`hello` (the minimal example) is spawned as a subprocess by `PluginHost`, called as a source
and as a transform, and its output and intents pass the core's validation. `tasks-file` is
driven through `McpSinks`, the core's sink adapter, with a task title holding every Tasks
signifier: the line it writes holds none of them.
"""

import asyncio
from pathlib import Path

from horae.apply import SinkCall
from horae.contracts import PluginManifest, TaskCreate
from horae.intents import validate_plugin_intents
from horae.plugins.host import McpSinks
from sdk_helpers import (
    DUE,
    HELLO,
    HOSTILE,
    REMINDER,
    TASKS_FILE,
    host_for,
    load_example,
    pinned,
    signifiers_in,
)


def test_the_minimal_plugin_is_under_50_lines() -> None:
    lines = HELLO.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 50, f"{HELLO.name} has {len(lines)} lines"


def test_the_core_host_spawns_the_example_over_stdio(tmp_path: Path) -> None:
    manifest = pinned(load_example(HELLO).plugin.manifest(command=["python3"]), HELLO)

    async def scenario() -> None:
        async with host_for(tmp_path, manifest) as host:
            greeted = await host.call("hello.greet", {"name": "Ada"}, None, timeout_s=60)
            shouted = await host.call("hello.shout", {}, greeted.data, timeout_s=60)
            mail = {"subject": f"{DUE} 2020-01-01 #urgent"}  # untrusted, from a mail
            replied = await host.call("hello.shout", {}, mail, timeout_s=60)
        (reply,) = validate_plugin_intents(replied.intents, PluginManifest.model_validate(manifest))
        assert isinstance(reply, TaskCreate)
        assert reply.title == "Reply to &#x1F4C5; 2020-01-01 \\#urgent"
        assert signifiers_in(reply.title) == []
        assert greeted.data == {"greeting": "Hello, Ada!"}
        (task,) = validate_plugin_intents(greeted.intents, PluginManifest.model_validate(manifest))
        assert isinstance(task, TaskCreate)
        assert (task.title, task.fingerprint) == ("Greet Ada back", "hello:Ada")
        assert shouted.data == {"greeting": "HELLO, ADA!"}
        assert shouted.intents == []
        assert greeted.violations == ()

    asyncio.run(scenario())


def test_a_tasks_sink_written_with_the_sdk_escapes_titles_end_to_end(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    manifest = pinned(load_example(TASKS_FILE).plugin.manifest(command=["python3"]), TASKS_FILE)
    fields = {"title": HOSTILE, "fingerprint": "mail:1", "due": "2026-10-22", "time": "19:00"}
    intent = TaskCreate.model_validate({"type": "task.create", **fields})

    async def scenario() -> None:
        async with host_for(tmp_path, manifest, write=[str(vault)]) as host:
            sinks = McpSinks(host.registry, host)
            sink = sinks.mcp_sink_for("tasks-file.append")
            assert sink is not None
            await sinks.prepare(["tasks-file.append"])
            assert sink.identity == f"mcp:tasks-file:tasks-file:{vault.resolve()}"
            call = SinkCall(
                run_id="r1", index=0, tool="tasks-file.append", params={}, intent=intent
            )
            applied = await sink.apply(call, expected_before_sha256=None)
        assert applied.target == "Tasks.md"

    asyncio.run(scenario())
    (line,) = (vault / "Tasks.md").read_text(encoding="utf-8").splitlines()
    assert line.startswith("- [ ] Pay &#x1F4C5; 2020-01-01 ")
    assert signifiers_in(line) == [DUE, REMINDER]
    assert line.endswith(f" {DUE} 2026-10-22 {REMINDER} 19:00")
