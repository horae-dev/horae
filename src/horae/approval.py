# SPDX-License-Identifier: Apache-2.0
"""Approval-gated `pipeline.run` (#55, schemas decision 77): the core of both sides.

**Asking** (`request_dry_run`, called by the MCP tool `pipeline.run`). A requester (an MCP
token, or the stdio client) asks for a pipeline: a `planning` approval row is written (at most
`MAX_OPEN` open and `MAX_PER_HOUR` a rolling hour per requester, checked in the same
transaction), the pipeline runs as a **dry run** (nothing applied; `Runner.run(dry_run=True)`)
and is planned (`plan_changes`, which only reads). If there is something applicable, the row
becomes `pending` for `ttl` with its **binding digest** (`binding_digest`); otherwise `void`
with the reason. Nothing here approves anything.

**Approving** (`preview` then `apply`, called by `horae approval approve`, on the user's
terminal). `preview` re-plans the dry run's intents against the current vault and checks the
binding digest, computed with the **current** instance's pipeline, configuration and lockfile
digests: a difference refuses the approval (`refused`, nothing applied). The user confirms the
change set; `apply` then claims the row (`pending` → `approving`, a compare-and-set that only
one caller wins, only before the TTL and while the requesting token is not revoked) once
the pipeline lock is held, and
`Runner.apply_planned` copies the dry run into a new run, re-plans it under the apply lock and
applies it only if the digest still matches. The approval ends `applied`, `failed` or
`refused`, naming the run, also when a signal interrupts it (from the saved run's record);
one a crash left `approving` is settled under the pipeline lock (`recover_stuck`). Every
transition is journalled (`approval_events`).

The MCP server (`horae.mcp_server`) imports `request_dry_run` only: no approve, deny or revoke
path is reachable from it (decision 77 B).
"""

import base64
import hmac
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from horae.apply import ChangeSet, plan_changes, to_dict
from horae.cli_host import live_ports
from horae.config import Instance
from horae.contracts import RunRecord
from horae.engine import pipeline_digest
from horae.engine.canonical import sha256_hex
from horae.runner import (
    ClaimRefused,
    PipelineLock,
    PipelineLocked,
    Runner,
    RunTimedOut,
    recover_pipeline,
)
from horae.store import MAX_LIST_RUNS, ApprovalLimited, ApprovalRow, Store

logger = logging.getLogger("horae.approval")

DEFAULT_TTL = timedelta(minutes=15)
MIN_TTL_MINUTES = 1
MAX_TTL_MINUTES = 60
MAX_OPEN = 3
"""Approvals a requester may have `planning` or `pending` at once."""
MAX_PER_HOUR = 12
"""`pipeline.run` requests a requester may make in a rolling hour."""
STDIO = "stdio"
"""The requester name of the stdio client (it has no token)."""
ID_PREFIX = "apr-"
MISMATCH = "approval_mismatch"
BINDING_VERSION = 1


def new_approval_id() -> str:
    """`apr-` and 12 lowercase base32 characters (60 random bits)."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()
    return ID_PREFIX + raw[:12]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Requester:
    """Who asked: an MCP token's id and name, or the stdio client (`token_id=None`)."""

    token_id: int | None
    name: str


@dataclass(frozen=True)
class Digests:
    """What a plan depends on besides its intents: the pipeline's definition, the instance's
    `models` and `plugins` configuration and its lockfile."""

    pipeline: str
    config: str | None
    lock: str | None

    @classmethod
    def of_record(cls, record: RunRecord) -> "Digests":
        """The digests the run used."""
        return cls(record.pipeline_digest, record.config_digest, record.lock_digest)

    @classmethod
    def of_instance(cls, instance: Instance, name: str) -> "Digests | None":
        """The instance's digests now; `None` when it no longer has pipeline `name`."""
        pipeline = instance.pipelines.get(name)
        if pipeline is None:
            return None
        return cls(pipeline_digest(pipeline), instance.config_digest(), instance.lock_digest())


def plan_document(plan: ChangeSet) -> dict[str, Any]:
    """The change set as the binding sees it: `to_dict` without what depends on the run's id
    (the id itself, the dry-run flag and the notifications' dedupe keys)."""
    doc = to_dict(plan)
    doc["run"] = {"pipeline": plan.pipeline, "status": plan.run_status}
    for note in doc["notifications"]:
        del note["dedupe_key"]
    return doc


