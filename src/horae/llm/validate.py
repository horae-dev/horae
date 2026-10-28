# SPDX-License-Identifier: Apache-2.0
"""The output validator of the bounded LLM step (#31, schemas decision 44).

`validate_output(output, schema, timeout_s=...)` is the default `ModelBackends.validate`
(decision 38 G: mandatory in production; decision 44 D). It returns the repair errors of
`output` (empty when it is valid), or raises `LlmError` when the output **could not be
validated** (`ModelBackends` reports it as `validator_unavailable`, with the attempt's usage:
no repair, the step degrades at once; decision 44 C):

1. **size**, in this process: the output re-serialised as the skill loader bounds it
   (compact JSON, `ensure_ascii=False`, UTF-8) is at most `MAX_OUTPUT_BYTES`, else the one
   repair error `SIZE_ERROR` (nothing larger is handed to the worker); an output that is not
   plain JSON cannot be validated (`NOT_JSON_ERROR`);
2. **schema and reserved `intents`**, in a **killable worker**: a short-lived child process
   (`python -I -B horae/llm/_schema_errors.py`, an empty environment, `cwd` `/`, a new
   session, the request on stdin, stderr discarded, no other descriptor) validates against
   the skill's full draft 2020-12 schema and writes the errors
   (`horae.llm._schema_errors.schema_errors`: path, keyword and a Horae-written description,
   never a value of the output). The child sets its own CPU and memory ceilings
   (`RLIMIT_CPU`; `RLIMIT_AS`, or `RLIMIT_DATA`). It is killed when it runs past the
   timeout (`TIMEOUT_ERROR`), when the awaiting task is cancelled (the step's deadline) or
   when it writes more than `MAX_WORKER_OUTPUT` bytes; a child that fails (a non-zero exit:
   out of memory, say) or answers anything but a short JSON array of short strings is
   `WORKER_ERROR`; an unusable schema is `SCHEMA_ERROR`.

The timeout is `min(timeout_s, VALIDATION_TIMEOUT_S)`: `ModelBackends` passes what is left of
the attempt's deadline (less a margin), so the validation gives up, and reports the attempt's
usage, before the engine's deadline cancels the step (decision 44 E). Every failure of the
validator is an error, never a pass (fail closed). The event loop is never blocked: the
parent only awaits the child's pipes.

Why a process per validation and not a thread or a pool: a thread cannot be stopped, so a
slow validation (a `uniqueItems` over many objects is quadratic; skills may not use `pattern`,
but bounded schemas can still be slow, decision 39 E) would keep a core busy after its
timeout; a process pool cannot kill one task without breaking the pool, and a pool forked from
the running process inherits its state (resolved secrets, open descriptors, threads). A fresh
`-I` child inherits nothing, costs one interpreter start (about half a second with
`jsonschema`) and runs at most twice per `llm` step.

`worker_problem()` is `horae doctor`'s check: the interpreter and the worker script exist and
a trivial validation gives the expected error."""

import asyncio
import contextlib
import json
import math
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from horae.engine.ports import LlmError
from horae.llm._schema_errors import MAX_ERROR_CHARS, MAX_ERRORS, SCHEMA_ERROR
from horae.llm.base import VALIDATOR_UNAVAILABLE
from horae.skills import MAX_OUTPUT_BYTES

VALIDATION_TIMEOUT_S = 10.0
"""Longest a validation may take (the child's start included) before it is killed."""
MAX_WORKER_OUTPUT = 64 * 1024
"""Most bytes accepted from the worker's stdout (read with that bound: a longer answer is
not read, the worker is killed)."""

WORKER = Path(__file__).with_name("_schema_errors.py")
"""The worker script (stdlib and `jsonschema` only)."""
WORKER_FLAGS = ("-I", "-B")
"""Isolated mode (no `PYTHON*` variables, no user site, not the script's folder on the path)
and no bytecode written."""
WORKER_CWD = "/"
"""The worker's working directory: nothing of the instance's."""

TIMEOUT_ERROR = "/: timeout: the output could not be validated in time"
WORKER_ERROR = "/: validator: the output could not be validated"
SIZE_ERROR = f"/: maxBytes: the output is larger than {MAX_OUTPUT_BYTES} bytes"
NOT_JSON_ERROR = "/: json: the output is not plain JSON"
UNAVAILABLE_ERRORS = frozenset({TIMEOUT_ERROR, WORKER_ERROR, SCHEMA_ERROR, NOT_JSON_ERROR})
"""The errors that mean "could not be validated" (raised as `LlmError`, never a repair)."""

_SELF_CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["a"],
    "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 9}},
}
_SELF_CHECK_ERRORS = ("/: required: missing required property a",)


def _unavailable(error: str) -> LlmError:
    return LlmError(VALIDATOR_UNAVAILABLE, error)


