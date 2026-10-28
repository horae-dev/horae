# SPDX-License-Identifier: Apache-2.0
"""`horae undo <run-id>` (#22, wired to the plugin host in #26): restore every note a run
changed (`horae.apply.undo`).

Registered on the `horae` app by `horae.cli`. Notes are restored through the sink that
applied them: the instance's plugins (`open_ports`, `McpSinks`; the core prepares their
identities) when it has a `plugins/` directory (`horae.cli_host`), so a run applied by
`file-sink` is undone by `file-sink`. `--vault` is the explicit override: the built-in local
sink (`LocalFileSink`) on that directory, for runs applied with it; its identity differs
from a plugin's, so it refuses (`sink_mismatch`) a run applied through a plugin. The
pipeline's lock and the instance's apply lock (`<state_dir>/locks/_apply.lock`) are held
while undoing, so no run can apply changes meanwhile. With the pipeline's lock held, its
`running` rows are dead (their process was killed): they are abandoned first
(`horae.runner.recover_pipeline`), so a run killed mid-apply can be undone at once.

Exit codes (`horae.cli_output`): 0 undone (or nothing left to undo), 1 configuration,
store, plugin or sink problem, or another vault than the run's (`sink_mismatch`; what was
restored stays restored; run it again), 2 usage error, 3 unknown, running or dry run, or a
run with no note to undo, 4 refused because a note changed since the run or a later run
changed it too (nothing written; `--force` restores it anyway), 5 the pipeline is running
or another process holds the apply lock, 70 internal error.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from horae.apply import LocalFileSink, SinkRegistry, StaticSinks, undo_run
from horae.apply.undo import UndoResult, render_undo_text, undo_to_dict
from horae.cli_host import host_sinks, load, uses_host
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_INTERNAL,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    CommandFailed,
    echo_json,
    io_failed,
    plural,
    run_command,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.runner import PipelineLock, recover_pipeline, try_acquire_apply_lock
from horae.store import Store, StoreError, UnknownRun

EXIT_UNKNOWN_RUN = EXIT_REFUSED
"""Unknown run, still running, a dry run, or nothing to undo."""
EXIT_CHANGED = EXIT_DIFFERENT
"""Refused: a note changed since the run, or a later run changed it (use `--force`)."""

__all__ = [
    "EXIT_CHANGED",
    "EXIT_INTERNAL",
    "EXIT_LOCKED",
    "EXIT_OK",
    "EXIT_PROBLEMS",
    "EXIT_UNKNOWN_RUN",
    "undo",
]


def _code(result: UndoResult) -> int:
    if result.refused is not None:
        if result.refused.kind in {"changed", "newer_run"}:
            return EXIT_CHANGED
        return EXIT_PROBLEMS if result.refused.kind == "sink_mismatch" else EXIT_UNKNOWN_RUN
    return EXIT_OK if result.complete else EXIT_PROBLEMS


def _now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def _sinks(instance: Path, vault: Path | None) -> AsyncIterator[SinkRegistry]:
    if uses_host(instance, vault):
        async with host_sinks(load(instance)) as sinks:
            yield sinks
    else:
        yield StaticSinks(default=LocalFileSink(vault))


def _undo(instance: Path, run_id: str, vault: Path | None, force: bool) -> UndoResult:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        found = plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    state_dir = instance / config.state_dir
    path = state_dir / STORE_FILE
    if not path.exists():  # never create the store just to look a run up
        raise CommandFailed(EXIT_UNKNOWN_RUN, "unknown_run", f"unknown run {run_id!r} (no store)")
    try:
        with Store.open(path) as store:
            summary = store.run_summary(run_id)
            if summary is None:
                raise UnknownRun(run_id)
            lock = PipelineLock.try_acquire(state_dir, summary.pipeline)
            if lock is None:
                message = f"pipeline {summary.pipeline!r} is running; undo it when it has finished"
                raise CommandFailed(EXIT_LOCKED, "pipeline_locked", message)
            with lock:
                applying = try_acquire_apply_lock(state_dir)
                if applying is None:
                    message = "another process is applying changes; try again when it has finished"
                    raise CommandFailed(EXIT_LOCKED, "apply_locked", message)
                with applying:
                    recover_pipeline(store, summary.pipeline, clock=_now)

                    async def restore() -> UndoResult:
                        async with _sinks(instance, vault) as sinks:
                            return await undo_run(
                                store,
                                run_id,
                                sinks,
                                force=force,
                                clock=_now,
                                call_timeout_s=config.plugin_defaults.call_timeout_s,
                            )

                    return asyncio.run(restore())
    except UnknownRun:
        raise CommandFailed(EXIT_UNKNOWN_RUN, "unknown_run", f"unknown run {run_id!r}") from None
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, state_dir, store=path) from None


def _report(instance: Path, run_id: str, vault: Path | None, force: bool, json: bool) -> int:
    result = _undo(instance, run_id, vault, force)
    if json:
        echo_json(undo_to_dict(result))
    else:
        typer.echo(render_undo_text(result), nl=False)
    return _code(result)


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
            help=(
                "Restore notes in this local vault with the built-in local sink, instead of "
                "through the instance's plugins."
            ),
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

    Notes are restored through the instance's plugins (<instance>/plugins) when it has
    them, else (or with --vault) with the built-in local sink on --vault. A note the run
    created is removed. Refused when a note changed since the run, or a later run changed it
    too, unless --force (the overwritten bytes are kept in the journal); always refused when
    the sink is another vault than the run's. Tasks, notifications and HTTP calls cannot be
    undone: they are listed. Running it again after an interruption continues where it
    stopped.

    Exit codes: 0 undone, 1 configuration, store, plugin or sink problem (or another vault),
    2 usage error, 3 unknown, running or dry run (or nothing to undo), 4 refused (a note
    changed since the run, or by a later run), 5 pipeline running or apply lock busy, 70
    internal error. With --json, errors are a JSON object on stdout ({schema_version,
    error})."""
    run_command("undo", json_output, lambda: _report(instance, run_id, vault, force, json_output))
