# SPDX-License-Identifier: Apache-2.0
"""The plugin host against real processes: the probe (SDK server) and raw fixture plugins.

Timeouts here are generous (CI machines are slow); nothing waits on a fixed sleep to observe a
result: every wait is bounded polling for a condition. Stderr and stdout are separate pipes, so
a test that checks what a call wrote to stderr waits for a sentinel line (`_drain`) instead of
relying on the host's quiet-window heuristic (decision 35 F).
"""

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from horae.contracts import Pipeline, PluginManifest
from horae.contracts.instance import PluginDefaults
from horae.engine import execute, make_context
from horae.plugins.host import PluginCallError, PluginHost
from horae.plugins.host.capture import HEAD_BYTES, TAIL_BYTES
from horae.plugins.host.env import BASE_KEYS, BASE_PATH
from horae.secrets import SecretResolver
from plugin_fixtures import (
    OTHER_SECRET,
    PROBE_TOKEN,
    host,
    lock_for,
    probe_manifest,
    raw_manifest,
    registry,
    secrets_dir,
)

T0 = datetime.datetime(2026, 10, 28, 6, 0, tzinfo=datetime.UTC)
_PLATFORM_ADDED = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
"""Variables the macOS interpreter adds to its own environment (Linux reads /proc)."""
REDACTED_PROBE = "[REDACTED:PROBE_TOKEN]"


def _run[T](coro: Any) -> Any:
    return asyncio.run(coro)


