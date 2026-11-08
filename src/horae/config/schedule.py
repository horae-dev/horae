# SPDX-License-Identifier: Apache-2.0
"""The effective scheduler mode (#39, schemas decision 49 C): `horae.yaml` `scheduler.mode`,
overridden by the `HORAE_SCHEDULER` environment variable. Shared by `horae serve` (which
refuses a bad value) and `horae doctor` (which reports it)."""

import os
from collections.abc import Mapping
from typing import get_args

from horae.contracts.instance import SchedulerMode

SCHEDULER_ENV = "HORAE_SCHEDULER"
"""Overrides `scheduler.mode` when set and not blank (`builtin` or `external`, any case)."""
MODES: tuple[SchedulerMode, ...] = get_args(SchedulerMode)
_SHOWN = 16
"""Characters of a bad `HORAE_SCHEDULER` value echoed (it is not a secret, but bounded)."""


class ModeError(ValueError):
    """`HORAE_SCHEDULER` holds something other than `builtin` or `external`."""


def scheduler_mode(
    configured: SchedulerMode, environ: Mapping[str, str] | None = None
) -> SchedulerMode:
    """The effective mode: `HORAE_SCHEDULER` when set and not blank (matched without regard
    to case), else `configured`. `ModeError` for any other value."""
    value = (os.environ if environ is None else environ).get(SCHEDULER_ENV, "").strip()
    if not value:
        return configured
    for mode in MODES:
        if value.lower() == mode:
            return mode
    shown = value[:_SHOWN].encode("ascii", "replace").decode("ascii")
    raise ModeError(f"{SCHEDULER_ENV}={shown!r}: expected one of {', '.join(MODES)}")


__all__ = ["MODES", "SCHEDULER_ENV", "ModeError", "scheduler_mode"]
