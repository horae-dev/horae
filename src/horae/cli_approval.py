# SPDX-License-Identifier: Apache-2.0
"""`horae approval list|show|approve|deny|revoke` (#55, schemas decision 77): the user's side
of `pipeline.run`.

An MCP client may only *ask* for a pipeline (`pipeline.run`: a dry run and an approval id).
Applying it is decided here, on the instance's terminal, and nowhere else:

- **`approve ID`**: re-plans the dry run against the current vault and configuration, refuses
  when it is no longer exactly what was approved for (`plan_mismatch`, recorded `refused`),
  shows the whole change set (as `horae diff`, HTTP bodies in full; a binary or too-large
  note cannot be shown, so it refuses, `not_reviewable`) and asks the user to retype a short
  code printed with it (the last 6 characters of the binding digest). Once confirmed, the
  approval is claimed (single use) and applied as a new run, checked again under the apply
  lock; `horae undo <run>` reverts it.

  **What stops whom.** The approval channel is this command on the instance: an MCP client
  that only speaks MCP (stdio or an HTTP token holder) has no way to run it. The TTY check
  and the retyped code are **friction, not a boundary**: an agent that has a shell as the
  same user can allocate a pseudo-terminal (`pty.fork()`, `script`, `expect`) and read the
  code from the output. Such an agent can already do anything the user can (`horae run`);
  give a client either MCP or a shell as you, not both, if you rely on the approval.
- **`deny ID`**: the approval ends `denied`; nothing is applied.
- **`revoke --all`**: every pending approval ends `revoked`.
- **`list [--status S]`** and **`show ID`**: the approvals and, for one, its journal of
  transitions (`approval_events`). Pending approvals past their TTL are recorded `expired`
  first.

Exit codes (`horae.cli_output`, decision 37): 0 done, 1 configuration or store problem, not
pending (expired, denied, revoked, already used), not interactive, not confirmed, not
reviewable (a binary or too-large note), or the apply failed, 2 usage error, 3 unknown
approval, 4 plan mismatch (nothing applied), 5 the pipeline is running or the apply lock is
busy, 70 internal error, 130/143 interrupted.
"""

import asyncio
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.apply import MAX_DIFF_BYTES, render_text
from horae.approval import MISMATCH, ApprovalError, apply, deny, preview, recover_stuck
from horae.cli_host import load
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    CommandFailed,
    JsonUsageCommand,
    JsonUsageGroup,
    echo,
    echo_json,
    io_failed,
    plural,
    run_command,
    safe,
)
from horae.cli_run import Interrupted, cancel_on_signals
from horae.config import Instance
from horae.runner import PipelineLocked
from horae.store import ApprovalEvent, ApprovalRow, ApprovalStatus, Store, StoreError

APPROVAL_SCHEMA_VERSION = 1
"""Version of the `horae approval ... --json` documents."""
_STATUSES: tuple[str, ...] = ApprovalStatus.__args__  # type: ignore[attr-defined]

approval_app = typer.Typer(
    name="approval",
    help="Approve, deny or revoke what an MCP client asked to run (pipeline.run).",
    no_args_is_help=True,
    add_completion=False,
    cls=JsonUsageGroup,
    pretty_exceptions_show_locals=False,
)