def binding_digest(record: RunRecord, plan: ChangeSet, digests: Digests) -> str:
    """What an approval is bound to (decision 77 C): SHA-256 of the canonical JSON of the
    pipeline, `digests`, the run's context and input hash, every proposed intent with its
    route, and the planned change set (`plan_document`)."""
    return sha256_hex(
        {
            "v": BINDING_VERSION,
            "pipeline": record.pipeline,
            "digests": {
                "pipeline": digests.pipeline,
                "config": digests.config,
                "lock": digests.lock,
            },
            "context": record.context.model_dump(mode="json"),
            "input_hash": record.input_hash,
            "intents": [p.model_dump(mode="json") for p in record.intents],
            "plan": plan_document(plan),
        }
    )


def _same(a: str, b: str | None) -> bool:
    return b is not None and hmac.compare_digest(a.encode(), b.encode())


class ApprovalError(Exception):
    """A request or an approval that cannot go on: `kind` (`unknown_pipeline`,
    `too_many_pending`, `rate_limited`, `pipeline_locked`, `timeout`, `unknown_approval`,
    `not_pending`, `revoked`, `plan_mismatch`, ...) and a message without secrets."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Requested:
    """The answer to a `pipeline.run` request."""

    approval_id: str
    status: Literal["pending", "void"]
    record: RunRecord
    plan: ChangeSet
    expires_at: datetime | None
    reason: str | None


def _no_approval(record: RunRecord, plan: ChangeSet) -> str | None:
    """Why a dry run gets no approval, else `None`."""
    if record.status == "failed":
        return "run_failed"
    if plan.refused is not None:
        return plan.refused.kind
    if plan.problems:
        return "plan_has_problems"
    if plan.empty:
        return "nothing_to_apply"
    return None


async def _plan(instance: Instance, record: RunRecord, store: Store, sinks: Any) -> ChangeSet:
    def notified(key: str) -> bool:
        return store.get_notification(key) is not None

    return await plan_changes(
        record,
        sinks,
        notified=notified,
        known_task=store.get_task,
        call_timeout_s=instance.config.plugin_defaults.call_timeout_s,
    )


async def request_dry_run(
    instance: Instance,
    name: str,
    who: Requester,
    *,
    ttl: timedelta = DEFAULT_TTL,
    now: Callable[[], datetime] = _now,
) -> Requested:
    """Dry-run pipeline `name` for `who` and open an approval for what it would apply (see
    the module docstring). Raises `ApprovalError` (`unknown_pipeline`, `too_many_pending`,
    `rate_limited`, `pipeline_locked`, `timeout`); a crash marks the approval `void` and
    propagates."""
    if name not in instance.pipelines:
        raise ApprovalError("unknown_pipeline", f"unknown pipeline {name!r}")
    approval_id = new_approval_id()
    with Store.open(instance.store_path) as store:
        store.approval_expire(at=now())
        try:
            store.approval_open(
                approval_id,
                name,
                token_id=who.token_id,
                requested_by=who.name,
                at=now(),
                max_open=MAX_OPEN,
                max_per_hour=MAX_PER_HOUR,
            )
        except ApprovalLimited as limited:
            raise ApprovalError(limited.kind, str(limited)) from None
        try:
            async with live_ports(instance) as live:
                record = await Runner(instance, store, live.ports).run(name, dry_run=True)
                plan = await _plan(instance, record, store, live.sinks)
        except PipelineLocked:
            store.approval_void(approval_id, at=now(), reason="pipeline_locked")
            raise ApprovalError(
                "pipeline_locked", f"pipeline {name!r} is running; ask again later"
            ) from None
        except RunTimedOut as timed_out:
            run_id = None if timed_out.record is None else timed_out.record.id
            store.approval_void(approval_id, at=now(), reason="timeout", dry_run_id=run_id)
            raise ApprovalError("timeout", "the dry run timed out") from None
        except BaseException as exc:
            try:
                store.approval_void(
                    approval_id, at=now(), reason=f"the dry run crashed ({type(exc).__name__})"
                )
            except Exception:  # the original error matters more
                logger.warning("approval %s could not be marked void", approval_id)
            raise
        reason = _no_approval(record, plan)
        if reason is not None:
            store.approval_void(approval_id, at=now(), reason=reason, dry_run_id=record.id)
            return Requested(approval_id, "void", record, plan, None, reason)
        binding = binding_digest(record, plan, Digests.of_record(record))
        expires_at = now() + ttl
        store.approval_planned(
            approval_id,
            dry_run_id=record.id,
            binding_sha256=binding,
            expires_at=expires_at,
            at=now(),
        )
        return Requested(approval_id, "pending", record, plan, expires_at, None)


@dataclass(frozen=True)
class Preview:
    """A pending approval ready for the user's decision: its row, the dry run, and the change
    set it would apply now (whose digest matched)."""

    row: ApprovalRow
    dry: RunRecord
    plan: ChangeSet


def _pending(store: Store, approval_id: str, at: datetime) -> ApprovalRow:
    """The approval, pending and usable at `at`, else `ApprovalError` (recording an expiry or a
    revoked requester)."""
    store.approval_expire(at=at)
    row = store.approval(approval_id)
    if row is None:
        raise ApprovalError("unknown_approval", f"no approval {approval_id!r}")
    if row.status != "pending":
        raise ApprovalError("not_pending", f"approval {approval_id} is {row.status}")
    if row.token_id is not None:
        token = next((t for t in store.mcp_tokens() if t.id == row.token_id), None)
        if token is None or token.revoked_at is not None:
            store.approval_decide(
                approval_id, "revoked", at=at, reason="the MCP token that asked was revoked"
            )
            raise ApprovalError("revoked", f"approval {approval_id}: its MCP token was revoked")
    return row


def _refuse(store: Store, approval_id: str, at: datetime, why: str) -> ApprovalError:
    store.approval_decide(approval_id, "refused", at=at, reason=f"{MISMATCH}: {why}")
    return ApprovalError("plan_mismatch", f"approval {approval_id} refused: {why}")


async def preview(
    instance: Instance, approval_id: str, *, now: Callable[[], datetime] = _now
) -> Preview:
    """Check that `approval_id` is pending and that what it would apply is still exactly
    what was approved for; raises `ApprovalError` otherwise (a mismatch is recorded
    `refused`). Applies nothing."""
    with Store.open(instance.store_path) as store:
        recover_stuck(instance, store, at=now())
        row = _pending(store, approval_id, now())
        dry = None if row.dry_run_id is None else store.get_run(row.dry_run_id)
        if dry is None:
            raise _refuse(store, approval_id, now(), "its dry run is no longer in the store")
        digests = Digests.of_instance(instance, row.pipeline)
        if digests is None:
            raise _refuse(store, approval_id, now(), f"pipeline {row.pipeline!r} is gone")
        async with live_ports(instance) as live:
            plan = await _plan(instance, dry, store, live.sinks)
        if not _same(binding_digest(dry, plan, digests), row.binding_sha256):
            raise _refuse(
                store,
                approval_id,
                now(),
                "the plan, the pipeline, its configuration or the vault changed since the dry run",
            )
        return Preview(row, dry, plan)


async def apply(
    instance: Instance, approved: Preview, *, now: Callable[[], datetime] = _now
) -> RunRecord:
    """Claim the approval (single use) and apply its plan as a new run, re-planned and
    re-checked under the apply lock; the run's record. The approval ends `applied`, `failed`
    or `refused` (a mismatch: the run is `failed`, nothing applied). Raises `ApprovalError`
    when the claim fails (`not_pending`: expired, denied, revoked or already used meanwhile)
    and what `Runner.apply_planned` raises (`PipelineLocked`: still `pending`)."""
    row = approved.row
    digests = Digests.of_instance(instance, row.pipeline)
    binding = row.binding_sha256

    def verify(record: RunRecord, planned: ChangeSet) -> str | None:
        if digests is not None and _same(binding_digest(record, planned, digests), binding):
            return None
        return (
            f"{MISMATCH}: the plan of approval {row.id} changed before it was applied; "
            "nothing was applied"
        )

    with Store.open(instance.store_path) as store:
        run_id: list[str] = []

        def claim() -> bool:
            # The pipeline lock is held: settle what a dead process left `approving` first.
            store.approval_recover(row.pipeline, at=now())
            return store.approval_claim(row.id, at=now())

        def begun(new_run: str) -> None:
            run_id.append(new_run)
            store.approval_started(row.id, new_run)

        try:
            async with live_ports(instance) as live:
                record = await Runner(instance, store, live.ports).apply_planned(
                    approved.dry, verify, claim=claim, on_begin=begun
                )
        except ClaimRefused:
            store.approval_expire(at=now())
            current = store.approval(row.id)
            status = "gone" if current is None else current.status
            raise ApprovalError("not_pending", f"approval {row.id} is {status}") from None
        except PipelineLocked:
            raise
        except BaseException as exc:
            # A cancellation (a signal) after the shielded apply still saved the run: its
            # record says what happened. Only a run with no record was interrupted.
            current = store.approval(row.id)
            if current is not None and current.status == "approving":
                saved = store.get_run(run_id[0]) if run_id else None
                if saved is not None:
                    _finish(store, row.id, saved, now())
                else:
                    store.approval_finish(
                        row.id,
                        "failed",
                        at=now(),
                        run_id=run_id[0] if run_id else None,
                        reason=f"interrupted: the apply did not finish ({type(exc).__name__})",
                    )
            raise
        _finish(store, row.id, record, now())
        return record


def _finish(store: Store, approval_id: str, record: RunRecord, at: datetime) -> None:
    """End an `approving` approval from the record of the run that applied it."""
    outcome: Literal["applied", "failed", "refused"] = "applied"
    if record.status == "failed":
        outcome = "refused" if (record.error or "").startswith(MISMATCH) else "failed"
    store.approval_finish(
        approval_id,
        outcome,
        at=at,
        run_id=record.id,
        reason=None if outcome == "applied" else record.error,
    )


def recover_stuck(instance: Instance, store: Store, *, at: datetime) -> list[str]:
    """Settle `approving` approvals a dead process left (decision 77 E): for each pipeline
    that has one and whose lock is free now (no apply of it is running), its dead runs are
    recovered and the approvals settled from them (`Store.approval_recover`). A pipeline
    whose lock is busy is left for later. The ids settled."""
    pipelines = {r.pipeline for r in store.approvals(status="approving", limit=MAX_LIST_RUNS)}
    settled: list[str] = []
    for name in sorted(pipelines):
        lock = PipelineLock.try_acquire(instance.state_dir, name)
        if lock is None:
            continue
        try:
            recover_pipeline(store, name, clock=lambda: at)
            settled.extend(store.approval_recover(name, at=at))
        finally:
            lock.release()
    return settled


def deny(
    store: Store,
    approval_id: str,
    *,
    at: datetime,
    status: Literal["denied", "revoked"] = "denied",
) -> ApprovalRow:
    """Deny (or revoke) a pending approval; its row. `ApprovalError` when it is unknown or no
    longer pending."""
    store.approval_expire(at=at)
    row = store.approval(approval_id)
    if row is None:
        raise ApprovalError("unknown_approval", f"no approval {approval_id!r}")
    reason = "denied by the user" if status == "denied" else "revoked by the user"
    if not store.approval_decide(approval_id, status, at=at, reason=reason):
        current = store.approval(approval_id)
        shown = row.status if current is None else current.status
        raise ApprovalError("not_pending", f"approval {approval_id} is {shown}")
    found = store.approval(approval_id)
    assert found is not None  # noqa: S101 - it was just updated
    return found


__all__ = [
    "DEFAULT_TTL",
    "ID_PREFIX",
    "MAX_OPEN",
    "MAX_PER_HOUR",
    "MAX_TTL_MINUTES",
    "MIN_TTL_MINUTES",
    "MISMATCH",
    "STDIO",
    "ApprovalError",
    "Digests",
    "Preview",
    "Requested",
    "Requester",
    "apply",
    "binding_digest",
    "deny",
    "new_approval_id",
    "plan_document",
    "preview",
    "recover_stuck",
    "request_dry_run",
]
