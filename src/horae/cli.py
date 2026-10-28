# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae (#26): init, run, runs, diff, undo, replay, doctor,
usage.

Every command shares the exit codes, error reporting and `--json` conventions of
`horae.cli_output` (schemas decision 37): 0 ok, 1 problems, 2 usage, 3 unknown or refused,
4 different, 5 locked, 70 internal, 130/143 interrupted (`horae run`)."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from horae import __version__
from horae.apply import LocalSink, SinkRegistry, StaticSinks, plan_changes, to_json
from horae.apply.render import render_text
from horae.cli_host import PLUGINS_DIR, host_sinks, instance_problems, load, uses_host
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_INTERNAL,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE,
    SCHEMA_VERSION,
    CommandFailed,
    JsonUsageCommand,
    JsonUsageGroup,
    echo,
    echo_json,
    internal_message,
    io_failed,
    plural,
    prime_secrets,
    report_error,
    run_command,
    safe,
)
from horae.config import (
    CONFIG_FILE,
    STORE_FILE,
    InstanceError,
    ProblemsError,
    check_instance,
    load_instance,
    read_yaml,
    validate,
)
from horae.contracts import InstanceConfig, RunRecord
from horae.replay import LockRegistry, ReplayResult, definition_for, replay
from horae.runner import lock_is_free
from horae.secrets import logs
from horae.skills import DirectorySkills
from horae.store import MAX_LIST_RUNS, RunSummary, Store, StoreError

EXIT_CODES_HELP = (
    "Exit codes (every command): 0 ok, 1 problems (configuration, store, plugins, a failed "
    "run), 2 usage error, 3 unknown or refused (run, pipeline, init target; not comparable), "
    "4 different (replay differs, plan or undo refused by changes), 5 locked (pipeline or "
    "apply lock), 70 internal error, 130/143 interrupted by SIGINT/SIGTERM (horae run). With "
    "--json every command prints one JSON document on stdout (schema_version 1), errors too."
)

app = typer.Typer(
    name="horae",
    help="Horae: a predictable, self-hosted personal agent.",
    epilog=EXIT_CODES_HELP,
    no_args_is_help=True,
    add_completion=False,
    # `horae --json` and `horae nope --json`: JSON usage errors too.
    cls=JsonUsageGroup,
    # Never print local variables in tracebacks: they may hold secret values.
    pretty_exceptions_show_locals=False,
)


def _command(name: str | None = None) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """`app.command`, with JSON usage errors under `--json` (`JsonUsageCommand`)."""
    return app.command(name, cls=JsonUsageCommand)


def _version_callback(value: bool) -> None:
    if value:
        echo(f"horae {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Horae: a predictable, self-hosted personal agent."""
    logs.install()  # every command's logs are scrubbed of secret values (#53)


_plural = plural

EXIT_UNKNOWN_RUN = EXIT_REFUSED
"""`horae diff` / `horae replay`: the run is unknown, still running, or refused."""
EXIT_NOT_COMPARABLE = EXIT_REFUSED
"""`horae replay`: the run could not be compared (see `replay_command`)."""
EXIT_PLANNED_WITH_PROBLEMS = EXIT_DIFFERENT
"""`horae diff`: the run is planned, but some intents cannot be applied."""

REPORT_SCHEMA_VERSION = SCHEMA_VERSION
"""Version of the `horae doctor --json` report."""


def _report(instance: Path, json_output: bool) -> int:
    result = check_instance(instance)
    problems = list(result.problems)
    if result.instance is not None:
        result.instance.prime_redaction()
        problems.extend(instance_problems(result.instance))
    ok = not any(problem.severity == "error" for problem in problems)
    warnings = sum(problem.severity == "warning" for problem in problems)
    if json_output:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": ok,
            "instance": str(instance.resolve()),
            "problems": [problem.to_dict() for problem in problems],
        }
        echo_json(report)
    else:
        for problem in problems:
            echo(safe(str(problem)))
        if ok and result.instance is not None:
            loaded = result.instance
            echo(
                f"OK: {_plural(len(loaded.pipelines), 'pipeline')}, "
                f"{_plural(len(loaded.lock.plugins), 'plugin')}, "
                f"{_plural(len(loaded.config.models), 'model alias', 'model aliases')}"
                + (f" ({_plural(warnings, 'warning')})" if warnings else "")
            )
        else:
            echo(_plural(len(problems) - warnings, "problem") + " found")
    return EXIT_OK if ok else EXIT_PROBLEMS


InstanceDir = Annotated[
    Path,
    typer.Option(
        "--instance",
        "-i",
        envvar="HORAE_INSTANCE",
        help="Instance directory (holds horae.yaml).",
        file_okay=False,
    ),
]
"""The `--instance` option of every command but `init`."""


