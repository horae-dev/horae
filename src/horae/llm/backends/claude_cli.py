# SPDX-License-Identifier: Apache-2.0
"""The `claude-cli` backend: the user's own Claude Code, run isolated (#29, ADR 0005, T11).

**Not a legal assessment.** Running `claude -p` headless on your own plan is PLAN.md's
**⚠️ unverified** reading of the terms (risk #70): this backend is for personal use under your
plan's terms; `anthropic` with an API key is the recommended backend for published setups.

One `ProviderBackend` per model alias (`backend: claude-cli`). Schemas decision 42 has the
rules; in short:

**Render, then send** (`horae.llm.base`). `render(completion)` is pure. Its payload is
`{"argv", "stdin", "max_tokens"}`: `argv` the arguments after the executable, `stdin` what the
process reads, `max_tokens` the step's output budget (passed as
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`, and used for the stdout cap, the usage floor and the
conservative charge). The arguments are fixed but for the
model and the system prompt (`ARGV`): print mode, one JSON result, no built-in tool
(`--tools ""`), only the empty MCP configuration (`--strict-mcp-config --mcp-config
{"mcpServers":{}}`), no settings source (`--setting-sources ""`), no session persistence, no
slash command, `--permission-mode dontAsk`, one turn (`--max-turns 1`), and `--system-prompt`
with Horae's `SYSTEM_PREAMBLE` then the skill's prompt (trusted; it starts with the preamble,
so it never looks like an option). The untrusted input and the repair errors go on **stdin**,
fenced and escaped as for every backend, never in `argv` (visible in `ps`). `send` refuses a
payload whose `argv` is not exactly `argv(model, system)` (same length, same items), and an
alias without a `price` (decision 42 A: so that every call has a dollar reservation).

**The process** (the plugin host's posture, #24): an argument list, never a shell; the
executable resolved to an absolute path (`models.<alias>.command`, else `claude` on the fixed
`BASE_PATH`, never Horae's `PATH`), its symlinks resolved; **exactly** this environment, nothing
inherited: `PATH` (`BASE_PATH`), `HOME` and `TMPDIR` (fresh empty per-call directories, mode
0700), `LANG=C.UTF-8`, `TZ=UTC`, `ENV_SWITCHES` (no auto-update, telemetry or non-essential
traffic, no `CLAUDE.md`, no auto memory), `GIT_CEILING_DIRECTORIES` (the workspace root, so no
git repository above it is found), `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (the step's `max_tokens`)
and `CLAUDE_CODE_OAUTH_TOKEN`, the alias' declared `secret` (token mode, the only mode in v1;
a `claude setup-token` token), revealed into the child's mapping only; the working directory
an empty per-call directory (no `CLAUDE.md`, no project settings); umask 077; a new session
(its own process group); the per-call directories removed afterwards (also on cancellation).
The per-call directories live under a Horae-owned 0700 directory, `<state_dir>/llm-runs`
(`LLM_RUNS_DIR`, wired by `horae.runner.open_ports`), not the system temporary directory
(decision 42 C); a backend built without one (a test, `horae doctor`'s version check) falls
back to a fresh private directory in the system temporary directory.

**Bounds.** The step's timeout, then the group gets `SIGTERM`, 2 s later `SIGKILL`
(`horae.plugins.host.transport.terminate`); stdout at most `_shared.response_cap(max_tokens)`
bytes (1 to 8 MiB, else `output_invalid`, as an HTTP body, T33); stderr captured head and tail
(64 KiB, drained for at most `DRAIN_S` after the exit) and redacted with every resolved secret
(the token included) before it reaches an `LlmError`.

**The version is pinned** (`CLAUDE_CODE_VERSION_RANGE`): `<command> --version` runs once per
backend (one per alias per run), in the same isolated environment without the token; a
version outside the range, or a command that cannot be found or run, is `backend_unavailable`
and nothing is sent; when the command is a script whose interpreter is not on `BASE_PATH`
(the npm package's `#!/usr/bin/env node`), the message says so: `command` should name the
native binary. `horae doctor` runs the same check (`check`). Bumping the range requires
the live isolation test (`tests/test_llm_claude_cli_live.py`) against the new release, its
output in the bump PR's body and the version in `docs/security.md` T11.

**The result.** stdout must be UTF-8 and one JSON object (strict `_shared.parse`) with `type:
"result"`. `is_error` maps to `LlmError` kinds (`api_error_status` through
`_shared.STATUS_KINDS`, `error_max_turns` → `output_invalid`, "Not logged in" →
`auth_failed`, anything else `host_error`); a non-zero exit without a result is `host_error`.
A successful result must have `subtype: "success"`, `num_turns: 1`, no `permission_denials`,
a stop reason other than `tool_use` / `max_tokens`, reported `output_tokens` no larger than
`max_tokens`, and a `result` string that is one JSON value (no fence stripping; schema
validation is #31's); otherwise `output_invalid` with the usage. Messages are Horae's (never
the CLI's text).

**Usage and cost.** Tokens from `usage` (`input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`), floored as for every backend
(`_shared.floored`; missing or unusable counts are charged conservatively). `total_cost_usd`
is notional under a subscription but counts against the step and daily budgets (answer 4 on
#97): `usd` is the larger of it and the alias' (required) `price` applied to the tokens.
Claude Code caches prompts by itself: `may_write_cache` is `True` (decision 41).
"""

