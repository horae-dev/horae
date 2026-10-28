# SPDX-License-Identifier: Apache-2.0
"""A fake `claude` executable for the `claude-cli` backend tests (#29): a Python script that
records each invocation (argv, environment, working directory, umask, stdin, process group,
what its `HOME` and working directory hold) as one JSON line in `calls.jsonl`, then behaves as
`behaviour.json` says: answer `--version`, print a canned JSON result, raw bytes, a flood, or
sleep (with a child process) until it is killed. It honours `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
as Claude Code does: with `over_limit: N`, it reports `output_tokens` N above that limit
(`0` reports exactly the limit), so a test sees the limit it was given echoed back.

No network and no real Claude Code: the script never reads anything but its own files."""

import base64
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "2.1.284 (Claude Code)"
"""What the fake prints for `--version` by default (in `CLAUDE_CODE_VERSION_RANGE`)."""

SCRIPT = """#!{python} -I
import base64, json, os, stat, subprocess, sys, time
HERE = {here!r}
with open(os.path.join(HERE, "behaviour.json"), encoding="utf-8") as f:
    b = json.load(f)
umask = os.umask(0)
os.umask(umask)
if sys.argv[1:] == ["--version"]:
    with open(os.path.join(HERE, "versions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({{"env": dict(os.environ), "argv": sys.argv}}) + "\\n")
    sys.stdout.write(b["version"] + "\\n")
    sys.exit(b.get("version_exit", 0))
data = sys.stdin.buffer.read()
record = {{
    "argv": sys.argv,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(".")),
    "home_entries": sorted(os.listdir(os.environ.get("HOME", "/nonexistent"))),
    "home_mode": stat.S_IMODE(os.stat(os.environ["HOME"]).st_mode),
    "tmp_mode": stat.S_IMODE(os.stat(os.environ["TMPDIR"]).st_mode),
    "cwd_mode": stat.S_IMODE(os.stat(".").st_mode),
    "umask": umask,
    "stdin": data.decode("utf-8"),
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
    "sid": os.getsid(0),
}}
open(os.path.join(os.environ["HOME"], "written-by-fake"), "w").close()
with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\\n")
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
stderr = b.get("stderr", "").replace("{{TOKEN}}", token)
if stderr:
    sys.stderr.write(stderr)
    sys.stderr.flush()
mode = b.get("mode", "result")
result = b["result"]
if b.get("over_limit") is not None and isinstance(result.get("usage"), dict):
    limit = int(os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"])
    result["usage"]["output_tokens"] = limit + b["over_limit"]
if mode == "result":
    text = json.dumps(result).replace("{{TOKEN}}", token)
    sys.stdout.write(text + "\\n")
elif mode == "raw":
    sys.stdout.buffer.write(base64.b64decode(b["raw"]))
elif mode == "flood":
    chunk = b"x" * 65536
    for _ in range(b["chunks"]):
        sys.stdout.buffer.write(chunk)
    sys.stdout.flush()
    time.sleep(30)
elif mode == "sleep":
    child = subprocess.Popen(["/bin/sleep", "60"])
    with open(os.path.join(HERE, "pids.json"), "w", encoding="utf-8") as f:
        json.dump([os.getpid(), child.pid], f)
    time.sleep(60)
sys.stdout.flush()
after = b.get("stderr_after", "")
if after:
    # a grandchild keeps stderr open and writes the tail after this process has exited
    code = "import sys, time; time.sleep(0.3); sys.stderr.write(sys.argv[1])"
    subprocess.Popen([sys.executable, "-I", "-c", code, after], stdout=subprocess.DEVNULL)
sys.exit(b.get("exit", 0))
"""


def success(output: Any, **overrides: Any) -> dict[str, Any]:
    """A result object shaped like Claude Code 2.1.284's `--output-format json`."""
    result: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 1200,
        "duration_api_ms": 1100,
        "num_turns": 1,
        "result": output if isinstance(output, str) else json.dumps(output),
        "stop_reason": "end_turn",
        "session_id": "00000000-0000-4000-8000-000000000000",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 1200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 45,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
        },
        "modelUsage": {},
        "permission_denials": [],
        "terminal_reason": "completed",
        "uuid": "00000000-0000-4000-8000-000000000001",
    }
    result.update(overrides)
    return result


NOT_LOGGED_IN = success(
    "Not logged in · Please run /login",
    is_error=True,
    total_cost_usd=0,
    usage={"input_tokens": 0, "output_tokens": 0},
    stop_reason="stop_sequence",
    terminal_reason="api_error",
)
"""What Claude Code 2.1.284 printed, checked locally, with an empty `HOME` and no token."""


@dataclass
class Fake:
    """One fake `claude` in `directory`."""

    directory: Path

    @property
    def command(self) -> str:
        return str(self.directory / "claude")

    def behave(self, **behaviour: Any) -> None:
        data = {"version": VERSION, "result": success({"items": []}), **behaviour}
        if isinstance(data.get("raw"), bytes):
            data["raw"] = base64.b64encode(data["raw"]).decode("ascii")
        (self.directory / "behaviour.json").write_text(json.dumps(data), encoding="utf-8")

    def calls(self) -> list[Mapping[str, Any]]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def versions(self) -> list[Mapping[str, Any]]:
        path = self.directory / "versions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def pids(self) -> list[int]:
        return list(json.loads((self.directory / "pids.json").read_text(encoding="utf-8")))


def make_fake(directory: Path, **behaviour: Any) -> Fake:
    """Write a fake `claude` (executable) into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude"
    script.write_text(SCRIPT.format(python=sys.executable, here=str(directory)), encoding="utf-8")
    script.chmod(0o755)
    fake = Fake(directory)
    fake.behave(**behaviour)
    return fake