InstanceOption = Annotated[
    Path,
    typer.Option(
        "--instance",
        "-i",
        envvar="HORAE_INSTANCE",
        help="Instance directory (holds horae.yaml).",
        file_okay=False,
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the result as JSON.")]
IdArgument = Annotated[str, typer.Argument(help="The approval id (apr-...).")]


def _now() -> datetime:
    return datetime.now(UTC)


def confirmation_code(row: ApprovalRow) -> str:
    """What the user retypes to approve: the last 6 characters of the binding digest. It
    makes approving a deliberate act; it is not a secret (it is printed with the changes)."""
    return (row.binding_sha256 or "")[-6:]


def _interactive() -> bool:
    """Whether stdin is a terminal a person types in (approving needs one)."""
    return sys.stdin.isatty()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _row_json(row: ApprovalRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "pipeline": row.pipeline,
        "requested_by": row.requested_by,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "decided_at": _iso(row.decided_at),
        "dry_run_id": row.dry_run_id,
        "applied_run_id": row.applied_run_id,
        "reason": row.reason,
    }


def _with_store[T](instance: Instance, body: Callable[[Store], T]) -> T:
    path = instance.store_path
    try:
        with Store.open(path) as store:
            return body(store)
    except ApprovalError as error:
        raise _failed(error) from None
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None


def _failed(error: ApprovalError) -> CommandFailed:
    code = {"unknown_approval": EXIT_REFUSED, "plan_mismatch": EXIT_DIFFERENT}.get(
        error.kind, EXIT_PROBLEMS
    )
    return CommandFailed(code, error.kind, error.message)


def _unknown(approval_id: str) -> CommandFailed:
    return CommandFailed(EXIT_REFUSED, "unknown_approval", f"no approval {approval_id!r}")


def _list(instance_dir: Path, status: str | None, json_output: bool) -> int:
    if status is not None and status not in _STATUSES:
        raise CommandFailed(2, "usage", f"--status: one of {', '.join(_STATUSES)}")
    instance = load(instance_dir)
    rows: list[ApprovalRow] = []
    if instance.store_path.exists():

        def body(store: Store) -> list[ApprovalRow]:
            store.approval_expire(at=_now())
            recover_stuck(instance, store, at=_now())
            return store.approvals(status=status)  # type: ignore[arg-type]

        rows = _with_store(instance, body)
    shown = [_row_json(row) for row in rows]
    if json_output:
        echo_json({"schema_version": APPROVAL_SCHEMA_VERSION, "approvals": shown})
        return EXIT_OK
    if not shown:
        echo("no approval")
        return EXIT_OK
    for item in shown:
        applied = f"  run={item['applied_run_id']}" if item["applied_run_id"] else ""
        echo(
            f"{item['id']}  {item['status']}  {safe(item['pipeline'])}  "
            f"by={safe(item['requested_by'])}  created={item['created_at']}  "
            f"expires={item['expires_at'] or '-'}  dry_run={item['dry_run_id'] or '-'}{applied}"
        )
    pending = sum(item["status"] == "pending" for item in shown)
    echo(f"{plural(len(shown), 'approval')}, {pending} pending")
    return EXIT_OK


def _show(instance_dir: Path, approval_id: str, json_output: bool) -> int:
    instance = load(instance_dir)
    if not instance.store_path.exists():
        raise _unknown(approval_id)

    def body(store: Store) -> tuple[ApprovalRow | None, list[ApprovalEvent]]:
        store.approval_expire(at=_now())
        recover_stuck(instance, store, at=_now())
        return store.approval(approval_id), store.approval_events(approval_id)

    row, events = _with_store(instance, body)
    if row is None:
        raise _unknown(approval_id)
    journal = [
        {"at": _iso(e.at), "event": e.event, "run_id": e.run_id, "detail": e.detail} for e in events
    ]
    if json_output:
        document = {"approval": _row_json(row), "events": journal}
        echo_json({"schema_version": APPROVAL_SCHEMA_VERSION, **document})
        return EXIT_OK
    for key, value in _row_json(row).items():
        echo(f"{key}: {safe(str(value)) if value is not None else '-'}")
    echo("events:")
    for e in journal:
        run = f" run={e['run_id']}" if e["run_id"] else ""
        detail = f" ({safe(e['detail'])})" if e["detail"] else ""
        echo(f"  {e['at']}  {e['event']}{run}{detail}")
    return EXIT_OK


def _approve(instance_dir: Path, approval_id: str) -> int:
    instance = load(instance_dir)
    if not instance.store_path.exists():
        raise _unknown(approval_id)
    if not _interactive():
        raise CommandFailed(
            EXIT_PROBLEMS,
            "not_interactive",
            "approving needs an interactive terminal: run `horae approval approve` yourself "
            "and review the changes",
        )
    try:
        seen = asyncio.run(preview(instance, approval_id))
    except ApprovalError as error:
        raise _failed(error) from None
    row = seen.row
    echo(
        f"approval {row.id}: pipeline {safe(row.pipeline)}, asked by {safe(row.requested_by)} "
        f"at {_iso(row.created_at)}, expires {_iso(row.expires_at)}"
    )
    hidden = [
        f.path for f in seen.plan.files if f.status != "unchanged" and (f.binary or f.too_large)
    ]
    if hidden:
        raise CommandFailed(
            EXIT_PROBLEMS,
            "not_reviewable",
            f"nothing applied: {len(hidden)} note change(s) cannot be shown in full (binary or "
            f"larger than {MAX_DIFF_BYTES} bytes): {', '.join(safe(p) for p in hidden)}; "
            f"approval {row.id} stays pending (`horae approval deny {row.id}` to deny it)",
        )
    echo(render_text(seen.plan, full=True), nl=False)
    code = confirmation_code(row)
    echo(f"To apply exactly these changes, type {code} (anything else: nothing is applied).")
    typed = typer.prompt("Code", default="", show_default=False)
    if typed.strip() != code:
        raise CommandFailed(
            EXIT_PROBLEMS,
            "not_confirmed",
            f"nothing applied; approval {row.id} stays pending until it expires "
            f"(`horae approval deny {row.id}` to deny it)",
        )
    try:
        record = asyncio.run(cancel_on_signals(apply(instance, seen)))
    except ApprovalError as error:
        raise _failed(error) from None
    except PipelineLocked as locked:
        message = f"{locked}; approval {row.id} is still pending"
        raise CommandFailed(EXIT_LOCKED, "pipeline_locked", message) from None
    except Interrupted as stop:
        echo(f"horae approval approve: interrupted; see `horae approval show {row.id}`", err=True)
        return 128 + stop.signum
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", str(exc)) from None
    why = record.error or ""
    if record.status == "failed":
        if why.startswith(MISMATCH):
            raise CommandFailed(EXIT_DIFFERENT, "plan_mismatch", why)
        if why.startswith("apply_locked"):
            raise CommandFailed(EXIT_LOCKED, "apply_locked", why)
        message = f"run {record.id} failed: {why} (`horae undo {record.id}` reverts it)"
        raise CommandFailed(EXIT_PROBLEMS, "run_failed", message)
    echo(
        f"applied approval {row.id} as run {record.id} ({record.status}); "
        f"`horae undo {record.id}` reverts it"
    )
    return EXIT_OK


def _deny(instance_dir: Path, approval_id: str, json_output: bool) -> int:
    instance = load(instance_dir)
    if not instance.store_path.exists():
        raise _unknown(approval_id)
    row: ApprovalRow = _with_store(instance, lambda s: deny(s, approval_id, at=_now()))
    if json_output:
        echo_json({"schema_version": APPROVAL_SCHEMA_VERSION, "approval": _row_json(row)})
    else:
        echo(f"denied approval {row.id}: nothing will be applied")
    return EXIT_OK


def _revoke(instance_dir: Path, all_pending: bool, json_output: bool) -> int:
    if not all_pending:
        raise CommandFailed(2, "usage", "revoke needs --all (deny one with `deny ID`)")
    instance = load(instance_dir)
    revoked: list[str] = []
    if instance.store_path.exists():

        def body(store: Store) -> list[str]:
            at = _now()
            store.approval_expire(at=at)
            done = []
            for row in store.approvals(status="pending", limit=1000):
                if store.approval_decide(row.id, "revoked", at=at, reason="revoked by the user"):
                    done.append(row.id)
            return done

        revoked = _with_store(instance, body)
    if json_output:
        echo_json({"schema_version": APPROVAL_SCHEMA_VERSION, "revoked": revoked})
    else:
        echo(f"revoked {plural(len(revoked), 'pending approval')}")
    return EXIT_OK


@approval_app.command("list", cls=JsonUsageCommand)
def list_approvals(
    instance: InstanceOption = Path(),
    status: Annotated[
        str | None, typer.Option("--status", help="Only approvals with this status.")
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """List pipeline.run approvals, newest first (expired ones are recorded first).

    Exit codes: 0 listed, 1 configuration or store problem, 2 usage error, 70 internal
    error."""
    run_command("approval list", json_output, lambda: _list(instance, status, json_output))


@approval_app.command("show", cls=JsonUsageCommand)
def show(
    approval_id: IdArgument, instance: InstanceOption = Path(), json_output: JsonOption = False
) -> None:
    """Show one approval and its journal of transitions.

    Exit codes: 0 shown, 1 configuration or store problem, 3 unknown approval, 70 internal
    error."""
    run_command("approval show", json_output, lambda: _show(instance, approval_id, json_output))


@approval_app.command("approve", cls=JsonUsageCommand)
def approve(approval_id: IdArgument, instance: InstanceOption = Path()) -> None:
    """Review and apply what an MCP client asked to run, on this terminal.

    Re-plans the dry run, refuses if anything changed since (the plan, the pipeline, its
    configuration, the vault), shows every change in full and asks you to retype a short
    code. Needs an interactive terminal; there is no --yes. This is friction against
    approving by accident, not a barrier against an agent with a shell as you. Applied once,
    as a new run (`horae undo <run>` reverts it).

    Exit codes: 0 applied, 1 not pending, not interactive, not confirmed, not reviewable or
    the apply failed,
    3 unknown approval, 4 plan mismatch (nothing applied), 5 pipeline running or apply lock
    busy, 70 internal error, 130/143 interrupted."""
    run_command("approval approve", False, lambda: _approve(instance, approval_id))


@approval_app.command("deny", cls=JsonUsageCommand)
def deny_command(
    approval_id: IdArgument, instance: InstanceOption = Path(), json_output: JsonOption = False
) -> None:
    """Deny a pending approval: nothing is applied.

    Exit codes: 0 denied, 1 not pending or store problem, 3 unknown approval, 70 internal
    error."""
    run_command("approval deny", json_output, lambda: _deny(instance, approval_id, json_output))


@approval_app.command("revoke", cls=JsonUsageCommand)
def revoke(
    instance: InstanceOption = Path(),
    all_pending: Annotated[
        bool, typer.Option("--all", help="Revoke every pending approval.")
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Revoke every pending approval (--all): nothing of them is applied.

    Exit codes: 0 done, 1 store problem, 2 usage error (no --all), 70 internal error."""
    run_command("approval revoke", json_output, lambda: _revoke(instance, all_pending, json_output))


__all__ = ["approval_app", "confirmation_code"]
