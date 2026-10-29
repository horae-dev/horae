# SPDX-License-Identifier: Apache-2.0
"""The live isolation check of the `claude-cli` backend against the real Claude Code (#29, T11,
answer 3 on #97, schemas decision 42 E). **Never runs in CI**: skipped unless
`HORAE_LIVE_CLAUDE=1` and `HORAE_LIVE_CLAUDE_SECRETS_DIR` names a directory holding a
`CLAUDE_CODE_OAUTH_TOKEN` file (a token made with `claude setup-token`).
`HORAE_LIVE_CLAUDE_COMMAND` is the executable (default: `claude` on this shell's `PATH`, then
`~/.local/bin/claude`; it must be the native binary, see decision 42). Each test makes one to
three small calls on your plan.

Run it before bumping `CLAUDE_CODE_VERSION_RANGE` (and before the flag set of #29 is final)
and paste its output into the PR:

    HORAE_LIVE_CLAUDE=1 HORAE_LIVE_CLAUDE_SECRETS_DIR=<dir with CLAUDE_CODE_OAUTH_TOKEN> \\
        uv run pytest -m live tests/test_llm_claude_cli_live.py -v -s

**Positive controls.** A negative assertion ("the canary did not fire") proves nothing if the
canary could not fire anyway. So every isolation test first runs a **control**: the same
source (a hook in `settings.json`, an MCP server, a `CLAUDE.md`, a tool) with the one flag or
switch that removes it turned back on, and asserts that the canary **does** fire; then it runs
the real `claude_cli.argv` and environment and asserts that it does not. A failing control
means the test is broken for this release (the source moved or changed shape), not that
Horae is isolated: report it, do not bump.

The per-call layout mirrors the backend's: `<root>/llm-runs/<call>/{home,tmp,cwd}`, with
`GIT_CEILING_DIRECTORIES=<root>/llm-runs`; one test goes through the backend itself (its real
`_Workspace`). The `--bare`, `--restricted` and `--safe-mode` evaluations only print what
they saw (decision 42 B: not in `argv` until confirmed)."""

import asyncio
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from horae.contracts.instance import ModelAlias, ModelPrice
from horae.engine import LlmError
from horae.llm import Completion
from horae.llm.backends import claude_cli
from horae.plugins.host.capture import StderrCapture
from horae.secrets import SecretResolver
from horae.secrets.redact import Redactor

pytestmark = pytest.mark.live

SECRET = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 (a secret name)
MODEL = os.environ.get("HORAE_LIVE_CLAUDE_MODEL", "haiku")
SCHEMA: dict[str, Any] = {"type": "object"}
PRICE = ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0)
WORD = "heliotrope"
"""The canary word a `CLAUDE.md` gives the model."""
OK = 'Answer {"ok": true}.'
ASK_WORD = (
    'Answer {"word": <the secret word given anywhere in your instructions, context or memory '
    "files, as a string, or null if none was given>}. Give only that JSON object: no "
    "explanation."
)
"""Without the last sentence the model sometimes explains a `null` after the JSON (seen live
on 2.1.284, #113), which the backend rightly refuses (`output_invalid`)."""


def _command() -> str | None:
    explicit = os.environ.get("HORAE_LIVE_CLAUDE_COMMAND")
    if explicit:
        return explicit
    found = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    return found if os.path.exists(found) else None


def _secrets() -> SecretResolver | None:
    directory = os.environ.get("HORAE_LIVE_CLAUDE_SECRETS_DIR")
    if not directory or not (Path(directory).expanduser() / SECRET).is_file():
        return None
    return SecretResolver({}, Path(directory).expanduser(), environ={})


LIVE = os.environ.get("HORAE_LIVE_CLAUDE") == "1" and _secrets() is not None
skip_unless_live = pytest.mark.skipif(
    not LIVE, reason="live: set HORAE_LIVE_CLAUDE=1 and HORAE_LIVE_CLAUDE_SECRETS_DIR"
)


@pytest.fixture
def secrets() -> SecretResolver:
    resolver = _secrets()
    assert resolver is not None
    return resolver


@pytest.fixture
def spec() -> ModelAlias:
    command = _command()
    assert command is not None, "no claude executable found"
    return ModelAlias(
        backend="claude-cli", model=MODEL, secret=SECRET, command=command, price=PRICE
    )