import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import Process

from horae.contracts.instance import ModelAlias
from horae.contracts.run import Usage
from horae.engine.ports import LlmBackendName, LlmError
from horae.llm.backends._shared import (
    INVALID,
    MAX_RESPONSE_BYTES,
    STATUS_KINDS,
    count,
    floored,
    parse,
    response_cap,
    system_text,
    token,
    user_texts,
    utf8,
    utf8_size,
)
from horae.llm.base import Completion, CompletionResult, cost, input_bound
from horae.plugins.host.capture import StderrCapture
from horae.plugins.host.env import BASE_PATH, base_env
from horae.plugins.host.host import CAPTURE_MARGIN
from horae.plugins.host.redact import Redactor
from horae.plugins.host.transport import _close_transport, exit_status, terminate
from horae.secrets import Secret, SecretError, SecretResolver

CLAUDE_CODE_VERSION_RANGE = ">=2.1.284,<2.2"
"""The Claude Code releases this backend runs (a minor range, refused outside it; answer 2
on #97). 2.1.284 is the release the flags were checked against."""
MIN_VERSION = (2, 1, 284)
BELOW_VERSION = (2, 2, 0)

COMMAND_NAME = "claude"
"""Looked up on `BASE_PATH` when the alias sets no `command`."""
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 (a variable name, not a value)
"""Where the declared secret goes in the child's environment."""
ENV_SWITCHES: Mapping[str, str] = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
"""Claude Code's own switches, set in every child environment (decision 42 B)."""
MAX_OUTPUT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
"""Where the step's `max_tokens` goes in a call's environment (decision 42 F)."""
CEILING_ENV = "GIT_CEILING_DIRECTORIES"
"""Set to the workspace root: git looks for no repository above it (decision 42 C)."""
LLM_RUNS_DIR = "llm-runs"
"""The per-call workspaces' root, under the instance's state directory (decision 42 C)."""
MANAGED_SETTINGS: tuple[str, ...] = (
    "/etc/claude-code/managed-settings.json",
    "/etc/claude-code/managed-mcp.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
)
"""Administrator-managed settings files: no flag turns them off (they can add hooks, an
environment and MCP servers). `horae doctor` warns when one exists (decision 42 D); their
contents are never read."""
TIMEZONE = "UTC"

EMPTY_MCP = '{"mcpServers":{}}'
"""The only MCP configuration (`--strict-mcp-config` ignores every other)."""
PAYLOAD_KEYS = frozenset({"argv", "stdin", "max_tokens"})

