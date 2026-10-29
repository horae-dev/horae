# SPDX-License-Identifier: Apache-2.0
"""Cron expressions of pipeline triggers (#39, schemas decision 49 A and B), on `cronsim`.

The contract (`horae.contracts.pipeline.CronExpression`) fixes the shape: five fields, no
macros. `cronsim` parses the fields (names such as `MON` or `JAN`, `L` for the last day of the
month) and computes the fire times, in the trigger's time zone (else the instance's).

Daylight saving time follows Debian cron (the behaviour `cronsim` implements, pinned by
`tests/test_scheduler.py`):

- a time that does not exist (the hour skipped in spring) fires once, at the first instant
  after the gap (`30 2 * * *` in Europe/Paris on the last Sunday of March fires at 03:00);
- a time that exists twice (the hour repeated in autumn) fires once, at its first occurrence;
- an expression whose hour field is `*` (`*/30 * * * *`) fires at every real occurrence, so
  the repeated hour runs it twice and the skipped hour not at all.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError


def cron_problem(expression: str) -> str | None:
    """Why `expression` is not a usable cron expression (`None` when it is)."""
    try:
        CronSim(expression, datetime(2000, 1, 1, tzinfo=UTC))
    except CronSimError as exc:
        reason = str(exc).lower() or "invalid"
        return f"invalid cron expression {expression!r} ({reason})"
    return None


def next_fire(expression: str, timezone: str, after: datetime) -> datetime:
    """The first fire time of `expression`, evaluated in `timezone`, strictly after `after`
    (an aware datetime), as an aware UTC datetime. Raises `ValueError` on an invalid
    expression (the loader has already refused one)."""
    if after.tzinfo is None:
        raise ValueError("`after` must be an aware datetime")
    local = after.astimezone(ZoneInfo(timezone))
    try:
        fire = next(iter(CronSim(expression, local)))
    except CronSimError as exc:
        raise ValueError(str(exc)) from None
    return fire.astimezone(UTC)


__all__ = ["cron_problem", "next_fire"]