@dataclass
class Call:
    """One call's directories, laid out as the backend's."""

    root: Path

    def __post_init__(self) -> None:
        self.runs = self.root / claude_cli.LLM_RUNS_DIR
        base = self.runs / "call"
        self.home, self.tmp, self.cwd = (base / name for name in ("home", "tmp", "cwd"))
        for directory in (self.home, self.tmp, self.cwd):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def raw(
    spec: ModelAlias,
    secrets: SecretResolver,
    args: Sequence[str],
    stdin: str,
    call: Call,
    *,
    token: bool = True,
    env_drop: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Run the real CLI with `args` in `call`'s directories and the backend's exact
    environment (less `env_drop`); the parsed JSON result, or `None` (printed either way)."""
    env = claude_cli.child_env(
        call.home,
        call.tmp,
        secrets.resolve(SECRET) if token else None,
        ceiling=call.runs,
        max_tokens=1024,
    )
    for name in env_drop:
        env.pop(name)
    capture = StderrCapture(0)
    outcome = asyncio.run(
        claude_cli.run(
            [claude_cli.resolve(spec), *args],
            env=env,
            cwd=call.cwd,
            stdin=stdin.encode(),
            timeout_s=120.0,
            capture=capture,
        )
    )
    stderr = capture.take(Redactor(secrets.resolved()))
    print(f"exit={outcome.returncode} stdout={outcome.stdout[:2000]!r} stderr={stderr!r}")
    try:
        data = json.loads(outcome.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    data = raw(*args, **kwargs)
    assert data is not None, "no JSON result"
    return data


def answer(data: Mapping[str, Any]) -> Any:
    """The JSON value of a successful result, as the backend reads it (one enclosing code
    fence allowed: the model wraps its JSON in one, #113)."""
    assert data["is_error"] is False, data.get("result")
    return json.loads(claude_cli.unfenced(data["result"]))


SYSTEM = "Answer with one JSON value only, no prose and no code fence."


def args(system: str = SYSTEM) -> list[str]:
    """The backend's exact arguments."""
    return claude_cli.argv(MODEL, system)


def variant(
    base: Sequence[str],
    *,
    values: Mapping[str, str] | None = None,
    drop: Sequence[str] = (),
    drop_pairs: Sequence[str] = (),
    extra: Sequence[str] = (),
) -> list[str]:
    """`base` with flag values replaced (`values`), flags removed (`drop`, alone; `drop_pairs`,
    with their value) and `extra` inserted before `--system-prompt`: a positive control."""
    out = list(base)
    for flag, value in (values or {}).items():
        out[out.index(flag) + 1] = value
    for flag in drop_pairs:
        at = out.index(flag)
        del out[at : at + 2]
    for flag in drop:
        out.remove(flag)
    at = out.index("--system-prompt")
    return [*out[:at], *extra, *out[at:]]


def control_then_real(
    make: Callable[[Path], tuple[Call, Path]],
    run_control: Callable[[Call], object],
    run_real: Callable[[Call], object],
    tmp_path: Path,
) -> None:
    """The positive control (the canary fires), then the real flags (it does not)."""
    call, canary = make(tmp_path / "control")
    run_control(call)
    assert canary.exists(), "CONTROL FAILED: the canary did not fire with the source enabled"
    call, canary = make(tmp_path / "real")
    run_real(call)
    assert not canary.exists(), "the canary fired under the real flags"


# --- the basics ------------------------------------------------------------------------------


@skip_unless_live
def test_live_version_is_in_the_supported_range(spec: ModelAlias) -> None:
    version = asyncio.run(claude_cli.probe_version(claude_cli.resolve(spec)))
    shown = ".".join(map(str, version))
    print(f"Claude Code {shown}, range {claude_cli.CLAUDE_CODE_VERSION_RANGE}")


@skip_unless_live
def test_live_a_completion_through_the_backend(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    backend = claude_cli.ClaudeCliBackend(
        "live", spec, secrets, work_dir=tmp_path / claude_cli.LLM_RUNS_DIR
    )
    completion = Completion(MODEL, OK, {"n": 1}, SCHEMA, 200, 120.0)
    outcome = asyncio.run(backend.complete(completion))
    print(outcome)
    assert outcome.output == {"ok": True}
    assert outcome.usage.tokens_out > 0
    assert outcome.usage.priced is True
    assert list((tmp_path / claude_cli.LLM_RUNS_DIR).iterdir()) == []


@skip_unless_live
def test_live_the_result_shape(spec: ModelAlias, secrets: SecretResolver, tmp_path: Path) -> None:
    data = result(spec, secrets, args(), OK, Call(tmp_path))
    for key in ("type", "subtype", "is_error", "result", "num_turns", "total_cost_usd", "usage"):
        assert key in data
    assert (data["type"], data["subtype"]) == ("result", "success")
    assert data["num_turns"] == 1
    for key in ("input_tokens", "output_tokens"):
        assert isinstance(data["usage"][key], int)


@skip_unless_live
def test_live_the_output_limit_is_honoured(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """Decision 42 F: `CLAUDE_CODE_MAX_OUTPUT_TOKENS` bounds the output (a long answer asked
    with a small limit is cut, or refused by the backend, never over the limit)."""
    backend = claude_cli.ClaudeCliBackend(
        "live", spec, secrets, work_dir=tmp_path / claude_cli.LLM_RUNS_DIR
    )
    prompt = 'Answer {"words": [<the numbers from 1 to 2000 spelled out in English>]}.'
    completion = Completion(MODEL, prompt, {"n": 1}, SCHEMA, 64, 120.0)
    refused: LlmError | None = None
    try:
        outcome = asyncio.run(backend.complete(completion))
    except LlmError as error:
        refused = error
    if refused is not None:
        print(f"refused: {refused.kind}: {refused.message} usage={refused.usage}")
        assert "over the max_tokens limit" not in refused.message, "the limit was not honoured"
        assert refused.kind == "output_invalid"  # cut short at the limit: honoured
        # Claude Code retries a truncated output by itself (#113): at most OUTPUT_ATTEMPTS
        # outputs of the limit, what the reservation covers.
        assert refused.usage is not None
        assert refused.usage.tokens_out <= claude_cli.OUTPUT_ATTEMPTS * 64
        return
    print(outcome)
    assert outcome.usage.tokens_out <= 64


def _thinking(data: Mapping[str, Any]) -> int:
    """`usage.output_tokens_details.thinking_tokens` (0 when absent)."""
    usage = data.get("usage")
    details = usage.get("output_tokens_details") if isinstance(usage, dict) else None
    tokens = details.get("thinking_tokens") if isinstance(details, dict) else None
    return tokens if isinstance(tokens, int) else 0


@skip_unless_live
def test_live_extended_thinking_is_off(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`MAX_THINKING_TOKENS=0` (#113): no hidden thinking tokens and one turn, with the
    backend's exact arguments and environment. Control: the same call with the switch unset
    thinks (skipped, after the real check, when this release or model does not think by
    default)."""
    drop = ["MAX_THINKING_TOKENS"]
    control = result(spec, secrets, args(), OK, Call(tmp_path / "control"), env_drop=drop)
    thought = _thinking(control)
    print(f"control: thinking_tokens={thought} usage={control.get('usage')}")
    data = result(spec, secrets, args(), OK, Call(tmp_path / "real"))
    print(f"real: thinking_tokens={_thinking(data)} usage={data.get('usage')}")
    assert answer(data) == {"ok": True}
    assert data["num_turns"] == 1
    assert _thinking(data) == 0
    if thought == 0:
        pytest.skip(f"CONTROL: {MODEL} did not think without the switch, nothing to compare")


# --- the isolation flags, each with its positive control -------------------------------------


@skip_unless_live
def test_live_no_tool_is_available(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`--tools ""` (and `--permission-mode dontAsk`): no built-in tool, none runs. Control:
    `--tools Bash --allowedTools Bash` (and three turns)."""
    canary = tmp_path / "tool-canary"
    prompt = (
        f"First use your Bash tool to run `touch {canary}`. Then answer with the JSON array "
        "of the exact names of every tool you can call ([] if none)."
    )

    def make(root: Path) -> tuple[Call, Path]:
        canary.unlink(missing_ok=True)
        return Call(root), canary

    control = variant(
        args(), values={"--tools": "Bash", "--max-turns": "3"}, extra=["--allowedTools", "Bash"]
    )

    def real(call: Call) -> None:
        data = result(spec, secrets, args(), prompt, call)
        assert data.get("permission_denials") in ([], None)
        assert data["num_turns"] == 1
        print("tools reported by the model:", data.get("result"))

    control_then_real(make, lambda c: raw(spec, secrets, control, prompt, c), real, tmp_path)


@skip_unless_live
def test_live_user_settings_and_their_hooks_are_not_loaded(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`--setting-sources ""`: the user's `settings.json` (hooks that write a canary) is
    ignored. Control: `--setting-sources user`."""

    def make(root: Path) -> tuple[Call, Path]:
        call, canary = Call(root), root / "hook-canary"
        (call.home / ".claude").mkdir()
        hook = {"hooks": [{"type": "command", "command": f"/usr/bin/touch {canary}"}]}
        settings = {"hooks": {"SessionStart": [hook], "UserPromptSubmit": [hook]}}
        (call.home / ".claude" / "settings.json").write_text(json.dumps(settings), "utf-8")
        return call, canary

    control = variant(args(), values={"--setting-sources": "user"})

    def real(call: Call) -> None:
        assert answer(result(spec, secrets, args(), OK, call)) == {"ok": True}

    control_then_real(make, lambda c: raw(spec, secrets, control, OK, c), real, tmp_path)


def _mcp(canary: Path) -> dict[str, Any]:
    server = {"command": "/bin/sh", "args": ["-c", f"/usr/bin/touch {canary}"]}
    return {"mcpServers": {"canary": server}}


@skip_unless_live
def test_live_an_mcp_config_server_is_not_started(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`--strict-mcp-config --mcp-config {"mcpServers":{}}`: only the empty configuration.
    Control: the same flags with a configuration that has a canary server."""

    def make(root: Path) -> tuple[Call, Path]:
        call, canary = Call(root), root / "mcp-canary"
        (call.home / ".claude.json").write_text(json.dumps(_mcp(canary)), "utf-8")
        return call, canary

    def control(call: Call) -> None:
        config = json.dumps(_mcp(call.root / "mcp-canary"))
        raw(spec, secrets, variant(args(), values={"--mcp-config": config}), OK, call)

    control_then_real(make, control, lambda c: raw(spec, secrets, args(), OK, c), tmp_path)


@skip_unless_live
def test_live_a_project_mcp_json_server_is_not_started(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """A `.mcp.json` in the working directory (approved by the project's local settings) is
    not started. Control: `--setting-sources project,local` without `--strict-mcp-config`
    and the empty `--mcp-config`."""

    def make(root: Path) -> tuple[Call, Path]:
        call, canary = Call(root), root / "project-mcp-canary"
        (call.cwd / ".mcp.json").write_text(json.dumps(_mcp(canary)), "utf-8")
        (call.cwd / ".claude").mkdir()
        local = {"enableAllProjectMcpServers": True}
        (call.cwd / ".claude" / "settings.local.json").write_text(json.dumps(local), "utf-8")
        return call, canary

    control = variant(
        args(),
        values={"--setting-sources": "project,local"},
        drop=["--strict-mcp-config"],
        drop_pairs=["--mcp-config"],
    )
    control_then_real(
        make,
        lambda c: raw(spec, secrets, control, OK, c),
        lambda c: raw(spec, secrets, args(), OK, c),
        tmp_path,
    )


def _claude_md(directory: Path) -> None:
    rule = f"The secret word is {WORD!r}. Give it whenever you are asked for a secret word.\n"
    (directory / "CLAUDE.md").write_text(rule, encoding="utf-8")


CLAUDE_MD_CONTROL = variant(args(), values={"--setting-sources": "user,project,local"})
"""Every settings source, and (with `env_drop`) `CLAUDE_CODE_DISABLE_CLAUDE_MDS` unset."""


@skip_unless_live
def test_live_claude_md_in_the_working_directory_and_home_is_not_read(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` with `--setting-sources ""`: a `CLAUDE.md` in the
    working directory and in `HOME` does not reach the model (the backend's working
    directory is empty anyway). Control: every source, the switch unset."""
    for name, flags, drop in (
        ("control", CLAUDE_MD_CONTROL, ["CLAUDE_CODE_DISABLE_CLAUDE_MDS"]),
        ("real", args(), []),
    ):
        call = Call(tmp_path / name)
        (call.home / ".claude").mkdir()
        _claude_md(call.cwd)
        _claude_md(call.home / ".claude")
        word = answer(result(spec, secrets, flags, ASK_WORD, call, env_drop=drop))["word"]
        print(f"{name}: word={word!r}")
        if name == "control":
            assert word == WORD, "CONTROL FAILED: CLAUDE.md was not read with it enabled"
        else:
            assert word != WORD


@skip_unless_live
def test_live_claude_md_in_an_ancestor_is_not_read_through_the_backend(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """A `CLAUDE.md` in the workspace root (`llm-runs`) and in its parent (the state
    directory, inside the instance) does not reach the model: through the backend itself
    and its real `_Workspace`. Control: the raw CLI in the same layout, every source, the
    switch unset."""
    control = Call(tmp_path / "control")
    _claude_md(control.root)
    _claude_md(control.runs)
    data = result(
        spec,
        secrets,
        CLAUDE_MD_CONTROL,
        ASK_WORD,
        control,
        env_drop=["CLAUDE_CODE_DISABLE_CLAUDE_MDS"],
    )
    word = answer(data)["word"]
    print(f"control: word={word!r}")
    assert word == WORD, "CONTROL FAILED: an ancestor CLAUDE.md was not read with it enabled"

    state = tmp_path / "real"
    runs = state / claude_cli.LLM_RUNS_DIR
    runs.mkdir(parents=True, mode=0o700)
    _claude_md(state)
    _claude_md(runs)
    backend = claude_cli.ClaudeCliBackend("live", spec, secrets, work_dir=runs)
    completion = Completion(MODEL, ASK_WORD, {"n": 1}, SCHEMA, 200, 120.0)
    outcome = asyncio.run(backend.complete(completion))
    print(f"real: {outcome.output!r}")
    assert outcome.output.get("word") != WORD
    assert sorted(path.name for path in runs.iterdir()) == ["CLAUDE.md"]


@skip_unless_live
def test_live_the_system_prompt_replaces_the_default(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`--system-prompt`: the model sees Horae's prompt, not Claude Code's default one."""
    system = f'Your secret word is "{WORD}". Answer with one JSON value only.'
    prompt = (
        'Answer {"word": <your secret word or null>, "mentions_claude_code": <whether your '
        "instructions mention Claude Code or software engineering tasks>}."
    )
    output = answer(result(spec, secrets, args(system), prompt, Call(tmp_path)))
    print(output)
    assert output["word"] == WORD
    assert output["mentions_claude_code"] is False


@skip_unless_live
def test_live_no_session_or_memory_is_persisted(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """`--no-session-persistence` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: no transcript and
    no memory file is written under `HOME`."""
    call = Call(tmp_path)
    raw(spec, secrets, args(), OK, call)
    written = sorted(str(path.relative_to(call.home)) for path in call.home.rglob("*"))
    print("written under HOME:", written)
    assert list(call.home.rglob("*.jsonl")) == []
    assert [path for path in written if "memory" in path.lower()] == []


@skip_unless_live
def test_live_without_the_token_nothing_authenticates(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path
) -> None:
    """An empty `HOME` and no token: no ambient login (keychain, real `HOME`) is used."""
    data = result(spec, secrets, args(), OK, Call(tmp_path), token=False)
    assert data["is_error"] is True
    print(data.get("result"))


@skip_unless_live
@pytest.mark.parametrize("flag", ["--bare", "--restricted", "--safe-mode"])
def test_live_evaluate_bare_restricted_and_safe_mode(
    spec: ModelAlias, secrets: SecretResolver, tmp_path: Path, flag: str
) -> None:
    """Answer 3 and decision 42 B: `--bare` / `--restricted` / `--safe-mode` are adopted only
    if they exist and keep token auth. This prints what the release does with each (an
    unknown option, an auth failure, or a success); it asserts nothing."""
    flags = variant(args(), extra=[flag])
    data = raw(spec, secrets, flags, OK, Call(tmp_path))
    if data is None:
        print(f"EVALUATION {flag}: no JSON result (unknown option?)")
        return
    shown = str(data.get("result"))[:200]
    print(f"EVALUATION {flag}: is_error={data.get('is_error')} result={shown!r}")
