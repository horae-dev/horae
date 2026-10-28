# SPDX-License-Identifier: Apache-2.0
"""`horae undo <run-id>` (#22): restore every note a run changed (`horae.apply.undo`).

Registered on the `horae` app by `horae.cli`. Notes are written with the provisional local
sink (`LocalFileSink`, `--vault`) until plugin sinks exist (#24). The pipeline's lock is held
while undoing, so a run of the same pipeline cannot apply changes meanwhile.

Exit codes follow `horae diff`: 0 undone (or nothing left to undo), 1 configuration, store,
lock or sink problem (what was restored stays restored; run it again), 2 usage error, 3
unknown, running or dry run, 4 refused because a note changed since the run (nothing
written; `--force` restores it anyway), 70 internal error.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from horae.apply import LocalFileSink, StaticSinks, undo_run
from horae.apply.render import safe
from horae.apply.undo import UNDO_SCHEMA_VERSION, UndoResult, render_undo_text, undo_to_dict
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.runner import PipelineLock
from horae.store import Store, StoreError, UnknownRun

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_UNKNOWN_RUN = 3
"""Unknown run, still running, or a dry run."""
EXIT_CHANGED = 4
"""Refused: a note changed since the run (use `--force`)."""
EXIT_INTERNAL = 70


class _UndoFailed(Exception):
    def __init__(self, code: int, kind: str, message: str) -> None:
        super().__init__(kind)
        self.code, self.kind, self.message = code, kind, message


def _error(kind: str, message: str, json_output: bool) -> None:
    if json_output:
        error = {"schema_version": UNDO_SCHEMA_VERSION, "error": {"kind": kind, "message": message}}
        typer.echo(json.dumps(error, indent=2, ensure_ascii=True))
    else:
        typer.echo(f"horae undo: {safe(message)}", err=True)


def _code(result: UndoResult) -> int:
    if result.refused is not None:
        return EXIT_CHANGED if result.refused.kind == "changed" else EXIT_UNKNOWN_RUN
    return EXIT_OK if result.complete else EXIT_PROBLEMS


def _undo(instance: Path, run_id: str, vault: Path | None, force: bool) -> UndoResult:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        message = "; ".join([*map(str, exc.problems), f"{len(exc.problems)} problem(s) found"])
        raise _UndoFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    state_dir = instance / config.state_dir
    path = state_dir / STORE_FILE
    if not path.exists():  # never create the store just to look a run up
        raise _UndoFailed(EXIT_UNKNOWN_RUN, "unknown_run", f"unknown run {run_id!r} (no store)")
    sinks = StaticSinks(default=LocalFileSink(vault))
    try:
        with Store.open(path) as store:
            summary = store.run_summary(run_id)
            if summary is None:
                raise UnknownRun(run_id)
            lock = PipelineLock.try_acquire(state_dir, summary.pipeline)
            if lock is None:
                message = f"pipeline {summary.pipeline!r} is running; undo it when it has finished"
                raise _UndoFailed(EXIT_PROBLEMS, "pipeline_locked", message)
            with lock:
                return asyncio.run(
                    undo_run(store, run_id, sinks, force=force, clock=lambda: datetime.now(UTC))
                )
    except UnknownRun:
        raise _UndoFailed(EXIT_UNKNOWN_RUN, "unknown_run", f"unknown run {run_id!r}") from None
    except (StoreError, OSError) as exc:
        raise _UndoFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None


def undo(
    run_id: Annotated[str, typer.Argument(help="The run to undo.")],
    instance: Annotated[
        Path,
        typer.Option(
            "--instance",
            "-i",
            envvar="HORAE_INSTANCE",
            help="Instance directory (holds horae.yaml).",
            file_okay=False,
        ),
    ] = Path(),
    vault: Annotated[
        Path | None,
        typer.Option(
            "--vault",
            help="Vault directory the run's notes are in (provisional until #24).",
            file_okay=False,
            exists=True,
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Restore notes that changed since the run too.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Restore every note a run changed, byte for byte, and mark the run undone.

    A note the run created is removed. Refused when a note changed since the run, unless
    --force. Tasks, notifications and HTTP calls cannot be undone: they are listed. Running
    it again after an interruption continues where it stopped.

    Exit codes: 0 undone, 1 configuration, store, lock or sink problem, 2 usage error,
    3 unknown, running or dry run, 4 refused (a note changed since the run), 70 internal
    error. With --json, errors are a JSON object on stdout ({schema_version, error})."""
    try:
        result = _undo(instance, run_id, vault, force)
    except _UndoFailed as failed:
        _error(failed.kind, failed.message, json_output)
        raise typer.Exit(failed.code) from None
    except Exception as exc:
        # Never print the exception, its traceback or its locals: a frame may hold a secret.
        name = type(exc).__name__
        del exc
        _error("internal", f"internal error ({name}); please report it", json_output)
        raise typer.Exit(EXIT_INTERNAL) from None
    if json_output:
        typer.echo(json.dumps(undo_to_dict(result), indent=2, ensure_ascii=True))
    else:
        typer.echo(render_undo_text(result), nl=False)
    raise typer.Exit(_code(result))
