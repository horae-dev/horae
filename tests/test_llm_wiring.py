# SPDX-License-Identifier: Apache-2.0
"""The LLM backends wired into the runner, `open_ports`, `horae run` and `horae doctor` (#28).

No network: the anthropic backend's transport is an `httpx2.MockTransport` (see
`tests/test_llm_anthropic.py` for the recorded fixtures)."""

import asyncio
import hashlib
import json
import re
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from typer.testing import CliRunner

from horae.cli import app
from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, RunRecord
from horae.contracts.instance import ModelAlias
from horae.engine import LlmRequest
from horae.llm import UNVALIDATED, ModelBackends, Prompt
from horae.llm.backends import AnthropicBackend
from horae.runner import Ports, Runner, open_ports, uses_llm
from horae.secrets import SecretResolver
from horae.store import Store
from runner_fixtures import PIPELINES, Plugins, pipeline
from test_llm_anthropic import DECLARED_VALUE, SCHEMA, SYSTEM, Recorder, fixture

runner = CliRunner()
ANTHROPIC = {
    "backend": "anthropic",
    "model": "claude-fixture-1",
    "secret": "ANTHROPIC_API_KEY",
    "price": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
}


class Prompts:
    def prompt(self, request: LlmRequest) -> Prompt:
        return Prompt(SYSTEM, request.input, SCHEMA, "1.2.0")


def _instance(root: Path, models: Mapping[str, Any], names: tuple[str, ...]) -> Instance:
    secrets = root / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "ANTHROPIC_API_KEY").write_text(DECLARED_VALUE, encoding="utf-8")
    config = InstanceConfig.model_validate({"models": models, "secrets": {"dir": "secrets"}})
    return Instance(
        root=root,
        config=config,
        pipelines={name: pipeline(name) for name in names},
        lock=Lockfile(schema_version=1),
    )


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    return _instance(tmp_path / "instance", {"default": ANTHROPIC}, tuple(PIPELINES))


@pytest.fixture
def store(inst: Instance) -> Iterator[Store]:
    with Store.open(inst.store_path) as opened:
        yield opened


def _llm(inst: Instance, recorder: Recorder) -> ModelBackends:
    def factory(alias: str, spec: ModelAlias, secrets: SecretResolver) -> AnthropicBackend:
        return AnthropicBackend(alias, spec, secrets, transport=recorder.transport)

    return ModelBackends(
        inst.config.models, inst.secret_resolver({}), {"anthropic": factory}, Prompts(), UNVALIDATED
    )


def test_a_run_records_usage_and_cost_per_call(inst: Instance, store: Store) -> None:
    recorder = Recorder("success")
    plugins = Plugins()
    llm = _llm(inst, recorder)
    record = asyncio.run(Runner(inst, store, Ports(plugins, plugins, llm)).run("brief"))
    asyncio.run(llm.aclose())
    assert record.status == "ok"
    [call] = record.llm
    assert (call.backend, call.model, call.model_alias, call.skill_version) == (
        "anthropic",
        "claude-fixture-1",
        "default",
        "1.2.0",
    )
    assert (call.usage.tokens_in, call.usage.tokens_out, call.usage.priced) == (1200, 45, True)
    assert call.usage.usd == pytest.approx((1200 * 3 + 45 * 15) / 1e6)
    assert record.cost.usd == pytest.approx(call.usage.usd)
    sent = recorder.requests[0].content
    assert call.request["body_sha256"] == hashlib.sha256(sent).hexdigest()  # the exact body
    assert call.request["max_tokens"] == recorder.body()["max_tokens"]
    assert "Review PR 42" not in json.dumps(call.request)  # digests, not the payload
    assert call.response == {"items": ["Review PR 42"], "summary": "One review is waiting."}
    # the collected input reaches the model as data
    assert "Review PR 42" in recorder.body()["messages"][0]["content"][0]["text"]
    stored = store.get_run(record.id)
    assert stored is not None
    text = json.dumps(stored.model_dump(mode="json"))
    assert DECLARED_VALUE not in text
    assert RunRecord.model_validate(stored.model_dump(mode="json")) == stored


def test_a_failed_call_costs_what_it_reported(inst: Instance, store: Store) -> None:
    plugins = Plugins()
    llm = _llm(inst, Recorder("max_tokens"))
    record = asyncio.run(Runner(inst, store, Ports(plugins, plugins, llm)).run("brief"))
    asyncio.run(llm.aclose())
    assert record.status == "degraded"
    assert record.steps[1].error is not None
    assert record.steps[1].error.kind == "output_invalid"
    assert record.cost.tokens_out == 2000
    assert record.cost.usd == pytest.approx((1200 * 3 + 2000 * 15) / 1e6)


