# SPDX-License-Identifier: Apache-2.0
"""What every `horae` command shares (#26, schemas decision 37): exit codes, errors, JSON.

**Exit codes** (the same for every command; each command's help lists the ones it uses):

- `0` `EXIT_OK`: done (no problem, listed, planned, identical, undone, created);
- `1` `EXIT_PROBLEMS`: a configuration, store, plugin or runtime problem (doctor found
  problems, the run failed or timed out, a sink failed);
- `2` `EXIT_USAGE`: usage error (a bad option, a missing argument, a malformed `--var`);
- `3` `EXIT_REFUSED`: unknown or refused target (an unknown run or pipeline; a run refused:
  failed, undone, already applied, a dry run, nothing to undo; a replay that cannot be
  compared; a non-empty `horae init` target);
- `4` `EXIT_DIFFERENT`: differences (a replay differs; a plan has problems, some intents
  cannot be applied; an undo refused because a note changed);
- `5` `EXIT_LOCKED`: locked (the pipeline is running, or another process holds the apply
  lock);
- `70` `EXIT_INTERNAL`: internal error (`EX_SOFTWARE`); only the exception's type is printed;
- `130` / `143` `EXIT_SIGINT` / `EXIT_SIGTERM`: `horae run` was interrupted (128 + the
  signal): before its apply, its partial record is saved `failed`, kind `cancelled`; during
  its apply, the apply finishes and the record is saved first. A `KeyboardInterrupt` that
  reaches any command (outside `horae run`'s handlers) is `130` too, kind `interrupted`.

**Output.** Text goes to stdout, errors to stderr as `horae <command>: <message>`, every
untrusted character escaped (`safe`: control, bidi, zero-width and separator characters).
No colour unless asked (`horae diff --color`). With `--json` every command prints exactly
one JSON document on stdout, `schema_version: 1`, ASCII only, strict (no `NaN`), errors
included: `{"schema_version": 1, "error": {"kind", "message"}}` (usage errors too, kind
`usage`, also those of the `horae` group itself: `horae --json`, `horae nope --json`).
Nothing printed is ever a secret value: an internal error shows its exception type only,
never its message, traceback or locals. Everything a command prints goes through `echo`,
which redacts it with the process registry (`horae.secrets.redact.current()`, #53, schemas
decision 43 F); a JSON document is redacted as data first (`to_json`), then as text. The
commands that read the instance prime the registry with every secret it names
(`prime_secrets`, `Instance.prime_redaction`), so a value is redacted even when no plugin
host was opened.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from typer.core import TyperCommand, TyperGroup

from horae.apply.render import dumps, safe
from horae.contracts.instance import InstanceConfig
from horae.secrets import SecretResolver
from horae.secrets.redact import current as current_redactor
from horae.secrets.redact import prime

if TYPE_CHECKING:
    # Typer vendors click privately; its `Context` base class is only needed for typing.
    from typer._click.core import Context

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_DIFFERENT = 4
EXIT_LOCKED = 5
EXIT_INTERNAL = 70
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

EXIT_CODES: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_PROBLEMS: "problems",
    EXIT_USAGE: "usage",
    EXIT_REFUSED: "unknown or refused",
    EXIT_DIFFERENT: "different",
    EXIT_LOCKED: "locked",
    EXIT_INTERNAL: "internal error",
    EXIT_SIGINT: "interrupted (SIGINT)",
    EXIT_SIGTERM: "terminated (SIGTERM)",
}
"""Every exit code a `horae` command can return."""

SCHEMA_VERSION = 1
"""`schema_version` of every `--json` document."""


class CommandFailed(Exception):
    """A command stops with `code`, an error `kind` and a message (untrusted text allowed)."""

    def __init__(self, code: int, kind: str, message: str) -> None:
        super().__init__(kind)
        self.code, self.kind, self.message = code, kind, message


def io_failed(exc: OSError, where: Path | str, store: Path | None = None) -> CommandFailed:
    """`CommandFailed` for an `OSError`: `store_error` when it names the store's files
    (`store` and its `-wal`, `-shm`, lock files), else `io_error`; the file it names (or
    `where`) and its `strerror`, never the exception's full text."""
    named = exc.filename if isinstance(exc.filename, (str, Path)) else None
    reason = exc.strerror or type(exc).__name__
    if named is not None and store is not None and str(named).startswith(str(store)):
        return CommandFailed(EXIT_PROBLEMS, "store_error", f"{named}: {reason}")
    return CommandFailed(EXIT_PROBLEMS, "io_error", f"{named or where}: {reason}")


def plural(count: int, word: str, plurals: str | None = None) -> str:
    return f"{count} {word if count == 1 else plurals or word + 's'}"


