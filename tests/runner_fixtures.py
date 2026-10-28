# SPDX-License-Identifier: Apache-2.0
"""Synthetic instances and ports for the runner tests (and the child process of the lock test).

Nothing here starts a plugin, opens a socket or calls a model."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, Pipeline
from horae.engine import LlmError, ToolCallError, ToolResult
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response

SKILL = "pack/brief"
BUDGET = {"usd": 0.05, "tokens_out": 2000}

PIPELINES: dict[str, list[dict[str, Any]]] = {
    "collect": [{"collect": ["fixture.items"]}],
    "slow": [{"collect": ["fixture.slow"]}, {"notify": ["ntfy.push"]}],
    "slow-transform": [{"collect": ["fixture.items"]}, {"transform": ["fixture.slow"]}],
    "partial": [{"collect": ["fixture.items", "fixture.broken"]}],
    "broken": [{"collect": ["fixture.items"]}, {"transform": ["fixture.broken_transform"]}],
    "brief": [
        {"collect": ["fixture.items"]},
        {"llm": {"skill": SKILL, "model": "default", "budget": BUDGET}},
    ],
}


def pipeline(name: str) -> Pipeline:
    return Pipeline.model_validate({"name": name, "steps": PIPELINES[name]})


def write_instance(root: Path, **config: Any) -> Path:
    """An instance directory with every pipeline of `PIPELINES` (no lockfile: no plugin)."""
    (root / "pipelines").mkdir(parents=True)
    data = {
        "timezone": "Europe/Paris",
        "models": {"default": {"backend": "claude-cli", "model": "sonnet"}},
        **config,
    }
    (root / "horae.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    for name, steps in PIPELINES.items():
        (root / "pipelines" / f"{name}.yaml").write_text(
            yaml.safe_dump({"name": name, "steps": steps}), encoding="utf-8"
        )
    return root


def instance(root: Path, **config: Any) -> Instance:
    """The same instance, built in memory."""
    data = {"models": {"default": {"backend": "claude-cli", "model": "sonnet"}}, **config}
    return Instance(
        root=root,
        config=InstanceConfig.model_validate(data),
        pipelines={name: pipeline(name) for name in PIPELINES},
        lock=Lockfile(schema_version=1),
    )


def _manifest(name: str, intents: tuple[str, ...] = (), **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0.1,<0.2",
        "provides": provides,
        "permissions": {"intents": list(intents)},
    }


def _broken(params: dict[str, Any], input: Any) -> Any:
    raise ToolCallError("tool_error", "synthetic failure")


class Plugins(FixturePlugins):
    """The fixture plugins; `fixture.slow` waits until `release` is set (or `release_file`
    exists), after setting `started` (and creating `ready_file`)."""

    def __init__(self, ready_file: Path | None = None, release_file: Path | None = None) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ready_file = ready_file
        self.release_file = release_file
        self.add(
            _manifest(
                "fixture",
                sources=["fixture.items", "fixture.slow", "fixture.broken"],
                transforms=["fixture.broken_transform", "fixture.slow"],
            ),
            {
                "fixture.items": lambda p, i: [{"title": "Review PR 42"}],
                "fixture.broken": _broken,
                "fixture.broken_transform": _broken,
            },
        )
        self.add(_manifest("pack", skills=[SKILL]))
        self.add(_manifest("ntfy", ("notify.push",), notifiers=["ntfy.push"]))

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        if ref != "fixture.slow":
            return await super().call(ref, params, input)
        self.started.set()
        if self.ready_file is not None:
            self.ready_file.write_text("ready", encoding="utf-8")
        while not self.release.is_set():
            if self.release_file is not None and self.release_file.exists():
                break
            await asyncio.sleep(0.01)
        return ToolResult(data=[])


def brief_llm(*, fail: bool = False) -> FakeLlm:
    if fail:
        return FakeLlm([LlmError("backend_error", "synthetic outage")])
    return FakeLlm([llm_response({"items": ["Review PR 42"]})])


def child_main(argv: list[str]) -> int:
    """`python tests/runner_fixtures.py <instance> <pipeline> [<ready> <release>]`.

    Runs one pipeline; exit 0 on success, 3 when the pipeline is locked (the message on
    stderr), and prints the record's id and status as JSON on stdout."""
    from horae.config import load_instance
    from horae.runner import PipelineLocked, Ports, Runner
    from horae.store import Store

    root, name = Path(argv[0]), argv[1]
    ready = Path(argv[2]) if len(argv) > 2 else None
    release = Path(argv[3]) if len(argv) > 3 else None
    loaded = load_instance(root)
    plugins = Plugins(ready, release)
    with Store.open(loaded.store_path) as store:
        runner = Runner(loaded, store, Ports(plugins, plugins), heartbeat_s=0.05)
        try:
            record = asyncio.run(runner.run(name))
        except PipelineLocked as locked:
            print(str(locked), file=sys.stderr)
            return 3
    print(json.dumps({"id": record.id, "status": record.status}))
    return 0


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))