@_command()
def doctor(
    instance: InstanceDir = Path(),
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Check the instance configuration, pipelines, lockfile, secrets and installed plugins.

    Also checks that every path of horae.lock exists, that the instance's own vault/ is the
    one horae.lock writes to (instances are not relocatable in v1: a copied or moved
    instance is reported), and, with a plugins/ directory, that the plugins are installed
    as horae.lock pins them and their commands (interpreters) are executable. With an llm
    step, it also checks that the LLM output validator (a child process) runs.

    Warnings (e.g. a cloud model alias without a price, whose usd budget is then
    ineffective) are reported but do not fail the check.

    Exit codes: 0 no problem (warnings allowed), 1 problems found, 2 usage error, 70 internal
    error.
    Secret values are never printed. With --json: {schema_version, ok, instance, problems};
    an error is {schema_version, error}."""
    run_command("doctor", json_output, lambda: _report(instance, json_output))


RUNS_SCHEMA_VERSION = 1
"""Version of the `horae runs --json` report."""


class RunStatusFilter(StrEnum):
    running = "running"
    ok = "ok"
    noop = "noop"
    degraded = "degraded"
    failed = "failed"
    undone = "undone"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(value: timedelta | None) -> str:
    if value is None:
        return "-"
    seconds = value.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


MAX_ERROR_COLUMN = 60
"""Characters of a run's error shown in the `horae runs` table (`--json` has all of it)."""
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")


def _reason(error: str | None) -> str:
    """A run's error for one table cell: control and bidi characters collapsed, capped."""
    if not error:
        return ""
    text = " ".join(_UNSAFE.sub(" ", error).split())
    return text if len(text) <= MAX_ERROR_COLUMN else text[: MAX_ERROR_COLUMN - 1] + "…"


def _run_json(run: RunSummary, stale: bool) -> dict[str, Any]:
    duration = run.duration
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "stale": stale,
        "dry_run": run.dry_run,
        "abandoned": run.abandoned_at is not None,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_s": None if duration is None else duration.total_seconds(),
        "cost": {
            "usd": run.cost.usd,
            "tokens_in": run.cost.tokens_in,
            "tokens_out": run.cost.tokens_out,
        },
        "error": run.error,
    }


def _run_row(run: RunSummary, stale: bool) -> list[str]:
    status = str(run.status)
    if stale:
        status += " (stale?)"
    if run.abandoned_at is not None:
        status += " (abandoned)"
    if run.dry_run:
        status += " (dry run)"
    return [
        run.id,
        run.pipeline,
        status,
        _iso(run.started_at) or "",
        _duration(run.duration),
        f"${run.cost.usd:.4f}",
        f"{run.cost.tokens_in}/{run.cost.tokens_out}",
        _reason(run.error),
    ]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]


def _config(instance: Path) -> InstanceConfig:
    """`horae.yaml` alone (enough for the store and the locks), or `config_invalid`. The
    secrets it names are resolved for redaction (`prime_secrets`)."""
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        found = _plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    prime_secrets(instance, config)
    return config


def _list_runs(
    instance: Path,
    pipeline: str | None,
    status: RunStatusFilter | None,
    limit: int,
    json_output: bool,
) -> int:
    config = _config(instance)
    state_dir = instance / config.state_dir
    path = state_dir / STORE_FILE
    runs: list[RunSummary] = []
    try:
        if path.exists():  # never create the store just to list it
            # Read-only: no migration, no WAL switch, no new file.
            with Store.open(path, read_only=True) as store:
                runs = store.list_runs(
                    pipeline=pipeline,
                    status=None if status is None else status.value,
                    limit=limit,
                )
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None
    # A `running` row whose pipeline lock is free has no live process: the next run of the
    # pipeline will abandon it. Checked without waiting and without creating anything.
    stale = [
        run.status == "running" and lock_is_free(state_dir, run.pipeline) is True for run in runs
    ]
    if json_output:
        report = {
            "schema_version": RUNS_SCHEMA_VERSION,
            "instance": str(instance.resolve()),
            "runs": [_run_json(run, s) for run, s in zip(runs, stale, strict=True)],
        }
        echo_json(report)
    elif not runs:
        echo("no runs")
    else:
        header = [
            "RUN",
            "PIPELINE",
            "STATUS",
            "STARTED",
            "DURATION",
            "COST",
            "TOKENS IN/OUT",
            "ERROR",
        ]
        rows = [_run_row(run, s) for run, s in zip(runs, stale, strict=True)]
        for line in _table(header, rows):
            echo(line)
    return EXIT_OK