@pytest.mark.parametrize(
    "text",
    ['{"items": [], "n": 1e999}', '{"items": [], "items": []}', "[" * 100_000 + "]" * 100_000],
    ids=["inf", "duplicate", "deep"],
)
def test_a_rejected_output_is_charged(inst: Instance, store: Store, text: str) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        _, body = fixture("success")
        body["content"] = [{"type": "text", "text": text}]
        return httpx2.Response(200, json=body)

    plugins = Plugins()
    llm = _llm(inst, Recorder(answer))
    record = asyncio.run(Runner(inst, store, Ports(plugins, plugins, llm)).run("brief"))
    asyncio.run(llm.aclose())
    assert record.status == "degraded"
    assert record.steps[1].error is not None
    assert record.steps[1].error.kind == "output_invalid"
    out = max(45, min(-(-len(text.encode()) // 4), 2000))  # floored by the output's size
    assert (record.cost.tokens_in, record.cost.tokens_out) == (1200, out)
    assert record.cost.usd == pytest.approx((1200 * 3 + out * 15) / 1e6)


def test_uses_llm(inst: Instance, tmp_path: Path) -> None:
    assert uses_llm(inst)
    plain = _instance(tmp_path / "plain", {"default": ANTHROPIC}, ("collect", "partial"))
    assert not uses_llm(plain)


def test_open_ports_builds_the_backends_lazily(inst: Instance, tmp_path: Path) -> None:
    async def go() -> None:
        secrets = inst.secret_resolver({})
        async with open_ports(inst, tmp_path / "plugins", secrets=secrets) as live:
            assert isinstance(live.ports.llm, ModelBackends)
            assert live.ports.llm.models == inst.config.models
            assert secrets.resolved() == ()  # no key read before a call

    asyncio.run(go())


def test_open_ports_without_an_llm_step_has_no_backend(tmp_path: Path) -> None:
    plain = _instance(tmp_path / "plain", {"default": ANTHROPIC}, ("collect",))

    async def go() -> None:
        async with open_ports(plain, tmp_path / "plugins") as live:
            assert live.ports.llm is None

    asyncio.run(go())


def test_open_ports_keeps_a_given_backend(inst: Instance, tmp_path: Path) -> None:
    given = _llm(inst, Recorder("success"))

    async def go() -> None:
        async with open_ports(inst, tmp_path / "plugins", llm=given) as live:
            assert live.ports.llm is given

    asyncio.run(go())


def test_open_ports_closes_its_backends(
    inst: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    async def aclose(self: ModelBackends) -> None:
        closed.append(True)

    monkeypatch.setattr(ModelBackends, "aclose", aclose)

    async def go() -> None:
        async with open_ports(inst, tmp_path / "plugins"):
            pass

    asyncio.run(go())
    assert closed == [True]


# --- horae run and horae doctor ---------------------------------------------------------------


def _write(root: Path, models: Mapping[str, Any], steps: list[dict[str, Any]]) -> Path:
    (root / "pipelines").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "secrets" / "ANTHROPIC_API_KEY").write_text(DECLARED_VALUE, encoding="utf-8")
    config = {"models": models, "secrets": {"dir": "secrets"}}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    definition = {"name": "brief", "steps": steps}
    (root / "pipelines" / "brief.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    # A plugin that only ships the skill (no runtime: no process is started).
    permissions: dict[str, Any] = {"intents": []}
    lock = {
        "schema_version": 1,
        "plugins": {
            "pack": {
                "version": "0.1.0",
                "repo": "https://github.com/horae-dev/pack-fixture.git",
                "commit": "0" * 40,
                "permissions": permissions,
            }
        },
    }
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    manifest = {
        "name": "pack",
        "version": "0.1.0",
        "horae": ">=0.1,<0.2",
        "provides": {"skills": ["pack/brief"]},
        "permissions": permissions,
    }
    (root / "plugins" / "pack").mkdir(parents=True)
    (root / "plugins" / "pack" / "plugin.yaml").write_text(yaml.safe_dump(manifest), "utf-8")
    # Its skill (#27): no intents, so no reserved `intents` output property.
    skill = root / "plugins" / "pack" / "skills" / "pack" / "brief"
    skill.mkdir(parents=True)
    (skill / "skill.yaml").write_text(
        "name: pack/brief\nversion: 1.2.0\ndescription: Brief.\ninput_view: [items]\n", "utf-8"
    )
    (skill / "prompt.md").write_text(SYSTEM + "\n", "utf-8")
    output = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"summary": {"type": "string", "maxLength": 200}},
    }
    (skill / "output.schema.json").write_text(json.dumps(output), "utf-8")
    return root


LLM_STEP = {
    "llm": {"skill": "pack/brief", "model": "default", "budget": {"usd": 0.05, "tokens_out": 50}}
}


def test_horae_run_reaches_the_model_alias_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`horae run` goes through the real `open_ports`: the llm step is no longer
    `llm_unavailable`, and its skill is loaded (#27). The pipeline collects nothing, so the
    viewed input is `null` and the step stops at `input_invalid` before any request; a
    missing skill is `skill_invalid`. No network is used either way."""
    sent: list[httpx2.Request] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - never sent
        sent.append(request)
        return httpx2.Response(500)

    original = AnthropicBackend.__init__

    def patched(self: AnthropicBackend, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs, transport=httpx2.MockTransport(refuse))

    monkeypatch.setattr(AnthropicBackend, "__init__", patched)
    root = _write(tmp_path / "instance", {"default": ANTHROPIC}, [LLM_STEP])
    result = runner.invoke(app, ["run", "brief", "-i", str(root), "--json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 0, report
    assert report["run"]["status"] == "degraded"
    with Store.open(root / "state" / "horae.db") as store:
        record = store.get_run(report["run_id"])
    assert record is not None
    error = record.steps[0].error
    assert error is not None
    assert error.kind == "input_invalid"
    assert sent == []
    shutil.rmtree(root / "plugins" / "pack" / "skills")
    result = runner.invoke(app, ["run", "brief", "-i", str(root), "--json"])
    report = json.loads(result.stdout)
    with Store.open(root / "state" / "horae.db") as store:
        record = store.get_run(report["run_id"])
    assert record is not None
    error = record.steps[0].error
    assert error is not None
    assert error.kind == "skill_invalid"
    assert sent == []


def _doctor(root: Path) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["doctor", "-i", str(root), "--json"])
    return result.exit_code, json.loads(result.stdout)


def test_doctor_reports_an_unavailable_backend(tmp_path: Path) -> None:
    models = {"default": {"backend": "claude-cli", "model": "sonnet"}, "spare": ANTHROPIC}
    root = _write(tmp_path / "instance", models, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 1
    [problem] = report["problems"]
    assert problem["file"] == "horae.yaml"
    assert re.search(
        r"models\.default\.backend: backend 'claude-cli' is not available", problem["message"]
    )
    assert "available: anthropic" in problem["message"]


def test_doctor_accepts_an_anthropic_alias_with_its_secret(tmp_path: Path) -> None:
    unused = {"backend": "claude-cli", "model": "sonnet"}  # not used by a pipeline: not checked
    root = _write(tmp_path / "instance", {"default": ANTHROPIC, "unused": unused}, [LLM_STEP])
    assert _doctor(root) == (0, _doctor(root)[1])
    assert _doctor(root)[1]["ok"] is True


def test_doctor_reports_a_missing_alias_secret(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": ANTHROPIC}, [LLM_STEP])
    (root / "secrets" / "ANTHROPIC_API_KEY").unlink()
    code, report = _doctor(root)
    assert code == 1
    assert any("ANTHROPIC_API_KEY" in p["message"] for p in report["problems"])


UNPRICED = {key: value for key, value in ANTHROPIC.items() if key != "price"}


def test_doctor_warns_about_a_cloud_alias_without_a_price(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": UNPRICED}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 0  # a warning does not fail the check
    assert report["ok"] is True
    [warning] = report["problems"]
    assert warning["severity"] == "warning"
    assert warning["file"] == "horae.yaml"
    assert warning["message"].startswith("models.default.price: model alias 'default'")
    assert "has no price" in warning["message"]
    assert "usd budget does not bound" in warning["message"]
    text = runner.invoke(app, ["doctor", "-i", str(root)])
    assert text.exit_code == 0
    assert "horae.yaml: warning: models.default.price" in text.stdout
    assert "(1 warning)" in text.stdout


def test_doctor_warns_about_a_usd_ceiling_over_an_unpriced_alias(tmp_path: Path) -> None:
    # Decision 41 B: the dollars of an unpriced alias are unbounded by a usd ceiling.
    root = _write(tmp_path / "instance", {"default": UNPRICED}, [LLM_STEP])
    config = yaml.safe_load((root / "horae.yaml").read_text(encoding="utf-8"))
    config["budgets"] = {
        "daily": {"usd": 1.0},
        "pipelines": {"brief": {"daily": {"usd": 0.5, "tokens_out": 1000}}},
    }
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    code, report = _doctor(root)
    assert code == 0
    messages = [p["message"] for p in report["problems"] if p["severity"] == "warning"]
    assert [m for m in messages if "ceiling" in m] == [
        "budgets.daily.usd: pipeline 'brief' uses model alias 'default', which has no price: "
        "the usd ceiling does not bound its calls (only tokens_out does)",
        "budgets.pipelines.brief.daily.usd: pipeline 'brief' uses model alias 'default', "
        "which has no price: the usd ceiling does not bound its calls (only tokens_out does)",
    ]
    config["budgets"] = {"daily": {"tokens_out": 1000}}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    assert not any("ceiling" in p["message"] for p in _doctor(root)[1]["problems"])
    config["models"] = {"default": ANTHROPIC}
    config["budgets"] = {"daily": {"usd": 1.0}}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    assert _doctor(root)[1]["problems"] == []


def test_doctor_does_not_warn_about_an_unused_or_local_alias(tmp_path: Path) -> None:
    models = {"default": ANTHROPIC, "spare": UNPRICED}  # `spare` is used by no pipeline
    root = _write(tmp_path / "instance", models, [LLM_STEP])
    code, report = _doctor(root)
    assert (code, report["problems"]) == (0, [])


def test_doctor_warnings_do_not_hide_errors(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": UNPRICED}, [LLM_STEP])
    (root / "secrets" / "ANTHROPIC_API_KEY").unlink()
    code, report = _doctor(root)
    assert code == 1
    assert report["ok"] is False
    severities = sorted(p["severity"] for p in report["problems"])
    assert severities == ["error", "warning"]
    text = runner.invoke(app, ["doctor", "-i", str(root)])
    assert "1 problem found" in text.stdout


# --- openai-compatible and ollama (#30, decision 40) ----------------------------------------

OLLAMA = {"backend": "ollama", "model": "llama3", "base_url": "http://ollama:11434"}
OPENAI = {
    "backend": "openai-compatible",
    "model": "mistral-small",
    "base_url": "https://llm-gateway.internal.test/v1",
}


def test_doctor_accepts_an_unpriced_ollama_alias(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": OLLAMA}, [LLM_STEP])
    code, report = _doctor(root)
    assert (code, report["problems"]) == (0, [])  # local: no price warning


def test_doctor_warns_about_an_unpriced_openai_compatible_alias(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": OPENAI}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 0
    [warning] = report["problems"]
    assert warning["severity"] == "warning"
    assert "backend 'openai-compatible'" in warning["message"]


def test_doctor_reports_a_missing_openai_compatible_secret(tmp_path: Path) -> None:
    spec = {**OPENAI, "secret": "LLM_API_KEY", "price": ANTHROPIC["price"]}
    root = _write(tmp_path / "instance", {"default": spec}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 1
    assert any("LLM_API_KEY" in p["message"] for p in report["problems"])


def test_doctor_warns_about_a_key_sent_over_http(tmp_path: Path) -> None:
    spec = {**OLLAMA, "secret": "ANTHROPIC_API_KEY"}  # a secret file `_write` creates
    root = _write(tmp_path / "instance", {"default": spec}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 0  # a warning does not fail the check
    [warning] = report["problems"]
    assert warning["severity"] == "warning"
    assert warning["message"].startswith("models.default.secret: model alias 'default'")
    assert "in clear text over http://" in warning["message"]
    assert DECLARED_VALUE not in json.dumps(report)


def test_doctor_does_not_warn_about_a_key_over_https_or_no_key_over_http(
    tmp_path: Path,
) -> None:
    https = {**OPENAI, "secret": "ANTHROPIC_API_KEY", "price": ANTHROPIC["price"]}
    for name, spec in (("https", https), ("keyless", OLLAMA)):
        root = _write(tmp_path / name, {"default": spec}, [LLM_STEP])
        code, report = _doctor(root)
        assert (code, report["problems"]) == (0, [])
