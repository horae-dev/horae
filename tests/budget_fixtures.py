# SPDX-License-Identifier: Apache-2.0
"""A fake provider behind the real `ModelBackends`, and the child process of the daily
ceiling concurrency test (#33).

Nothing here opens a socket or calls a model."""

import asyncio
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from horae.contracts.instance import ModelAlias
from horae.engine.ports import LlmBackendName, LlmError, LlmRequest
from horae.llm import ModelBackends, Prompt
from horae.llm.base import Completion, CompletionResult, cost
from horae.secrets import SecretResolver
from runner_fixtures import SKILL, Plugins

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}
PRICE = {"input_per_mtok": 1.0, "output_per_mtok": 10.0}
STEP_BUDGET = {"usd": 0.05, "tokens_out": 2000}


class Prompts:
    def prompt(self, request: LlmRequest) -> Prompt:
        return Prompt("Rank the items.", request.input, SCHEMA, "1.0.0")


@dataclass
class FakeProvider:
    """Answers `{"items": [...]}`, reporting `tokens_out` output tokens (default: all of
    `max_tokens`) and 100 input tokens, priced with the alias' price."""

    spec: ModelAlias
    script: list[Any] = field(default_factory=list)
    """Per call: `None` (answer), an `LlmError` to raise, or a list of schema errors to
    answer with an invalid output (the validator returns them)."""
    tokens_out: int | None = None
    sent: list[Mapping[str, Any]] = field(default_factory=list)
    sent_file: Path | None = None
    go_file: Path | None = None
    name: LlmBackendName = "anthropic"

    def render(self, completion: Completion) -> dict[str, Any]:
        return {
            "model": completion.model,
            "max_tokens": completion.max_tokens,
            "system": completion.system,
            "input": completion.input,
            "repair": list(completion.repair),
        }

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.sent.append(payload)
        if self.sent_file is not None:
            self.sent_file.write_text("sent", encoding="utf-8")
        if self.go_file is not None:
            deadline = time.monotonic() + 30
            while not self.go_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        item = self.script.pop(0) if self.script else None
        out = payload["max_tokens"] if self.tokens_out is None else self.tokens_out
        usage = cost(self.spec.price, 100, out)
        if isinstance(item, LlmError):
            raise item
        output: Any = {"items": ["Review PR 42"]}
        if isinstance(item, list):
            output = {"items": "not a list", "errors": item}
        return CompletionResult(output=output, usage=usage)

    async def aclose(self) -> None:
        return None


def validate(output: Any, schema: Mapping[str, Any]) -> list[str]:
    errors = output.get("errors") if isinstance(output, dict) else None
    return list(errors) if isinstance(errors, list) else []


def backends(
    models: Mapping[str, ModelAlias], provider: FakeProvider | None = None, **kwargs: Any
) -> tuple[ModelBackends, list[FakeProvider]]:
    """A `ModelBackends` whose `anthropic` aliases are `FakeProvider`s (`provider` for
    every alias when given)."""
    made: list[FakeProvider] = []

    def factory(alias: str, spec: ModelAlias, secrets: SecretResolver) -> FakeProvider:
        fake = provider or FakeProvider(spec, **kwargs)
        made.append(fake)
        return fake

    llm = ModelBackends(
        models,
        SecretResolver({}, Path("/nonexistent"), {}),
        {"anthropic": factory},
        Prompts(),
        validate,
    )
    return llm, made


def write_instance(root: Path, names: tuple[str, ...], **config: Any) -> Path:
    """An instance whose pipelines `names` each collect `fixture.items` then call the LLM."""
    (root / "pipelines").mkdir(parents=True)
    data = {
        "timezone": "Europe/Paris",
        "models": {
            "default": {
                "backend": "anthropic",
                "model": "fixture-model",
                "secret": "ANTHROPIC_API_KEY",
                "price": PRICE,
            }
        },
        **config,
    }
    (root / "horae.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    for name in names:
        steps = [
            {"collect": ["fixture.items"]},
            {"llm": {"skill": SKILL, "model": "default", "budget": STEP_BUDGET}},
        ]
        (root / "pipelines" / f"{name}.yaml").write_text(
            yaml.safe_dump({"name": name, "steps": steps}), encoding="utf-8"
        )
    return root


def child_main(argv: list[str]) -> int:
    """`python tests/budget_fixtures.py <instance> <pipeline> <dir>`: run the pipeline once;
    its LLM call creates `<dir>/<pipeline>.sent` and waits for `<dir>/go`. Prints the run's
    status and step error kind as JSON."""
    from horae.config import load_instance
    from horae.runner import Ports, Runner
    from horae.store import Store

    root, name, where = Path(argv[0]), argv[1], Path(argv[2])
    loaded = load_instance(root)
    plugins = Plugins()
    llm, _ = backends(loaded.config.models, sent_file=where / f"{name}.sent", go_file=where / "go")
    with Store.open(loaded.store_path) as store:
        runner = Runner(loaded, store, Ports(plugins, plugins, llm=llm), heartbeat_s=0.05)
        record = asyncio.run(runner.run(name))
    error = record.steps[1].error
    print(json.dumps({"status": record.status, "kind": None if error is None else error.kind}))
    return 0


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))