@_command()
def runs(
    instance: InstanceDir = Path(),
    pipeline: Annotated[
        str | None, typer.Option("--pipeline", "-p", help="Only runs of this pipeline.")
    ] = None,
    status: Annotated[
        RunStatusFilter | None, typer.Option("--status", "-s", help="Only runs with this status.")
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=0, max=MAX_LIST_RUNS, help="At most this many runs."),
    ] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Print the runs as JSON.")] = False,
) -> None:
    """List runs, newest first, with status, duration and cost.

    Exit codes: 0 listed (possibly none), 1 configuration or store problem, 2 usage error,
    70 internal error. With --json: {schema_version, instance, runs}; an error is
    {schema_version, error}."""
    run_command(
        "runs", json_output, lambda: _list_runs(instance, pipeline, status, limit, json_output)
    )


@asynccontextmanager
async def _diff_sinks(instance: Path, vault: Path | None) -> AsyncIterator[SinkRegistry]:
    if uses_host(instance, vault):
        async with host_sinks(load(instance)) as sinks:
            yield sinks
    else:
        yield StaticSinks(default=LocalSink(vault))


def _diff(instance: Path, run_id: str, vault: Path | None, json_output: bool, color: bool) -> int:
    config = _config(instance)
    path = instance / config.state_dir / STORE_FILE
    if not path.exists():  # never create the store just to read it
        raise CommandFailed(EXIT_REFUSED, "unknown_run", f"unknown run {run_id!r} (no store)")
    try:
        # Read-only: no migration, no WAL switch, no new file (schemas decision 32, D5).
        with Store.open(path, read_only=True) as store:
            record = store.get_run(run_id)
            if record is None:
                raise CommandFailed(
                    EXIT_REFUSED, "unknown_run", f"unknown or unfinished run {run_id!r}"
                )

            def notified(key: str) -> bool:
                return store.get_notification(key) is not None

            async def plan() -> Any:
                async with _diff_sinks(instance, vault) as sinks:
                    return await plan_changes(
                        record,
                        sinks,
                        notified=notified,
                        call_timeout_s=config.plugin_defaults.call_timeout_s,
                    )

            changes = asyncio.run(plan())
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None
    if json_output:
        echo(to_json(changes))
    else:
        echo(render_text(changes, color=color), nl=False, color=color or None)
    if changes.refused is not None:
        return EXIT_REFUSED
    return EXIT_DIFFERENT if changes.problems else EXIT_OK