def prime_secrets(root: Path, config: InstanceConfig) -> None:
    """Resolve, for redaction only, the secrets `horae.yaml` names (`secrets.refs` and the
    model aliases' `secret`) for a command that reads `horae.yaml` alone (#53)."""
    names = set(config.secrets.refs)
    names.update(spec.secret for spec in config.models.values() if spec.secret)
    prime(SecretResolver(config.secrets.refs, root / config.secrets.dir), names)


def echo(
    message: str = "", *, err: bool = False, nl: bool = True, color: bool | None = None
) -> None:
    """`typer.echo`, redacted (every secret this process resolved becomes
    `[REDACTED:<NAME>]`)."""
    typer.echo(current_redactor().redact_text(message)[0], err=err, nl=nl, color=color)


def to_json(value: Any) -> str:
    """`value` as the JSON document of a command: indented, ASCII, strict; its strings and
    keys redacted (`Redactor.scrub_data`)."""
    scrubbed, _ = current_redactor().scrub_data(value)
    return dumps(scrubbed, indent=2, ensure_ascii=True)


def echo_json(value: Any) -> None:
    echo(to_json(value))


def error_document(kind: str, message: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "error": {"kind": kind, "message": message}}


def report_error(command: str, kind: str, message: str, json_output: bool) -> None:
    """An error: a JSON object on stdout with `--json`, else `horae <command>: ...` on stderr."""
    if json_output:
        echo_json(error_document(kind, message))
    else:
        echo(f"horae {command}: {safe(message)}", err=True)


def internal_message(exc: BaseException) -> str:
    # Never the exception's message, traceback or locals: a frame may hold a secret.
    return f"internal error ({type(exc).__name__}); please report it"


def run_command(command: str, json_output: bool, body: Callable[[], int]) -> NoReturn:
    """Run `body` (which prints its own result and returns the exit code) and exit with it;
    `CommandFailed` and any other exception become an error report."""
    try:
        code = body()
    except CommandFailed as failed:
        report_error(command, failed.kind, failed.message, json_output)
        code = failed.code
    except KeyboardInterrupt:
        # Ctrl-C outside `horae run`'s own signal handling (or before it is installed).
        report_error(command, "interrupted", "interrupted by SIGINT", json_output)
        code = EXIT_SIGINT
    except Exception as exc:
        message = internal_message(exc)
        del exc
        report_error(command, "internal", message, json_output)
        code = EXIT_INTERNAL
    raise typer.Exit(code)


def _wants_json(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg == "--json":
            return True
    return False


def _json_usage_error(error: Exception, wants_json: bool) -> None:
    """Under `--json`, a usage error (Typer's vendored, private `UsageError`: exit code 2 and
    `format_message`) becomes a JSON error document and `typer.Exit(2)`; else it is
    re-raised by the caller."""
    format_message = getattr(error, "format_message", None)
    if not wants_json or getattr(error, "exit_code", None) != EXIT_USAGE:
        return
    message = format_message() if callable(format_message) else "usage error"
    echo_json(error_document("usage", str(message)))
    raise typer.Exit(EXIT_USAGE) from None


class JsonUsageCommand(TyperCommand):
    """A command whose usage errors (exit 2) are a JSON error object on stdout when `--json`
    is among its arguments, like every other error."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: "Context | None" = None,
        **extra: Any,
    ) -> "Context":
        wants_json = _wants_json(list(args))
        try:
            return super().make_context(info_name, args, parent, **extra)
        except Exception as error:
            _json_usage_error(error, wants_json)
            raise


class JsonUsageGroup(TyperGroup):
    """The `horae` group: its own usage errors (`horae --json`, `horae nope --json`, an
    unknown option before the command) are a JSON error object too when `--json` is among
    the arguments (before any `--`)."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: "Context | None" = None,
        **extra: Any,
    ) -> "Context":
        wants_json = _wants_json(list(args))
        try:
            return super().make_context(info_name, args, parent, **extra)
        except Exception as error:
            _json_usage_error(error, wants_json)
            raise

    def resolve_command(self, ctx: "Context", args: list[str]) -> Any:
        wants_json = _wants_json(list(args))
        try:
            return super().resolve_command(ctx, args)
        except Exception as error:
            _json_usage_error(error, wants_json)
            raise


__all__ = [
    "EXIT_CODES",
    "EXIT_DIFFERENT",
    "EXIT_INTERNAL",
    "EXIT_LOCKED",
    "EXIT_OK",
    "EXIT_PROBLEMS",
    "EXIT_REFUSED",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "EXIT_USAGE",
    "SCHEMA_VERSION",
    "CommandFailed",
    "JsonUsageCommand",
    "JsonUsageGroup",
    "echo",
    "echo_json",
    "error_document",
    "internal_message",
    "io_failed",
    "plural",
    "prime_secrets",
    "report_error",
    "run_command",
    "safe",
    "to_json",
]
