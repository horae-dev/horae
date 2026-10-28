# SPDX-License-Identifier: Apache-2.0
"""Run one pipeline: lock, heartbeat, execute, persist, timeouts and stale-run recovery (#19).

See `horae.runner._runner` for the lifecycle, `horae.runner._lock` for the pipeline locks and
`horae.runner._ports` for the live ports of a run (`open_ports`: plugin host, registry, sinks)."""

from horae.runner._lock import (
    APPLY_LOCK,
    LOCKS_DIR,
    LocksUnsupported,
    PipelineLock,
    lock_is_free,
    lock_path,
    try_acquire_apply_lock,
)
from horae.runner._ports import LivePorts, open_ports, uses_llm
from horae.runner._runner import (
    APPLY_LOCK_WAIT_S,
    DEFAULT_HEARTBEAT_S,
    LOCK_RETRY_S,
    STOP_GRACE_S,
    PipelineLocked,
    Ports,
    RunLost,
    Runner,
    RunnerError,
    RunTimedOut,
    UnknownPipeline,
    new_run_id,
    recover_pipeline,
)

__all__ = [
    "APPLY_LOCK",
    "APPLY_LOCK_WAIT_S",
    "DEFAULT_HEARTBEAT_S",
    "LOCKS_DIR",
    "LOCK_RETRY_S",
    "STOP_GRACE_S",
    "LivePorts",
    "LocksUnsupported",
    "PipelineLock",
    "PipelineLocked",
    "Ports",
    "RunLost",
    "RunTimedOut",
    "Runner",
    "RunnerError",
    "UnknownPipeline",
    "lock_is_free",
    "lock_path",
    "new_run_id",
    "open_ports",
    "recover_pipeline",
    "try_acquire_apply_lock",
    "uses_llm",
]