@_command()
def diff(
    run_id: Annotated[str, typer.Argument(help="The run to show.")],
    instance: InstanceDir = Path(),
    vault: Annotated[
        Path | None,
        typer.Option(
            "--vault",
            help=(
                "Read notes from this local vault with the built-in read-only sink, instead "
                "of through the instance's plugins."
            ),
            file_okay=False,
            exists=True,
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the diff as JSON.")] = False,
    color: Annotated[bool, typer.Option("--color", help="Colour the text diff.")] = False,
) -> None:
    """Show what applying a stored run would change, without applying anything.

    Notes are read through the instance's plugins (<instance>/plugins, their read-only
    `read` operation) when it has them; --vault reads a local vault with the built-in
    read-only sink instead. A failed, undone or already applied run is refused.

    Exit codes: 0 planned, 1 configuration, plugin or store problem, 2 usage error, 3 unknown
    or refused run, 4 planned but some intents cannot be applied, 70 internal error. With
    --json, errors are a JSON object on stdout ({schema_version, error: {kind, message}})."""
    run_command("diff", json_output, lambda: _diff(instance, run_id, vault, json_output, color))


def _replay(instance: Path, store: Path | None, run_id: str, against_current: bool) -> ReplayResult:
    try:
        loaded = load_instance(instance)
        loaded.prime_redaction()
    except InstanceError as exc:
        problems = "; ".join(str(problem) for problem in exc.problems)
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", problems) from None
    path = store if store is not None else loaded.store_path
    if not path.is_file():
        raise CommandFailed(
            EXIT_REFUSED, "unknown_run", f"unknown run {run_id!r}: no store at {path}"
        )
    try:
        # Read-only: no migration, no WAL switch, no new file.
        with Store.open(path, read_only=True) as opened:
            record: RunRecord | None = opened.get_run(run_id)
    except ValidationError:
        raise CommandFailed(
            EXIT_REFUSED,
            "not_comparable",
            f"run {run_id!r}: its record is not a valid v1 run record",
        ) from None
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None
    if record is None:
        raise CommandFailed(
            EXIT_REFUSED, "unknown_run", f"unknown run {run_id!r} (or still running, or abandoned)"
        )
    current = loaded.pipelines.get(record.pipeline)
    chosen, _ = definition_for(record, current, against_current)
    registry = LockRegistry(loaded.lock, chosen, record)
    if registry.unresolved:
        return ReplayResult(
            run_id=run_id,
            pipeline=record.pipeline,
            outcome="error",
            against_current=against_current,
            error=(
                f"{', '.join(registry.unresolved)}: resolved by no plugin of horae.lock "
                "(neither as the run resolved it nor by namespace)"
            ),
        )
    models = {alias: spec.model for alias, spec in loaded.config.models.items()}
    # Skills are read from the installed plugins (local files only, #27); without them a run
    # whose `llm` step loaded a skill replays as different (the viewed input and key differ).
    plugins_dir = instance / PLUGINS_DIR
    skills = (
        DirectorySkills({name: plugins_dir / name for name in loaded.lock.plugins})
        if plugins_dir.is_dir()
        else None
    )
    return replay(
        record,
        current,
        plugins=registry,
        models=models,
        against_current=against_current,
        lock_digest=loaded.lock_digest(),
        config_digest=loaded.config_digest(),
        skills=skills,
    )


_REPLAY_CODES = {"identical": EXIT_OK, "different": EXIT_DIFFERENT, "error": EXIT_REFUSED}


def _replay_json(result: ReplayResult, kind: str) -> dict[str, Any]:
    """The replay report with `error` as every command's `{kind, message}` (or `null`)."""
    report = result.to_dict()
    error = report.get("error")
    report["error"] = None if error is None else {"kind": kind, "message": error}
    return report


def _replay_report(
    instance: Path, store: Path | None, run_id: str, against_current: bool, json_output: bool
) -> int:
    try:
        result = _replay(instance, store, run_id, against_current)
    except CommandFailed as failed:
        if not json_output:
            raise
        # Under --json a stop is the replay report itself, with outcome `error` (every key).
        result = ReplayResult(
            run_id=run_id,
            pipeline=None,
            outcome="error",
            against_current=against_current,
            error=failed.message,
        )
        echo_json(_replay_json(result, failed.kind))
        return failed.code
    if json_output:
        echo_json(_replay_json(result, "not_comparable"))
    else:
        echo(result.render())
    return _REPLAY_CODES[result.outcome]


@_command("replay")
def replay_command(
    run_id: Annotated[str, typer.Argument(help="The run to replay.")],
    instance: InstanceDir = Path(),
    store: Annotated[
        Path | None,
        typer.Option(
            "--store", help=f"SQLite store (default: <state_dir>/{STORE_FILE}).", dir_okay=False
        ),
    ] = None,
    against_current: Annotated[
        bool,
        typer.Option(
            "--against-current",
            help=(
                "Replay the recorded data through the current pipeline definition, and accept "
                "a changed horae.lock or horae.yaml (noted)."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Re-execute a recorded run from its inputs and LLM responses, and compare its intents.

    Nothing external is called: sources, transforms and the LLM are answered from the record,
    and plugins are resolved from horae.lock and the record. The store is opened read-only.

    Exit codes: 0 identical; 1 configuration or store problem; 2 usage error (a bad option);
    3 unknown run (or no store) or not comparable (pipeline, horae.lock or horae.yaml changed
    without --against-current, unresolved plugin, a call the record cannot answer, a record
    that is not replayable); 4 different; 70 internal error. With --json the report has every
    key, also on errors (outcome `error`, and `error` is {kind, message} like every
    command's; `null` otherwise)."""
    run_command(
        "replay",
        json_output,
        lambda: _replay_report(instance, store, run_id, against_current, json_output),
    )


# `horae init`, `run`, `undo`, `usage` and `cache` live in their own modules; registered here
# so `horae.cli.app` has every command.
from horae.cli_cache import cache_app as _cache_app  # noqa: E402
from horae.cli_init import init as _init  # noqa: E402
from horae.cli_run import run as _run  # noqa: E402
from horae.cli_undo import undo as _undo  # noqa: E402
from horae.cli_usage import usage as _usage  # noqa: E402

_command("init")(_init)
_command("run")(_run)
_command("undo")(_undo)
_command("usage")(_usage)
app.add_typer(_cache_app, name="cache")

__all__ = [
    "EXIT_DIFFERENT",
    "EXIT_INTERNAL",
    "EXIT_LOCKED",
    "EXIT_NOT_COMPARABLE",
    "EXIT_OK",
    "EXIT_PLANNED_WITH_PROBLEMS",
    "EXIT_PROBLEMS",
    "EXIT_REFUSED",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "EXIT_UNKNOWN_RUN",
    "EXIT_USAGE",
    "app",
    "internal_message",
    "report_error",
]
