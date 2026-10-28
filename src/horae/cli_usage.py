# SPDX-License-Identifier: Apache-2.0
"""`horae usage` (#33): what LLM calls cost, per pipeline and day, from the usage ledger.

Registered on the `horae` app by `horae.cli`. Reads the store read-only (no migration, no
WAL switch, no new file; decision 31 E): a missing store lists nothing, a schema older than
this Horae is a store problem ("run `horae run` once to migrate it"). Days are local days of
the instance's `timezone`, as the daily budgets count them (decision 41).

Exit codes (`horae.cli_output`, decision 37): 0 listed (possibly nothing), 1 configuration or
store problem, 2 usage error (a malformed date, `--since` after `--until`), 70 internal error.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CommandFailed,
    echo_json,
    io_failed,
    plural,
    run_command,
    safe,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.store import Store, StoreError, UsageGroup

_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

USAGE_SCHEMA_VERSION = 1
"""Version of the `horae usage --json` report."""

__all__ = ["USAGE_SCHEMA_VERSION", "GroupBy", "usage"]


class GroupBy(StrEnum):
    day = "day"
    pipeline = "pipeline"
    model = "model"


_KEYS: dict[GroupBy | None, tuple[str, ...]] = {
    None: ("day", "pipeline"),
    GroupBy.day: ("day",),
    GroupBy.pipeline: ("pipeline",),
    GroupBy.model: ("backend", "model"),
}


@dataclass
class _Total:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    unpriced: int = 0
    estimated: int = 0

    def add(self, group: UsageGroup) -> None:
        self.calls += group.calls
        self.tokens_in += group.tokens_in
        self.tokens_out += group.tokens_out
        self.cache_read_tokens += group.cache_read_tokens
        self.cache_write_tokens += group.cache_write_tokens
        self.usd += group.usd
        self.unpriced += group.unpriced
        self.estimated += group.estimated

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "usd": round(self.usd, 9),
            "unpriced_calls": self.unpriced,
            "estimated_calls": self.estimated,
        }


def _key(group: UsageGroup, fields: tuple[str, ...]) -> tuple[str, ...]:
    values = {
        "day": group.day,
        "pipeline": group.pipeline,
        "backend": group.backend or "-",
        "model": group.model or f"({group.alias})",
    }
    return tuple(values[name] for name in fields)


def summarize(
    groups: Iterable[UsageGroup], by: GroupBy | None
) -> tuple[tuple[str, ...], list[tuple[tuple[str, ...], _Total]], _Total]:
    """`(key names, [(key, total)] in key order, grand total)`."""
    fields = _KEYS[by]
    totals: dict[tuple[str, ...], _Total] = {}
    grand = _Total()
    for group in groups:
        totals.setdefault(_key(group, fields), _Total()).add(group)
        grand.add(group)
    return fields, sorted(totals.items()), grand


def _config(instance: Path) -> InstanceConfig:
    try:
        return validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        found = plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None


def _groups(
    instance: Path, pipeline: str | None, since: date | None, until: date | None
) -> list[UsageGroup]:
    config = _config(instance)
    path = instance / config.state_dir / STORE_FILE
    try:
        if not path.exists():  # never create the store just to read it
            return []
        with Store.open(path, read_only=True) as store:
            return store.usage_report(since=since, until=until, pipeline=pipeline)
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]


def _cells(total: _Total) -> list[str]:
    usd = f"${total.usd:.4f}"
    if total.unpriced:
        usd += "*"
    if total.estimated:
        usd += "~"
    return [
        str(total.calls),
        f"{total.tokens_in}/{total.tokens_out}",
        f"{total.cache_read_tokens}/{total.cache_write_tokens}",
        usd,
    ]


def _report(
    instance: Path,
    pipeline: str | None,
    since: date | None,
    until: date | None,
    by: GroupBy | None,
    json_output: bool,
) -> int:
    if since is not None and until is not None and since > until:
        raise CommandFailed(EXIT_USAGE, "usage", "--since is after --until")
    fields, rows, grand = summarize(_groups(instance, pipeline, since, until), by)
    if json_output:
        echo_json(
            {
                "schema_version": USAGE_SCHEMA_VERSION,
                "instance": str(instance.resolve()),
                "by": list(fields),
                "pipeline": pipeline,
                "since": None if since is None else since.isoformat(),
                "until": None if until is None else until.isoformat(),
                "rows": [
                    {**dict(zip(fields, key, strict=True)), **total.to_dict()}
                    for key, total in rows
                ],
                "total": grand.to_dict(),
            }
        )
        return EXIT_OK
    if not rows:
        typer.echo("no LLM usage")
        return EXIT_OK
    header = [*(name.upper() for name in fields), "CALLS", "TOKENS IN/OUT", "CACHE R/W", "USD"]
    table = [[*(safe(part) for part in key), *_cells(total)] for key, total in rows]
    table.append(["TOTAL", *([""] * (len(fields) - 1)), *_cells(grand)])
    for line in _table(header, table):
        typer.echo(line)
    if grand.unpriced:
        typer.echo(
            f"* {plural(grand.unpriced, 'call')} of a model alias without `price`: "
            "their cost is not included"
        )
    if grand.estimated:
        typer.echo(
            f"~ {plural(grand.estimated, 'call')} counted at the most they could cost "
            "(in flight, interrupted, or no usage reported)"
        )
    return EXIT_OK


def _day(value: str | None) -> date | None:
    """`--since` / `--until`: a `YYYY-MM-DD` day (a usage error otherwise)."""
    if value is None:
        return None
    if _DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise typer.BadParameter("expected a date, YYYY-MM-DD")


def _day_option(name: str, help: str) -> Any:
    return typer.Option(name, metavar="DATE", help=help, parser=_day)


def usage(
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
    pipeline: Annotated[
        str | None, typer.Option("--pipeline", "-p", help="Only calls of this pipeline.")
    ] = None,
    since: Annotated[
        date | None, _day_option("--since", "From this local day (YYYY-MM-DD), inclusive.")
    ] = None,
    until: Annotated[
        date | None, _day_option("--until", "Up to this local day (YYYY-MM-DD), inclusive.")
    ] = None,
    by: Annotated[
        GroupBy | None,
        typer.Option("--by", help="Group by day, pipeline or model (default: day and pipeline)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Show what LLM calls cost, per pipeline and day, with totals.

    From the usage ledger: one row per LLM call, days in the instance's time zone. Calls of a
    model alias without a price are flagged (*), and so are calls counted at the most they
    could cost (~).

    Exit codes: 0 listed (possibly nothing), 1 configuration or store problem, 2 usage error,
    70 internal error. With --json: {schema_version, instance, by, pipeline, since, until,
    rows, total}; an error is {schema_version, error}."""
    run_command(
        "usage", json_output, lambda: _report(instance, pipeline, since, until, by, json_output)
    )