MAX_STDOUT_BYTES = MAX_RESPONSE_BYTES
"""The largest stdout accepted (8 MiB); a call accepts `_shared.response_cap(max_tokens)`."""
MAX_VERSION_BYTES = 4096
VERSION_TIMEOUT_S = 15.0
MAX_SYSTEM_BYTES = 64 << 10
"""The largest system prompt (it goes in `argv`; Linux caps one argument at 128 KiB)."""
MAX_USD = 1e6
"""The largest `total_cost_usd` accepted; a larger one is not usable."""
DRAIN_S = 1.0
"""How long stdout, then stderr, may stay open after the process exited (a grandchild holding
it)."""
_POLL_S = 0.01
_VERSION = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6}) \(Claude Code\)\n?$")
API = "claude-cli"


def argv(model: str, system: str) -> list[str]:
    """The arguments of one call (after the executable)."""
    return [
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP,
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        "1",
        "--system-prompt",
        system,
    ]


_MODEL_AT = 4
_ARGC = len(argv("", ""))


def child_env(
    home: Path,
    tmpdir: Path,
    secret: Secret | None,
    *,
    ceiling: Path,
    max_tokens: int | None = None,
) -> dict[str, str]:
    """The exact environment of a child (the token only when `secret` is given, the output
    limit only when `max_tokens` is)."""
    env = base_env(home=home, tmpdir=tmpdir, timezone=TIMEZONE)
    env.update(ENV_SWITCHES)
    env[CEILING_ENV] = str(ceiling)
    if max_tokens is not None:
        env[MAX_OUTPUT_ENV] = str(max_tokens)
    if secret is not None:
        env[TOKEN_ENV] = secret.reveal()
    return env


def managed_settings() -> list[str]:
    """The `MANAGED_SETTINGS` files that exist (checked by name only, never read)."""
    return [path for path in MANAGED_SETTINGS if os.path.lexists(path)]