def _serialised(output: Any) -> bytes | None:
    try:
        return json.dumps(
            output, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return None


def _answer(data: bytes) -> tuple[str, ...]:
    """The worker's errors, checked: a JSON array of at most `MAX_ERRORS` short strings."""
    try:
        errors = json.loads(data)
    except ValueError:
        return (WORKER_ERROR,)
    if not isinstance(errors, list) or len(errors) > MAX_ERRORS:
        return (WORKER_ERROR,)
    if not all(isinstance(e, str) and 0 < len(e) <= MAX_ERROR_CHARS for e in errors):
        return (WORKER_ERROR,)
    return tuple(errors)


def _command() -> list[str]:
    return [sys.executable, *WORKER_FLAGS, str(WORKER)]


def _request(output: Any, schema: Mapping[str, Any], timeout_s: float) -> bytes | None:
    try:
        return json.dumps(
            {"output": output, "schema": dict(schema), "cpu_s": math.ceil(timeout_s) + 1},
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        return None


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes | None:
    """At most `limit` bytes of `stream` up to its end, or `None` when it holds more."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(limit + 1 - size):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return None
    return b"".join(chunks)


async def _exchange(process: asyncio.subprocess.Process, request: bytes) -> bytes | None:
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:  # pragma: no cover (both are pipes)
        return None
    stdin.write(request)
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await stdin.drain()
    stdin.close()
    answer = await _read_bounded(stdout, MAX_WORKER_OUTPUT)
    if answer is None:
        return None
    await process.wait()
    return answer


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Wait for a killed worker: what is left in its stdout pipe (at most a pipe buffer, the
    writer is dead) is read and discarded first, since asyncio only reports the exit once
    every pipe is closed, and a reader paused on a full buffer never sees the end."""
    if process.stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            while await process.stdout.read(MAX_WORKER_OUTPUT):
                pass
    await process.wait()


async def _run_worker(request: bytes, timeout_s: float) -> tuple[str, ...]:
    process = await asyncio.create_subprocess_exec(
        *_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={},
        cwd=WORKER_CWD,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout = await _exchange(process, request)
    except TimeoutError:
        return (TIMEOUT_ERROR,)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.shield(_reap(process))
    if stdout is None or process.returncode != 0:
        return (WORKER_ERROR,)
    return _answer(stdout)


async def validate_output(
    output: Any, schema: Mapping[str, Any], *, timeout_s: float = VALIDATION_TIMEOUT_S
) -> tuple[str, ...]:
    """The repair errors of `output` against the skill's full output `schema` (see the module
    docstring); empty when it is valid. Raises `LlmError` (`validator_unavailable`) when the
    output could not be validated within `min(timeout_s, VALIDATION_TIMEOUT_S)`."""
    serialised = _serialised(output)
    if serialised is None:
        raise _unavailable(NOT_JSON_ERROR)
    if len(serialised) > MAX_OUTPUT_BYTES:
        return (SIZE_ERROR,)
    timeout_s = min(timeout_s, VALIDATION_TIMEOUT_S)
    if not timeout_s > 0:
        raise _unavailable(TIMEOUT_ERROR)
    request = _request(output, schema, timeout_s)
    if request is None:
        raise _unavailable(WORKER_ERROR)
    try:
        errors = await _run_worker(request, timeout_s)
    except OSError:
        errors = (WORKER_ERROR,)
    unavailable = [error for error in errors if error in UNAVAILABLE_ERRORS]
    if unavailable:
        raise _unavailable(unavailable[0])
    return errors


def worker_problem(timeout_s: float = VALIDATION_TIMEOUT_S) -> str | None:
    """Why the validation worker cannot run here (`horae doctor`), or `None`: the interpreter
    (`sys.executable`) and the worker script exist, and a trivial validation, run exactly as
    `validate_output` runs it, gives the expected error."""
    if not sys.executable or not Path(sys.executable).is_file():
        return f"the Python interpreter {sys.executable!r} does not exist"
    if not WORKER.is_file():
        return f"the validation worker {WORKER} does not exist"
    request = _request({}, _SELF_CHECK_SCHEMA, timeout_s)
    if request is None:  # pragma: no cover (a constant request)
        return "the validation worker's self-check cannot be encoded"
    try:
        done = subprocess.run(  # noqa: S603 (a fixed argv, never a shell)
            _command(),
            input=request,
            capture_output=True,
            env={},
            cwd=WORKER_CWD,
            start_new_session=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"the validation worker did not answer within {timeout_s:g} s"
    except OSError as error:
        return f"the validation worker cannot start ({type(error).__name__})"
    if done.returncode != 0 or len(done.stdout) > MAX_WORKER_OUTPUT:
        return f"the validation worker failed (exit status {done.returncode})"
    if _answer(done.stdout) != _SELF_CHECK_ERRORS:
        return "the validation worker gave an unexpected answer to its self-check"
    return None


__all__ = [
    "MAX_WORKER_OUTPUT",
    "NOT_JSON_ERROR",
    "SIZE_ERROR",
    "TIMEOUT_ERROR",
    "UNAVAILABLE_ERRORS",
    "VALIDATION_TIMEOUT_S",
    "WORKER",
    "WORKER_CWD",
    "WORKER_ERROR",
    "WORKER_FLAGS",
    "validate_output",
    "worker_problem",
]
