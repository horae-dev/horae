# SPDX-License-Identifier: Apache-2.0
"""`horae cache purge [--all | --older-than DAYS] [--skill NAME] [--json]` (#32, decision 45 E).

Deletes LLM cache entries on demand (`Store.cache_purge`): every entry (`--all`), those
created more than DAYS days ago (`--older-than`), optionally only one skill's (`--skill`;
alone, every entry of that skill). It opens the store writable (migrating it if needed), but
never creates one: a missing store has nothing to purge. It takes no pipeline lock: a run
that looks an entry up while it is deleted simply calls the model. Prints how many entries
were deleted and how many remain.

Exit codes (`horae.cli_output`, decision 37): 0 purged (possibly nothing), 1 configuration
or store problem, 2 usage error (no selection, `--all` with `--older-than`), 70 internal
error.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CommandFailed,
    JsonUsageCommand,
    JsonUsageGroup,
    echo,
    echo_json,
    io_failed,
    plural,
    prime_secrets,
    run_command,
    safe,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.store import Store, StoreError

CACHE_SCHEMA_VERSION = 1
"""Version of the `horae cache purge --json` report."""

MAX_OLDER_THAN_DAYS = 36600

cache_app = typer.Typer(
    name="cache",
    help="Manage the LLM cache (accepted outputs reused on unchanged input).",
    no_args_is_help=True,
    add_completion=False,
    cls=JsonUsageGroup,
    pretty_exceptions_show_locals=False,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _purge(
    instance: Path, everything: bool, older_than: int | None, skill: str | None
) -> tuple[int, int]:
    """`(deleted, remaining)`."""
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        found = plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    prime_secrets(instance, config)
    path = instance / config.state_dir / STORE_FILE
    before = None if everything or older_than is None else _now() - timedelta(days=older_than)
    try:
        if not path.exists():  # never create the store just to empty it
            return 0, 0
        with Store.open(path) as store:
            deleted = store.cache_purge(skill=skill, created_before=before)
            return deleted, store.cache_count()
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None


def _report(
    instance: Path,
    everything: bool,
    older_than: int | None,
    skill: str | None,
    json_output: bool,
) -> int:
    if everything and older_than is not None:
        raise CommandFailed(EXIT_USAGE, "usage", "--all and --older-than are exclusive")
    if not everything and older_than is None and skill is None:
        message = "nothing selected: pass --all, --older-than DAYS or --skill NAME"
        raise CommandFailed(EXIT_USAGE, "usage", message)
    deleted, remaining = _purge(instance, everything, older_than, skill)
    if json_output:
        echo_json(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "instance": str(instance.resolve()),
                "all": everything,
                "older_than_days": older_than,
                "skill": skill,
                "deleted": deleted,
                "remaining": remaining,
            }
        )
        return EXIT_OK
    what = "" if skill is None else f" of skill {safe(skill)}"
    echo(f"deleted {plural(deleted, 'cache entry', 'cache entries')}{what}; {remaining} left")
    return EXIT_OK


@cache_app.command("purge", cls=JsonUsageCommand)
def purge(
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
    everything: Annotated[bool, typer.Option("--all", help="Delete every entry.")] = False,
    older_than: Annotated[
        int | None,
        typer.Option(
            "--older-than",
            metavar="DAYS",
            min=0,
            max=MAX_OLDER_THAN_DAYS,
            help="Delete the entries created more than DAYS days ago.",
        ),
    ] = None,
    skill: Annotated[
        str | None, typer.Option("--skill", help="Only this skill's entries (e.g. pack/brief).")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Delete LLM cache entries: all of them, or older ones, optionally of one skill.

    The next run on an input whose entry was deleted calls the model again. The store is
    never created; nothing is locked.

    Exit codes: 0 purged (possibly nothing), 1 configuration or store problem, 2 usage error
    (nothing selected, --all with --older-than), 70 internal error. With --json:
    {schema_version, instance, all, older_than_days, skill, deleted, remaining}; an error is
    {schema_version, error}."""
    run_command(
        "cache purge",
        json_output,
        lambda: _report(instance, everything, older_than, skill, json_output),
    )


__all__ = ["CACHE_SCHEMA_VERSION", "cache_app", "purge"]
