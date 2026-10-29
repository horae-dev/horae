# SPDX-License-Identifier: Apache-2.0
"""How a run is shown to a client: `horae runs --json` (#26) and the HTTP API's `/runs` (#37)
share this representation (schemas decision 51 C).

It is the store's `RunSummary` (the indexed columns), never the run record: no step output,
LLM text, journal bytes or vault content. The run's `pid` and `host` are left out. Callers
redact the document before they send it (`horae.cli_output.to_json`, the API's response)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from horae.runner import lock_is_free
from horae.store import RunSummary


def iso(value: datetime | None) -> str | None:
    """A time as `YYYY-MM-DDTHH:MM:SSZ` (UTC, whole seconds)."""
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stale_flags(state_dir: Path, runs: Sequence[RunSummary]) -> list[bool]:
    """Per run, whether it is `running` with a free pipeline lock: no live process holds it,
    so the next run of the pipeline will abandon it. Checked without waiting and without
    creating anything."""
    return [
        run.status == "running" and lock_is_free(state_dir, run.pipeline) is True for run in runs
    ]


def run_json(run: RunSummary, stale: bool) -> dict[str, Any]:
    """One run as a JSON-ready object (not yet redacted)."""
    duration = run.duration
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "stale": stale,
        "dry_run": run.dry_run,
        "abandoned": run.abandoned_at is not None,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "duration_s": None if duration is None else duration.total_seconds(),
        "cost": {
            "usd": run.cost.usd,
            "tokens_in": run.cost.tokens_in,
            "tokens_out": run.cost.tokens_out,
        },
        "error": run.error,
    }


__all__ = ["iso", "run_json", "stale_flags"]