async def _call(h: PluginHost, ref: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
    return await h.call(ref, params or {}, kw.pop("input", None), **kw)


async def _error(h: PluginHost, ref: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
    with pytest.raises(PluginCallError) as info:
        await _call(h, ref, params, **kw)
    return info.value


async def _drain(h: PluginHost, plugin: str, seen: str | None, sentinel: str) -> str:
    """`seen` plus whatever `plugin` writes to stderr next, until `sentinel` shows up."""
    text = seen or ""
    deadline = time.monotonic() + 10
    while sentinel not in text and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        process = h._entries[plugin].process
        if process is not None:
            text += process.capture.take(h.redactor()) or ""
    assert sentinel in text, text
    return text


def _gone(pid: int, within: float = 10.0) -> bool:
    """Whether process `pid` disappears within `within` seconds (bounded polling)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover (pid reused by another user)
            return True
        time.sleep(0.02)
    return False


# --- environment -----------------------------------------------------------------------------


def test_the_plugin_sees_exactly_the_base_env_and_its_declared_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "AWS_SECRET_ACCESS_KEY": "host-sentinel-aws",
        "HORAE_X": "host-sentinel-horae",
        "PYTHONPATH": "/host/sentinel/pythonpath",
        "LD_PRELOAD": "/host/sentinel/preload.so",
        "OTHER_SECRET": "host-sentinel-env",
    }.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    resolver = SecretResolver(
        {}, secrets_dir(tmp_path, PROBE_TOKEN=PROBE_TOKEN, OTHER_SECRET=OTHER_SECRET)
    )
    resolver.resolve("OTHER_SECRET")  # an instance secret the probe does not declare

    async def scenario() -> tuple[Any, Path]:
        async with host(tmp_path, resolver=resolver, timezone="Europe/Paris") as h:
            result = await _call(h, "probe.env")
            assert h._run_dir is not None
            return result, h._run_dir

    result, run_dir = _run(scenario())
    env, digests = result.data["env"], result.data["sha256"]
    assert set(env) == BASE_KEYS | {"PROBE_TOKEN"} | _PLATFORM_ADDED
    assert env["PATH"] == BASE_PATH
    assert env["LANG"] == "C.UTF-8"
    assert env["TZ"] == "Europe/Paris"
    for key in ("HOME", "TMPDIR"):
        assert Path(env[key]).is_relative_to(tmp_path / "work")
    assert env["HOME"] != str(Path.home())
    assert Path(result.data["cwd"]).resolve().is_relative_to((tmp_path / "work").resolve())
    assert result.data["cwd_entries"] == []
    assert result.data["umask"] == 0o077
    # The secret's value is right (its digest), and redacted from the returned data.
    assert digests["PROBE_TOKEN"] == hashlib.sha256(PROBE_TOKEN.encode()).hexdigest()
    assert env["PROBE_TOKEN"] == REDACTED_PROBE
    assert [v.kind for v in result.violations] == ["secret_in_output"]
    # Horae's own environment is untouched, and the run's directories are removed.
    assert dict(os.environ) == before
    assert PROBE_TOKEN not in os.environ.values()
    assert not run_dir.exists()


@pytest.mark.parametrize("name", ["NODE_OPTIONS", "LANG", "TZ", "HTTPS_PROXY", "SSL_CERT_FILE"])
def test_a_secret_on_the_deny_list_is_refused_before_spawn(tmp_path: Path, name: str) -> None:
    manifest = probe_manifest(permissions={"secrets": [name]})
    resolver = SecretResolver({}, secrets_dir(tmp_path, **{name: "x"}))

    async def scenario() -> None:
        async with host(tmp_path, manifest, resolver=resolver) as h:
            error = await _error(h, "probe.env")
            assert error.kind == "env_unsafe"
            assert name in error.message
            assert (await _error(h, "probe.env")).kind == "plugin_unavailable"

    _run(scenario())
    # Resolved when the host opened, for redaction only (decision 35 B); never passed on.
    assert [secret.name for secret in resolver.resolved()] == [name]


def test_a_missing_secret_fails_the_call_before_spawn(tmp_path: Path) -> None:
    resolver = SecretResolver({}, secrets_dir(tmp_path))

    async def scenario() -> None:
        async with host(tmp_path, resolver=resolver) as h:
            error = await _error(h, "probe.env")
            assert error.kind == "secret_missing"
            assert "PROBE_TOKEN" in error.message

    _run(scenario())


def test_no_shell_arguments_are_passed_verbatim(tmp_path: Path) -> None:
    hostile = ["$(touch pwned)", "; touch pwned", "| cat", "`id`", "a b"]
    manifest = probe_manifest()
    manifest["runtime"]["command"] += hostile

    async def scenario() -> Any:
        async with host(tmp_path, manifest) as h:
            return (await _call(h, "probe.env")).data

    data = _run(scenario())
    assert data["argv"] == hostile
    assert not list(tmp_path.rglob("pwned"))


def test_a_shell_command_line_is_not_run(tmp_path: Path) -> None:
    manifest = probe_manifest()
    manifest["runtime"]["command"] = [f"{sys.executable} -c 'print(1)'"]

    async def scenario() -> None:
        async with host(tmp_path, manifest) as h:
            assert (await _error(h, "probe.env")).kind == "spawn_failed"

    _run(scenario())


# --- timeouts, crashes, restarts -------------------------------------------------------------


def test_a_call_times_out_kills_the_process_and_the_next_call_restarts_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with host(tmp_path) as h:
            first = (await _call(h, "probe.env")).data["pid"]
            started = time.monotonic()
            error = await _error(h, "probe.sleep", {"seconds": 60}, timeout_s=1.0)
            assert error.kind == "timeout"
            assert "1 s" in error.message
            assert time.monotonic() - started < 30
            assert _gone(first)
            second = (await _call(h, "probe.env")).data["pid"]
            assert second != first

    _run(scenario())


def test_the_plugin_call_timeout_applies_without_a_pipeline_timeout(tmp_path: Path) -> None:
    def timeouts(name: str) -> PluginDefaults:
        assert name == "probe"
        return PluginDefaults(call_timeout_s=1.0)

    async def scenario() -> None:
        async with host(tmp_path, timeouts=timeouts) as h:
            assert (await _error(h, "probe.sleep", {"seconds": 60})).kind == "timeout"

    _run(scenario())


def test_a_crash_is_reported_restarted_once_then_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with host(tmp_path) as h:
            error = await _error(h, "probe.crash")
            assert error.kind == "plugin_crashed"
            assert "exited with code 3" in error.message
            assert error.stderr is not None
            assert "crashing now" in error.stderr
            assert (await _call(h, "probe.env")).data["pid"] > 0  # the one restart
            assert (await _error(h, "probe.crash", {"code": 4})).kind == "plugin_crashed"
            unavailable = await _error(h, "probe.env")
            assert unavailable.kind == "plugin_unavailable"
            assert "one restart" in unavailable.message

    _run(scenario())


def test_the_whole_process_group_is_stopped_with_the_host(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        async with host(tmp_path) as h:
            env = (await _call(h, "probe.env")).data
            assert env["pgid"] == env["pid"]  # its own process group
            child = (await _call(h, "probe.child")).data["pid"]
            return env["pid"], child

    leader, child = _run(scenario())
    assert _gone(leader)
    assert _gone(child)


def test_calls_after_the_host_closed_fail_cleanly(tmp_path: Path) -> None:
    h = host(tmp_path)

    async def scenario() -> None:
        assert (await _error(h, "probe.env")).kind == "host_error"

    _run(scenario())


# --- stderr and redaction --------------------------------------------------------------------


def _parts(*items: tuple[str, int]) -> dict[str, Any]:
    return {"parts": [{"text": text, "times": times} for text, times in items]}


def test_stderr_is_captured_per_call_and_redacted_in_every_encoding(tmp_path: Path) -> None:
    resolver = SecretResolver(
        {}, secrets_dir(tmp_path, PROBE_TOKEN=PROBE_TOKEN, OTHER_SECRET=OTHER_SECRET)
    )
    resolver.resolve("OTHER_SECRET")
    forms = [
        PROBE_TOKEN,
        base64.b64encode(PROBE_TOKEN.encode()).decode(),
        base64.urlsafe_b64encode(PROBE_TOKEN.encode()).decode().rstrip("="),
        quote(PROBE_TOKEN, safe=""),
        json.dumps(PROBE_TOKEN)[1:-1],
        OTHER_SECRET,  # resolved by the instance, not declared by the probe
    ]

    async def scenario() -> tuple[str, str]:
        async with host(tmp_path, resolver=resolver) as h:
            text = "".join(f"form {form} end\n" for form in forms) + "END-1\n"
            first = await _call(h, "probe.stderr", _parts((text, 1)))
            first_text = await _drain(h, "probe", first.stderr, "END-1")
            second = await _call(h, "probe.stderr", _parts(("second call\nEND-2\n", 1)))
            return first_text, await _drain(h, "probe", second.stderr, "END-2")

    first, second = _run(scenario())
    for form in forms:
        assert form not in first
    assert first.count("[REDACTED:PROBE_TOKEN]") == 5
    assert "[REDACTED:OTHER_SECRET]" in first
    assert second == "second call\nEND-2\n"  # each call gets its own window


def test_a_megabyte_of_stderr_is_capped_and_secrets_at_the_cuts_never_leak(
    tmp_path: Path,
) -> None:
    secret = PROBE_TOKEN
    # Put a secret across the head's cut and one across the tail's.
    payload = _parts(
        ("a", HEAD_BYTES - 10),
        (secret, 1),
        ("b", 1024 * 1024),
        (secret, 1),
        ("c", TAIL_BYTES - 10),
    )
    # The probe exits once it has written everything, so the capture is complete when the
    # call fails (the stderr pipe is drained to its end), with no timing involved.
    payload["exit_code"] = 9

    async def scenario() -> Any:
        async with host(tmp_path) as h:
            return await _error(h, "probe.stderr", payload)

    error = _run(scenario())
    assert error.kind == "plugin_crashed"
    stderr = error.stderr
    assert stderr is not None
    assert len(stderr) <= 65536
    assert "bytes elided" in stderr
    assert stderr.count("[REDACTED:PROBE_TOKEN]") == 2
    rest = stderr.replace("[REDACTED:PROBE_TOKEN]", "")
    for size in range(4, len(secret) + 1):  # no prefix, suffix or middle part of the secret
        for start in range(len(secret) - size + 1):
            assert secret[start : start + size] not in rest


def test_log_notifications_are_captured_and_redacted(tmp_path: Path) -> None:
    async def scenario() -> Any:
        async with host(tmp_path) as h:
            return await _call(h, "probe.log", {"message": f"token is {PROBE_TOKEN}"})

    result = _run(scenario())
    assert result.stderr is not None
    assert "token is [REDACTED:PROBE_TOKEN]" in result.stderr


def test_a_tool_error_message_is_redacted(tmp_path: Path) -> None:
    async def scenario() -> Any:
        async with host(tmp_path) as h:
            return await _error(h, "probe.fail", {"message": f"bad {PROBE_TOKEN}"})

    error = _run(scenario())
    assert error.kind == "tool_error"
    assert PROBE_TOKEN not in error.message
    assert "[REDACTED:PROBE_TOKEN]" in error.message


def test_a_secret_in_the_output_or_intents_is_redacted_and_flagged(tmp_path: Path) -> None:
    push = {"type": "notify.push", "body": f"leak {PROBE_TOKEN}"}

    async def scenario() -> tuple[Any, Any]:
        async with host(tmp_path) as h:
            echoed = await _call(h, "probe.echo", {"reveal": "PROBE_TOKEN"})
            proposed = await _call(h, "probe.intents", {"proposed": [push], "data": 1})
            return echoed, proposed

    echoed, proposed = _run(scenario())
    assert echoed.data["revealed"] == "[REDACTED:PROBE_TOKEN]"
    assert echoed.violations[0].kind == "secret_in_output"
    assert "PROBE_TOKEN" in echoed.violations[0].detail
    assert PROBE_TOKEN not in echoed.violations[0].detail
    assert proposed.intents == [{"type": "notify.push", "body": "leak [REDACTED:PROBE_TOKEN]"}]
    assert "intents" in proposed.violations[0].detail


# --- results ---------------------------------------------------------------------------------


def test_input_travels_in_meta_and_text_json_is_accepted(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, Any]:
        async with host(tmp_path) as h:
            echoed = await _call(h, "probe.echo", {"value": 7}, input={"items": [1]})
            text = await _call(h, "probe.text", {"body": '{"a": [1, 2]}'})
            return echoed, text

    echoed, text = _run(scenario())
    assert echoed.data == {"value": 7, "input": {"items": [1]}, "revealed": None}
    assert echoed.violations == ()
    assert text.data == {"a": [1, 2]}


def _raw_host(tmp_path: Path, mode: str = "normal", **kwargs: Any) -> PluginHost:
    return host(tmp_path, raw_manifest(mode), resolver=SecretResolver({}, tmp_path), **kwargs)


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("raw.junk", "protocol_error"),
        ("raw.big", "output_too_large"),
        ("raw.bad_result", "protocol_error"),
        ("raw.bad_intents", "output_invalid"),
        ("raw.two_blocks", "output_invalid"),
        ("raw.not_json", "output_invalid"),
        ("raw.close", "plugin_crashed"),
        ("raw.unlisted", "tool_missing"),
    ],
)
def test_misbehaving_results_are_typed_errors(tmp_path: Path, tool: str, kind: str) -> None:
    async def scenario() -> None:
        async with _raw_host(tmp_path) as h:
            assert (await _error(h, tool, timeout_s=60)).kind == kind
            assert (await _call(h, "raw.ok")).data == {"ok": True}  # restarted if needed

    _run(scenario())


def test_a_json_rpc_error_keeps_the_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _raw_host(tmp_path) as h:
            error = await _error(h, "raw.rpc_error")
            assert error.kind == "protocol_error"
            assert "unknown argument" in error.message
            entry = h._entries["raw"]
            assert (await _call(h, "raw.ok")).data == {"ok": True}
            assert entry.starts == 1

    _run(scenario())


def test_no_client_capability_is_advertised(tmp_path: Path) -> None:
    async def scenario() -> Any:
        async with _raw_host(tmp_path) as h:
            return (await _call(h, "raw.init")).data

    initialize = _run(scenario())
    assert initialize["capabilities"] == {}


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("sampling/createMessage", {"messages": [], "maxTokens": 10}),
        ("roots/list", {}),
        (
            "elicitation/create",
            {"message": "your password?", "requestedSchema": {"type": "object", "properties": {}}},
        ),
    ],
)
def test_server_requests_to_the_client_are_refused(
    tmp_path: Path, method: str, params: dict[str, Any]
) -> None:
    async def scenario() -> Any:
        async with _raw_host(tmp_path) as h:
            return (await _call(h, "raw.ask", {"method": method, "params": params})).data

    answer = _run(scenario())["answer"]
    assert "result" not in answer, answer
    assert answer["error"]["message"]


def test_a_null_structured_result_is_data(tmp_path: Path) -> None:
    async def scenario() -> Any:
        async with _raw_host(tmp_path) as h:
            return (await _call(h, "raw.null")).data

    assert _run(scenario()) is None


@pytest.mark.parametrize(
    ("mode", "kind", "text"),
    [("silent", "startup_timeout", "did not start within 1 s"), ("exit", "handshake_failed", "")],
)
def test_a_failed_start_makes_the_plugin_unavailable(
    tmp_path: Path, mode: str, kind: str, text: str
) -> None:
    def timeouts(name: str) -> PluginDefaults:
        return PluginDefaults(startup_timeout_s=1.0)

    async def scenario() -> None:
        async with _raw_host(tmp_path, mode, timeouts=timeouts) as h:
            error = await _error(h, "raw.ok")
            assert error.kind == kind
            assert text in error.message
            assert (await _error(h, "raw.ok")).kind == "plugin_unavailable"

    _run(scenario())


def test_a_plugin_ignoring_sigterm_is_killed(tmp_path: Path) -> None:
    async def scenario() -> int:
        async with _raw_host(tmp_path, "stubborn") as h:
            await _call(h, "raw.ok")
            process = h._entries["raw"].process
            assert process is not None
            assert process.state.process is not None
            return process.state.process.pid

    started = time.monotonic()
    pid = _run(scenario())
    assert _gone(pid)
    assert time.monotonic() - started < 30


def test_runtime_and_tool_problems(tmp_path: Path) -> None:
    remote = {
        "name": "remote",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": ["remote.brief"]},
        "permissions": {"network": ["mcp.example.org"]},
        "runtime": {"protocol": "mcp-http", "url": "https://mcp.example.org/mcp"},
    }
    pack = {
        "name": "pack",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": ["pack.nothing"], "skills": ["pack/brief"]},
        "permissions": {},
    }

    async def scenario() -> None:
        async with host(tmp_path, probe_manifest(), remote, pack) as h:
            assert (await _error(h, "remote.brief")).kind == "runtime_unsupported"
            assert (await _error(h, "pack.nothing")).kind == "runtime_unsupported"
            assert (await _error(h, "nobody.tool")).kind == "tool_missing"
            assert h.registry.plugin_for_skill("pack/brief").manifest.name == "pack"

    _run(scenario())


def test_an_unexpected_host_bug_is_a_host_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with host(tmp_path) as h:

            def broken(ref: str) -> Any:
                raise RuntimeError(f"secret {PROBE_TOKEN}")

            monkeypatch.setattr(h.registry, "installed_for_tool", broken)
            error = await _error(h, "probe.env")
            assert error.kind == "host_error"
            assert PROBE_TOKEN not in error.message

    _run(scenario())


# --- through the engine ----------------------------------------------------------------------


def _ntfy() -> dict[str, Any]:
    return {
        "name": "ntfy",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"notifiers": ["ntfy.push"]},
        "permissions": {"intents": ["notify.push"]},
    }


def _pipeline(*steps: dict[str, Any]) -> Pipeline:
    return Pipeline.model_validate({"name": "probe-run", "steps": list(steps)})


async def _execute(h: PluginHost, pipeline: Pipeline) -> Any:
    return await execute(
        pipeline,
        run_id="run-1",
        context=make_context(T0, "UTC"),
        started_at=T0,
        plugins=h.registry,
        tools=h,
    )


def test_intents_from_meta_are_validated_and_routed_by_the_engine(tmp_path: Path) -> None:
    push = {"type": "notify.push", "title": "Probe", "body": "hello"}
    task = {"type": "task.create", "title": "Not allowed", "fingerprint": "probe:1"}
    ok = _pipeline(
        {"collect": [{"probe.intents": {"proposed": [push]}}]}, {"notify": ["ntfy.push"]}
    )
    rejected = _pipeline(
        {"collect": [{"probe.intents": {"proposed": [task]}}]}, {"notify": ["ntfy.push"]}
    )

    async def scenario() -> tuple[Any, Any]:
        async with host(tmp_path, probe_manifest(), _ntfy()) as h:
            return await _execute(h, ok), await _execute(h, rejected)

    good, bad = _run(scenario())
    assert good.status == "ok"
    assert [(i.intent.type, i.applied_by) for i in good.intents] == [("notify.push", "ntfy.push")]
    assert bad.status == "failed"
    assert bad.steps[0].error is not None
    assert bad.steps[0].error.kind == "intent_rejected"


def test_a_pipeline_survives_a_timeout_and_records_violations(tmp_path: Path) -> None:
    pipeline = _pipeline(
        {
            "collect": [
                {"tool": "probe.sleep", "params": {"seconds": 60}, "timeout_s": 1},
                {"probe.echo": {"reveal": "PROBE_TOKEN"}},
                {"probe.stderr": {"parts": [{"text": "oops "}, {"env": "PROBE_TOKEN"}]}},
            ]
        },
        {"transform": [{"tool": "probe.echo", "params": {"value": 1}, "timeout_s": 30}]},
    )

    async def scenario() -> tuple[Any, dict[str, str]]:
        async with host(tmp_path) as h:
            record = await _execute(h, pipeline)
        return record, h.leftover_stderr

    record, leftover = _run(scenario())
    assert record.status == "degraded"
    collect = record.steps[0]
    assert collect.status == "degraded"
    assert collect.output == [
        {"source": "probe.sleep", "call": 0, "params": {"seconds": 60}, "error_kind": "timeout"}
    ]
    assert [snapshot.source for snapshot in record.inputs] == ["probe.echo", "probe.stderr"]
    assert [v.kind for v in record.violations] == ["secret_in_output"], record.violations
    # The quiet-window heuristic usually puts it in the step's stderr; a slow pipe may move it
    # to the next call's, or to what the probe wrote after its last call (decision 35 F).
    everywhere = "".join(step.stderr or "" for step in record.steps) + "".join(leftover.values())
    assert "oops [REDACTED:PROBE_TOKEN]" in everywhere
    assert PROBE_TOKEN not in record.model_dump_json()
    assert PROBE_TOKEN not in everywhere
    transformed = record.steps[1].output
    assert transformed["value"] == 1
    assert transformed["input"][0]["source"] == "probe.echo"


def test_the_registry_resolves_tools_for_planning(tmp_path: Path) -> None:
    reg = registry(probe_manifest())
    resolved = reg.plugin_for_tool("probe.env")
    assert resolved.manifest.name == "probe"
    assert resolved.commit == lock_for(probe_manifest()).plugins["probe"].commit
    assert PluginManifest.model_validate(probe_manifest()).provides == resolved.manifest.provides


# --- hardening (review findings, schemas decision 35) ----------------------------------------


def test_a_secret_of_a_plugin_not_started_yet_is_redacted(tmp_path: Path) -> None:
    """Decision 35 B: the host resolves every declared secret when it opens, so a plugin
    echoing another plugin's secret is redacted even before that plugin ever started."""
    other = "sentinel-value-for-bb-plugin"
    resolver = SecretResolver({}, secrets_dir(tmp_path, PROBE_TOKEN=PROBE_TOKEN, BB_TOKEN=other))
    bb = raw_manifest(secrets=["BB_TOKEN"])

    async def scenario() -> Any:
        async with host(tmp_path, probe_manifest(), bb, resolver=resolver) as h:
            result = await _call(h, "probe.echo", {"value": f"from bb: {other}"})
            assert "raw" not in h._entries  # bb was never started
            return result

    result = _run(scenario())
    assert result.data["value"] == "from bb: [REDACTED:BB_TOKEN]"
    assert [v.kind for v in result.violations] == ["secret_in_output"]
    assert "BB_TOKEN" in result.violations[0].detail


def test_basic_auth_hex_and_misaligned_base64_in_the_output_are_redacted(tmp_path: Path) -> None:
    raw = PROBE_TOKEN.encode()
    values = [
        "Basic " + base64.b64encode(b"user:" + raw).decode(),
        base64.b64encode(b"x" + raw + b"y").decode(),
        raw.hex(),
        json.dumps({"nested": {"token": PROBE_TOKEN}}),
    ]

    async def scenario() -> Any:
        async with host(tmp_path) as h:
            return await _call(h, "probe.echo", {"value": values})

    result = _run(scenario())
    for original, redacted in zip(values, result.data["value"], strict=True):
        assert redacted != original
        assert REDACTED_PROBE in redacted
    assert PROBE_TOKEN not in json.dumps(result.data)
    assert [v.kind for v in result.violations] == ["secret_in_output"]


def test_a_secret_as_an_object_key_fails_the_call(tmp_path: Path) -> None:
    async def scenario() -> Any:
        async with host(tmp_path) as h:
            return await _error(h, "probe.echo", {"value": {PROBE_TOKEN: 1}})

    error = _run(scenario())
    assert error.kind == "output_invalid"
    assert "object key holds secret PROBE_TOKEN" in error.message
    assert PROBE_TOKEN not in error.message


def test_a_number_equal_to_a_secret_is_redacted(tmp_path: Path) -> None:
    resolver = SecretResolver({}, secrets_dir(tmp_path, PROBE_TOKEN=PROBE_TOKEN, PIN="4242"))
    resolver.resolve("PIN")  # a short, numeric instance secret

    async def scenario() -> Any:
        async with host(tmp_path, resolver=resolver) as h:
            return await _call(h, "probe.echo", {"value": [4242, 4243, 42.42, "4242"]})

    result = _run(scenario())
    assert result.data["value"] == ["[REDACTED:PIN]", 4243, 42.42, "[REDACTED:PIN]"]
    assert "PIN" in result.violations[0].detail


def test_log_data_with_a_control_character_secret_is_redacted(tmp_path: Path) -> None:
    secret = "ctl\x01-secret-value"  # noqa: S105 (synthetic; `repr` would show `\x01`)
    resolver = SecretResolver({}, secrets_dir(tmp_path, CTL=secret))
    resolver.resolve("CTL")

    async def scenario() -> Any:
        async with host(tmp_path, raw_manifest(), resolver=resolver) as h:
            return await _call(h, "raw.log", {"token": secret})

    result = _run(scenario())
    assert result.stderr is not None  # log notifications arrive before the result
    assert '[log info] {"token": "[REDACTED:CTL]"}' in result.stderr
    assert "ctl" not in result.stderr


def test_deeply_nested_text_json_is_output_invalid_and_keeps_stderr(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, str]:
        async with host(tmp_path) as h:
            body = "[" * 200_000 + "]" * 200_000
            error = await _error(h, "probe.text", {"body": body, "note": "deep NOTE-END\n"})
            stderr = await _drain(h, "probe", error.stderr, "NOTE-END")
            assert (await _call(h, "probe.env")).data["pid"] > 0  # the process is fine
            return error, stderr

    error, stderr = _run(scenario())
    assert error.kind == "output_invalid"
    assert "nested too deeply" in error.message
    assert "deep NOTE-END" in stderr


def test_the_sdk_log_records_are_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    resolver = SecretResolver({}, secrets_dir(tmp_path, OTHER_SECRET=OTHER_SECRET))
    resolver.resolve("OTHER_SECRET")

    async def scenario() -> Any:
        async with host(tmp_path, raw_manifest(), resolver=resolver) as h:
            return await _call(h, "raw.progress", {"token": OTHER_SECRET})

    with caplog.at_level(logging.DEBUG):
        assert _run(scenario()).data == {"ok": True}
    captured = capfd.readouterr()
    assert OTHER_SECRET not in caplog.text
    assert OTHER_SECRET not in captured.err + captured.out
    warned = [r for r in caplog.records if "Failed to validate notification" in r.getMessage()]
    assert warned, caplog.text
    assert all(r.exc_info is None and "details withheld" in r.getMessage() for r in warned)


def test_stderr_of_a_process_that_died_between_calls_goes_with_the_next_call(
    tmp_path: Path,
) -> None:
    dying = {
        "parts": [{"text": "dying "}, {"env": "PROBE_TOKEN"}, {"text": "\n"}],
        "delay_s": 0.2,
        "exit_code": 7,
    }

    async def scenario() -> Any:
        async with host(tmp_path) as h:
            await _call(h, "probe.env")
            assert (await _call(h, "probe.stderr", dying)).data == {"written": 0}
            process = h._entries["probe"].process
            assert process is not None
            deadline = time.monotonic() + 10
            while process.alive and time.monotonic() < deadline:  # the host notices the exit
                await asyncio.sleep(0.02)
            assert not process.alive
            return await _call(h, "probe.env")

    after = _run(scenario())
    assert after.stderr is not None
    assert after.stderr.startswith("--- previous process ---\ndying [REDACTED:PROBE_TOKEN]\n")


def test_stderr_written_after_the_last_call_is_kept_redacted(tmp_path: Path) -> None:
    late = {
        "parts": [{"text": "late "}, {"env": "PROBE_TOKEN"}, {"text": " LATE-END\n"}],
        "delay_s": 0.2,
    }

    async def scenario() -> dict[str, str]:
        async with host(tmp_path) as h:
            await _call(h, "probe.stderr", late)
            process = h._entries["probe"].process
            assert process is not None
            deadline = time.monotonic() + 10
            while b"LATE-END" not in bytes(process.capture._head) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        return h.leftover_stderr

    leftover = _run(scenario())
    assert leftover == {"probe": "late [REDACTED:PROBE_TOKEN] LATE-END\n"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _Watch:
    """A `ToolCaller` over a host that notes which plugin processes are alive at each call."""

    def __init__(self, inner: PluginHost, fail_release: bool = False) -> None:
        self.inner = inner
        self.fail_release = fail_release
        self.pids: dict[str, int] = {}
        self.alive: dict[str, dict[str, bool]] = {}
        self.released: list[str] = []

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> Any:
        self.alive[ref] = {name: _alive(pid) for name, pid in self.pids.items()}
        result = await self.inner.call(ref, params, input, timeout_s=timeout_s)
        plugin = ref.split(".")[0]
        process = self.inner._entries[plugin].process
        if process is not None and process.state.process is not None:
            self.pids[plugin] = process.state.process.pid
        return result

    async def release(self, plugin: str) -> None:
        self.released.append(plugin)
        if self.fail_release:
            raise RuntimeError("release failed")
        await self.inner.release(plugin)


@pytest.mark.parametrize("fail_release", [False, True])
def test_a_plugin_is_stopped_after_its_last_planned_call(
    tmp_path: Path, fail_release: bool
) -> None:
    pipeline = _pipeline({"collect": [{"probe.env": {}}]}, {"transform": ["raw.ok"]})

    async def scenario() -> tuple[Any, _Watch]:
        async with host(tmp_path, probe_manifest(), raw_manifest()) as h:
            watch = _Watch(h, fail_release)
            record = await execute(
                pipeline,
                run_id="run-1",
                context=make_context(T0, "UTC"),
                started_at=T0,
                plugins=h.registry,
                tools=watch,
            )
            return record, watch

    record, watch = _run(scenario())
    assert record.status == "ok", record.steps
    assert watch.released == ["probe", "raw"]
    if fail_release:  # a failing release is ignored; the host still stops it at exit
        assert watch.alive["raw.ok"] == {"probe": True}
    else:
        assert watch.alive["raw.ok"] == {"probe": False}  # gone before the transform ran
    assert not any(_alive(pid) for pid in watch.pids.values())