def version_ok(version: tuple[int, int, int]) -> bool:
    """Whether `version` is in `CLAUDE_CODE_VERSION_RANGE`."""
    return MIN_VERSION <= version < BELOW_VERSION


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`(major, minor, patch)` of a `claude --version` line, or `None`."""
    match = _VERSION.match(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def resolve(spec: ModelAlias) -> str:
    """The absolute, symlink-free path of the alias' executable; `LlmError
    (backend_unavailable)` when it is missing or not executable."""
    path = spec.command if spec.command is not None else shutil.which(COMMAND_NAME, path=BASE_PATH)
    where = spec.command or f"{COMMAND_NAME} on {BASE_PATH}"
    if path is None:
        raise LlmError("backend_unavailable", f"cannot find {where}")
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise LlmError("backend_unavailable", f"{where} is not an executable file")
    return real


@dataclass
class Outcome:
    """What one process run gave."""

    returncode: int | None
    stdout: bytes
    too_large: bool = False
    timed_out: bool = False


def _private_root(root: Path) -> None:
    """Create `root` (0700) if needed; `LlmError(backend_unavailable)` unless it is a real
    directory of this user (not a symlink), made 0700."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LlmError("backend_unavailable", f"{API}: {LLM_RUNS_DIR} is not a private directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        root.chmod(0o700)


class _Workspace:
    """Fresh empty `home`, `tmp` and `cwd` directories (0700) in a new directory under
    `root` (a private 0700 directory, created if needed; the system temporary directory when
    `None`), removed on exit. `ceiling` is the directory that holds them all."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def __enter__(self) -> "_Workspace":
        if self.root is not None:
            _private_root(self.root)
        self.base = Path(tempfile.mkdtemp(prefix="horae-claude-cli-", dir=self.root))
        self.ceiling = self.base.parent
        self.home, self.tmp, self.cwd = (self.base / name for name in ("home", "tmp", "cwd"))
        for directory in (self.home, self.tmp, self.cwd):
            directory.mkdir(mode=0o700)
        return self

    def __exit__(self, *_: object) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


async def run(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdin: bytes,
    timeout_s: float,
    capture: StderrCapture,
    max_stdout: int = MAX_STDOUT_BYTES,
) -> Outcome:
    """Run `command` (an argument list, no shell) with exactly `env`, in `cwd`, umask 077, in
    its own session; feed it `stdin`; keep at most `max_stdout` bytes of stdout and drain
    stderr into `capture`. After `timeout_s`, or once stdout is too large, the group is
    terminated; it always is on return. Raises `LlmError(backend_unavailable)` when the
    process cannot start."""
    failure: LlmError | None = None
    try:
        process: Process = await anyio.open_process(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            umask=0o077,
        )
    except (OSError, ValueError) as error:
        reason = error.strerror if isinstance(error, OSError) and error.strerror else "invalid"
        failure = LlmError("backend_unavailable", f"cannot start {API}: {reason}")
    if failure is not None:
        raise failure
    out = bytearray()
    flags = {"too_large": False}
    stdout_done, stderr_done = anyio.Event(), anyio.Event()

    async def write_stdin() -> None:
        assert process.stdin is not None  # noqa: S101 (spawned with pipes)
        with suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            if stdin:
                await process.stdin.send(stdin)
            await process.stdin.aclose()

    async def read_stdout() -> None:
        assert process.stdout is not None  # noqa: S101
        try:
            async for chunk in process.stdout:
                out.extend(chunk)
                if len(out) > max_stdout:
                    flags["too_large"] = True
                    return
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        finally:
            stdout_done.set()

    async def read_stderr() -> None:
        assert process.stderr is not None  # noqa: S101
        try:
            async for chunk in process.stderr:
                capture.feed(chunk)
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        finally:
            stderr_done.set()

    timed_out = False
    try:
        with anyio.move_on_after(timeout_s) as scope:
            async with anyio.create_task_group() as group:
                group.start_soon(write_stdin)
                group.start_soon(read_stdout)
                group.start_soon(read_stderr)
                while process.returncode is None and not flags["too_large"]:
                    await anyio.sleep(_POLL_S)
                if not flags["too_large"]:
                    with anyio.move_on_after(DRAIN_S):
                        await stdout_done.wait()
                    with anyio.move_on_after(DRAIN_S):
                        await stderr_done.wait()  # the tail, often the reason of a failure
                group.cancel_scope.cancel()
        timed_out = scope.cancelled_caught
    finally:
        with anyio.CancelScope(shield=True):
            await terminate(process, graceful=False)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    with suppress(OSError, anyio.BrokenResourceError):
                        await pipe.aclose()
            _close_transport(process)
    return Outcome(
        process.returncode,
        bytes(out[:max_stdout]),
        too_large=flags["too_large"],
        timed_out=timed_out,
    )


_NAME = re.compile(r"^[A-Za-z0-9._+/-]{1,128}$")


def script_hint(command: str) -> str:
    """Why `command` may not run in the child environment: a script (`#!`) whose interpreter
    is not found there (the npm package's `#!/usr/bin/env node`, with `node` off
    `BASE_PATH`), else `""`."""
    try:
        with open(command, "rb") as file:
            head = file.read(256)
    except OSError:
        return ""
    if not head.startswith(b"#!"):
        return ""
    words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if not words:
        return ""
    name = words[0]
    if os.path.basename(name) == "env":
        name = next((word for word in words[1:] if not word.startswith("-")), "")
        missing = not name or shutil.which(name, path=BASE_PATH) is None
    else:
        missing = not os.access(name, os.X_OK)
    if not missing:
        return ""
    shown = repr(name) if _NAME.match(name) else "its interpreter"
    return (
        f"; the command is a script run by {shown}, which is not found on the child PATH "
        f"({BASE_PATH}): set `command` to the native Claude Code binary"
    )


async def probe_version(command: str, root: Path | None = None) -> tuple[int, int, int]:
    """`command --version`, in the isolated environment without the token (its workspace
    under `root`); `LlmError(backend_unavailable)` when it fails or is outside
    `CLAUDE_CODE_VERSION_RANGE`."""
    failure: LlmError | None = None
    with _Workspace(root) as work:
        try:
            outcome = await run(
                [command, "--version"],
                env=child_env(work.home, work.tmp, None, ceiling=work.ceiling),
                cwd=work.cwd,
                stdin=b"",
                timeout_s=VERSION_TIMEOUT_S,
                capture=StderrCapture(0),
                max_stdout=MAX_VERSION_BYTES,
            )
        except LlmError as error:
            failure = LlmError(error.kind, error.message + script_hint(command))
    if failure is not None:
        raise failure
    text = utf8(outcome.stdout) if outcome.returncode == 0 and not outcome.timed_out else None
    version = parse_version(text) if text is not None else None
    if version is None:
        raise LlmError(
            "backend_unavailable",
            f"`{COMMAND_NAME} --version` did not give a Claude Code version" + script_hint(command),
        )
    if not version_ok(version):
        shown = ".".join(map(str, version))
        raise LlmError(
            "backend_unavailable",
            f"Claude Code {shown} is outside the supported range {CLAUDE_CODE_VERSION_RANGE}",
        )
    return version


def check(spec: ModelAlias) -> str | None:
    """What `horae doctor` reports for a `claude-cli` alias: `None` when its executable is
    found and its version is in range, else why not (runs `--version`; synchronous)."""
    try:
        command = resolve(spec)
        anyio.run(probe_version, command)
    except LlmError as error:
        return error.message
    return None


def _usd(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    usd = float(value)
    return usd if math.isfinite(usd) and 0 <= usd <= MAX_USD else None


class ClaudeCliBackend:
    """The `claude-cli` provider backend of one model alias (see the module docstring)."""

    def __init__(
        self,
        alias: str,
        spec: ModelAlias,
        secrets: SecretResolver,
        *,
        work_dir: Path | None = None,
    ) -> None:
        self._alias = alias
        self._spec = spec
        self._secrets = secrets
        self._work_dir = Path(os.path.abspath(work_dir)) if work_dir is not None else None
        self._command: str | LlmError | None = None
        self._secret: Secret | None = None

    @property
    def name(self) -> LlmBackendName:
        return "claude-cli"

    @property
    def may_write_cache(self) -> bool:
        """`True`: Claude Code writes prompt caches by itself (decision 41)."""
        return True

    def render(self, completion: Completion) -> dict[str, Any]:
        """The payload for `completion` (pure)."""
        system = system_text(completion)
        if utf8_size(system) > MAX_SYSTEM_BYTES:
            raise LlmError("invalid_request", f"the system prompt is over {MAX_SYSTEM_BYTES} bytes")
        return {
            "argv": argv(completion.model, system),
            "stdin": "\n\n".join(user_texts(completion)),
            "max_tokens": completion.max_tokens,
        }

    async def complete(self, completion: Completion) -> CompletionResult:
        """`send(render(completion))`."""
        return await self.send(self.render(completion), completion.timeout_s)

    @staticmethod
    def _rendered(payload: Mapping[str, Any]) -> bool:
        args, stdin = payload.get("argv"), payload.get("stdin")
        limit = count(payload.get("max_tokens"))
        if set(payload) != PAYLOAD_KEYS or not isinstance(args, list) or len(args) != _ARGC:
            return False
        if not isinstance(stdin, str) or limit is None or limit < 1:
            return False
        model, system = args[_MODEL_AT], args[-1]
        return isinstance(model, str) and isinstance(system, str) and args == argv(model, system)

    def _token(self) -> Secret:
        if self._secret is not None:
            return self._secret
        if self._spec.secret is None:
            raise LlmError(
                "backend_unavailable",
                f"model alias {self._alias!r}: backend 'claude-cli' needs `secret` (a "
                f"{TOKEN_ENV} made with `claude setup-token`; token mode is the only mode)",
            )
        failure: LlmError | None = None
        try:
            self._secret = self._secrets.resolve(self._spec.secret)
        except SecretError as error:
            failure = LlmError("auth_failed", f"model alias {self._alias!r}: {error}")
        if failure is not None or self._secret is None:
            raise failure or LlmError("auth_failed", f"model alias {self._alias!r}: no token")
        if "\x00" in self._secret.reveal():
            self._secret = None
            raise LlmError("auth_failed", f"model alias {self._alias!r}: the token has a NUL")
        return self._secret

    async def _executable(self) -> str:
        """The checked executable (resolved and version-checked once; a failure too)."""
        if self._command is None:
            try:
                command = resolve(self._spec)
                await probe_version(command, self._work_dir)
                self._command = command
            except LlmError as error:
                self._command = error
        if isinstance(self._command, LlmError):
            raise LlmError(self._command.kind, self._command.message)
        return self._command

    def conservative(self, payload: Mapping[str, Any], notional: float | None = None) -> Usage:
        """The charge when no usable usage was reported: the input bound and the whole
        output budget (or the notional cost, when larger)."""
        usage = cost(self._spec.price, input_bound(payload), int(payload["max_tokens"]))
        return self._with_notional(usage, notional)

    @staticmethod
    def _with_notional(usage: Usage, notional: float | None) -> Usage:
        if notional is None:
            return usage
        return usage.model_copy(update={"usd": max(usage.usd, notional), "priced": True})

    def _redactor(self) -> Redactor:
        return Redactor(self._secrets.resolved())

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        """Run the CLI once with exactly `payload` (see the module docstring)."""
        if not self._rendered(payload):
            raise LlmError("invalid_request", "the payload is not one `render` made")
        if self._spec.price is None:
            raise LlmError(
                "backend_unavailable",
                f"model alias {self._alias!r}: backend 'claude-cli' needs `price` (so that "
                "every call has a dollar reservation; decision 42 A)",
            )
        secret = self._token()
        command = await self._executable()
        capture = StderrCapture(CAPTURE_MARGIN)
        max_tokens = int(payload["max_tokens"])
        with _Workspace(self._work_dir) as work:
            env = child_env(
                work.home, work.tmp, secret, ceiling=work.ceiling, max_tokens=max_tokens
            )
            try:
                outcome = await run(
                    [command, *payload["argv"]],
                    env=env,
                    cwd=work.cwd,
                    stdin=str(payload["stdin"]).encode("utf-8"),
                    timeout_s=timeout_s,
                    capture=capture,
                    max_stdout=response_cap(max_tokens),
                )
            finally:
                env.clear()
        stderr = capture.take(self._redactor())
        return self._result(payload, outcome, stderr)

    def _result(
        self, payload: Mapping[str, Any], outcome: Outcome, stderr: str | None
    ) -> CompletionResult:
        if outcome.timed_out:
            raise LlmError("timeout", f"{API} did not answer in time", stderr=stderr)
        if outcome.too_large:
            raise LlmError(
                "output_invalid",
                f"{API} wrote more than {response_cap(int(payload['max_tokens']))} bytes",
                stderr=stderr,
                usage=self.conservative(payload),
            )
        text = utf8(outcome.stdout)
        data = parse(text) if text is not None else INVALID
        if not isinstance(data, dict) or data.get("type") != "result":
            if outcome.returncode != 0:
                raise LlmError(
                    "host_error", f"{API} {exit_status(outcome.returncode)}", stderr=stderr
                )
            what = "not UTF-8" if text is None else "not one JSON result object"
            raise LlmError(
                "output_invalid",
                f"{API} wrote output that is {what}",
                stderr=stderr,
                usage=self.conservative(payload),
            )
        usage = self._usage(data, payload)
        if data.get("is_error") is not False:
            raise self._error(data, usage, stderr)
        if outcome.returncode != 0:
            raise LlmError(
                "host_error", f"{API} {exit_status(outcome.returncode)}", stderr=stderr, usage=usage
            )
        return self._success(data, payload, usage, stderr)

    def _usage(self, data: Mapping[str, Any], payload: Mapping[str, Any]) -> Usage:
        notional = _usd(data.get("total_cost_usd"))
        reported = data.get("usage")
        if not isinstance(reported, dict):
            return self.conservative(payload, notional)
        tokens_in, tokens_out = (
            count(reported.get("input_tokens")),
            count(reported.get("output_tokens")),
        )
        if tokens_in is None or tokens_out is None:
            return self.conservative(payload, notional)
        result = data.get("result")
        usage = floored(
            self._spec.price,
            payload,
            tokens_in,
            tokens_out,
            output_bytes=utf8_size(result) if isinstance(result, str) else 0,
            max_tokens=int(payload["max_tokens"]),
            cache_read=count(reported.get("cache_read_input_tokens")),
            cache_write=count(reported.get("cache_creation_input_tokens")),
        )
        return self._with_notional(usage, notional)

    @staticmethod
    def _spent(data: Mapping[str, Any]) -> bool:
        """Whether a failed call reported any spend (else nothing was processed)."""
        reported = data.get("usage")
        tokens = 0
        if isinstance(reported, dict):
            tokens = (count(reported.get("input_tokens")) or 0) + (
                count(reported.get("output_tokens")) or 0
            )
        return tokens > 0 or (_usd(data.get("total_cost_usd")) or 0) > 0

    def _error(self, data: Mapping[str, Any], usage: Usage, stderr: str | None) -> LlmError:
        status = count(data.get("api_error_status"))
        subtype = token(data.get("subtype"))
        result = data.get("result")
        kind = "host_error"
        if status is not None:
            kind = STATUS_KINDS.get(status, "host_error")
        elif subtype == "error_max_turns":
            kind = "output_invalid"
        elif isinstance(result, str) and result.startswith("Not logged in"):
            kind = "auth_failed"
        details = [f"HTTP {status}"] if status is not None else []
        details += [subtype] if subtype else []
        suffix = f" ({', '.join(details)})" if details else ""
        return LlmError(
            kind,
            f"{API} reported an error{suffix}",
            stderr=stderr,
            usage=usage if self._spent(data) else None,
        )

    def _success(
        self,
        data: Mapping[str, Any],
        payload: Mapping[str, Any],
        usage: Usage,
        stderr: str | None,
    ) -> CompletionResult:
        def invalid(reason: str) -> LlmError:
            return LlmError("output_invalid", f"{API}: {reason}", stderr=stderr, usage=usage)

        if data.get("subtype") != "success":
            raise invalid("the result is not a success")
        if count(data.get("num_turns")) != 1:
            raise invalid("the call did not take exactly one turn")
        denials = data.get("permission_denials")
        if denials is not None and denials != []:
            raise invalid("the model asked for a tool (no tools are allowed)")
        stop = token(data.get("stop_reason"))
        if stop == "tool_use":
            raise invalid("unexpected stop reason tool_use")
        if stop == "max_tokens":
            raise invalid("the output was truncated (max_tokens)")
        reported = data.get("usage")
        tokens_out = count(reported.get("output_tokens")) if isinstance(reported, dict) else None
        if tokens_out is not None and tokens_out > int(payload["max_tokens"]):
            raise invalid("the reported output is over the max_tokens limit")
        result = data.get("result")
        output = parse(result) if isinstance(result, str) else INVALID
        if output is INVALID:
            raise invalid("the result is not one JSON value")
        return CompletionResult(output=output, usage=usage, stop_reason=stop)

    async def aclose(self) -> None:
        self._secret = None


def claude_cli_backend(
    alias: str, spec: ModelAlias, secrets: SecretResolver, *, work_dir: Path | None = None
) -> ClaudeCliBackend:
    """The `BackendFactory` of `claude-cli` (`work_dir`: the per-call workspaces' root,
    `<state_dir>/llm-runs` in a run; see `horae.llm.backends.model_backends`)."""
    return ClaudeCliBackend(alias, spec, secrets, work_dir=work_dir)


__all__ = [
    "BELOW_VERSION",
    "CEILING_ENV",
    "CLAUDE_CODE_VERSION_RANGE",
    "COMMAND_NAME",
    "EMPTY_MCP",
    "ENV_SWITCHES",
    "LLM_RUNS_DIR",
    "MANAGED_SETTINGS",
    "MAX_OUTPUT_ENV",
    "MAX_STDOUT_BYTES",
    "MAX_SYSTEM_BYTES",
    "MIN_VERSION",
    "PAYLOAD_KEYS",
    "TOKEN_ENV",
    "ClaudeCliBackend",
    "Outcome",
    "argv",
    "check",
    "child_env",
    "claude_cli_backend",
    "managed_settings",
    "parse_version",
    "probe_version",
    "resolve",
    "run",
    "script_hint",
    "version_ok",
]
