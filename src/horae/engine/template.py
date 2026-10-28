# SPDX-License-Identifier: Apache-2.0
"""Parameter templating: a tiny, strict substitution over the run context.

Syntax: `{{name}}` (spaces inside the braces are allowed, `{{ date }}`), where `name` matches
`[A-Za-z_][A-Za-z0-9_]*`. There is nothing else: no filters, no attribute or index access, no
expressions, no escapes, no loops. It is implemented with one regular expression, never Jinja
or `eval`.

Rules:

- **Only the run context fills templates.** The variables are `now` (RFC 3339), `timezone` and
  every key of `RunContext.vars` (`date`, `YYYY-MM-DD` in the run's time zone, plus the
  caller's extra variables). Step outputs are never variables: data read from outside cannot
  become a parameter of a later call (PLAN.md §2.1 tenet 5).
- **Unknown variables are an error** (`TemplateError`), as is any other `{{` or `}}` in a
  string (an unterminated or malformed placeholder). Literal double braces cannot be written
  in v1.
- **Single pass.** Substituted values are not scanned again, so a variable whose value
  contains `{{x}}` inserts that text literally.
- Only string values are rendered, at any depth of the parameters (mappings and lists);
  mapping keys and non-string scalars are kept as they are.

All parameters of a pipeline are rendered once, before the first step runs, so a template
error fails the run before any plugin is called.
"""

import datetime
import re
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae.contracts.run import RunContext

PLACEHOLDER = re.compile(r"\{\{ *([A-Za-z_][A-Za-z0-9_]*) *\}\}")

BUILTIN_VARS = ("now", "timezone", "date")
"""Variables the core defines; the caller's extra variables may not reuse these names."""


class TemplateError(ValueError):
    """A parameter could not be rendered: unknown variable or malformed placeholder."""


def make_context(
    now: datetime.datetime, timezone: str, extra: Mapping[str, str] | None = None
) -> RunContext:
    """The template context of a run started at `now` (an aware datetime) in `timezone`.

    `vars` holds `date` (the local date of `now` in `timezone`) and the caller's `extra`
    variables, which may not shadow a built-in name. The time zone must exist."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown time zone {timezone!r}") from error
    extra = dict(extra or {})
    shadowed = sorted(set(extra) & set(BUILTIN_VARS))
    if shadowed:
        raise ValueError(f"variables {shadowed} are built in and cannot be set")
    local = now.astimezone(zone)
    return RunContext(now=now, timezone=timezone, vars={"date": local.date().isoformat(), **extra})


def variables(context: RunContext) -> dict[str, str]:
    """The template namespace of `context`: `now`, `timezone` and `vars`."""
    return {
        "now": context.now.isoformat().replace("+00:00", "Z"),
        "timezone": context.timezone,
        **context.vars,
    }


def render_string(text: str, names: Mapping[str, str]) -> str:
    """Substitute every `{{name}}` in `text`; raise `TemplateError` on anything else."""
    parts: list[str] = []
    position = 0
    for match in PLACEHOLDER.finditer(text):
        parts.append(_literal(text[position : match.start()]))
        name = match.group(1)
        if name not in names:
            raise TemplateError(f"unknown template variable {name!r}")
        parts.append(names[name])
        position = match.end()
    parts.append(_literal(text[position:]))
    return "".join(parts)


def _literal(chunk: str) -> str:
    if "{{" in chunk or "}}" in chunk:
        raise TemplateError("malformed template placeholder (use `{{name}}`)")
    return chunk


def render(value: Any, names: Mapping[str, str]) -> Any:
    """A copy of `value` with every string rendered by `render_string`."""
    if isinstance(value, str):
        return render_string(value, names)
    if isinstance(value, dict):
        return {key: render(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, names) for item in value]
    return value
