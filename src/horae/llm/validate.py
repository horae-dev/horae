# SPDX-License-Identifier: Apache-2.0
"""The output validator of the bounded LLM step (#31, schemas decision 44).

`validate_output(output, schema)` is the `ModelBackends(validate=...)` hook (decision 38 G:
mandatory before production use; `horae.llm.backends.model_backends` installs it). It returns
the repair errors of `output` (empty when it is valid):

1. **size**, in this process: the output re-serialised as the skill loader bounds it
   (compact JSON, `ensure_ascii=False`, UTF-8) is at most `MAX_OUTPUT_BYTES`, else the one
   error `SIZE_ERROR` (nothing larger is handed to the worker); an output that is not plain
   JSON is `NOT_JSON_ERROR`;
2. **schema and reserved `intents`**, in a **killable worker**: a short-lived child process
   (`python -I horae/llm/_schema_errors.py`, a clean environment, the request on stdin)
   validates against the skill's full draft 2020-12 schema and writes the errors
   (`horae.llm._schema_errors.schema_errors`: path, keyword and a Horae-written
   description, never a value of the output). The child is killed when it runs past
   `timeout_s` (`TIMEOUT_ERROR`) or when the awaiting task is cancelled (the step's
   deadline), and it sets a CPU-time ceiling on itself as a backstop. A child that fails,
   or answers anything but a short JSON array of short strings, is `WORKER_ERROR`.

Every failure of the validator is an error, never a pass (fail closed), so an output that
could not be validated is never applied. The event loop is never blocked: the parent only
awaits the child's pipes.

Why a process per validation and not a thread or a pool: a thread cannot be stopped, so a
slow validation (a `uniqueItems` over many objects is quadratic; skills may not use `pattern`,
but bounded schemas can still be slow, decision 39 E) would keep a core busy after its
timeout; a process pool cannot kill one task without breaking the pool, and a pool forked from
the running process inherits its state (resolved secrets, open descriptors, threads). A fresh
`-I` child inherits nothing, costs one interpreter start (about half a second with
`jsonschema`) and runs at most twice per `llm` step."""

import asyncio
import contextlib
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from horae.llm._schema_errors import MAX_ERROR_CHARS, MAX_ERRORS
from horae.skills import MAX_OUTPUT_BYTES

VALIDATION_TIMEOUT_S = 10.0
"""Longest a validation may take (the child's start included) before it is killed."""
MAX_WORKER_OUTPUT = 64 * 1024
"""Most bytes accepted from the worker's stdout."""

WORKER = Path(__file__).with_name("_schema_errors.py")
"""The worker script (stdlib and `jsonschema` only)."""

TIMEOUT_ERROR = "/: timeout: the output could not be validated in time"
WORKER_ERROR = "/: validator: the output could not be validated"
SIZE_ERROR = f"/: maxBytes: the output is larger than {MAX_OUTPUT_BYTES} bytes"
NOT_JSON_ERROR = "/: json: the output is not plain JSON"


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


async def _run_worker(request: bytes, timeout_s: float) -> tuple[str, ...]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(WORKER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={},
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, _ = await process.communicate(request)
    except TimeoutError:
        return (TIMEOUT_ERROR,)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.shield(process.wait())
    if process.returncode != 0 or len(stdout) > MAX_WORKER_OUTPUT:
        return (WORKER_ERROR,)
    return _answer(stdout)


async def validate_output(
    output: Any, schema: Mapping[str, Any], *, timeout_s: float = VALIDATION_TIMEOUT_S
) -> tuple[str, ...]:
    """The repair errors of `output` against the skill's full output `schema` (see the module
    docstring); empty when it is valid."""
    serialised = _serialised(output)
    if serialised is None:
        return (NOT_JSON_ERROR,)
    if len(serialised) > MAX_OUTPUT_BYTES:
        return (SIZE_ERROR,)
    try:
        request = json.dumps(
            {"output": output, "schema": dict(schema), "cpu_s": math.ceil(timeout_s) + 1},
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        return (WORKER_ERROR,)
    try:
        return await _run_worker(request, timeout_s)
    except OSError:
        return (WORKER_ERROR,)


__all__ = [
    "MAX_WORKER_OUTPUT",
    "NOT_JSON_ERROR",
    "SIZE_ERROR",
    "TIMEOUT_ERROR",
    "VALIDATION_TIMEOUT_S",
    "WORKER",
    "WORKER_ERROR",
    "validate_output",
]
