# SPDX-License-Identifier: Apache-2.0
"""Validate the intents a plugin proposes (PLAN.md §2.1 tenets 3 and 4, §3.3; ADR 0002).

`validate_intents(payloads, manifest)` takes the raw intents a plugin returned (already
parsed JSON, e.g. from an MCP tool result) and the manifest of the plugin that proposed them.
It returns the typed intents, or raises `IntentRejected` with one `IntentRejection` per
problem. Each intent is checked, in this order:

1. it is an object whose `type` is in the closed set (`intent_invalid` otherwise);
2. its type is in the plugin's `permissions.intents` (`intent_undeclared`). This comes before
   the schema check, so an undeclared intent is reported as such whatever its content;
3. it validates against its model, the source of the JSON Schema in `schemas/intents/`
   (`intent_invalid`);
4. for `http.post`: the body serializes as standard JSON (no `NaN` or `Infinity`), and the
   host, as parsed by `url_host` (the one URL parser of the contracts), is one of the
   plugin's `permissions.network` hosts: exact match, case-insensitive, no suffix or
   wildcard matching (`host_undeclared`).

**Batch semantics: all or nothing.** A plugin's proposals for one step are one batch. If any
intent is rejected, the whole batch is rejected and the step fails: nothing from it is
journalled or applied, not even the valid intents. Every intent is still checked, so the
rejection lists every problem, not only the first. The batch itself must be a list
(`batch_invalid`) of at most `MAX_INTENTS` intents (`batch_too_large`).

**Messages are safe to log and show.** They never echo untrusted content: a value that
failed validation is never quoted, and an unknown `type` or key is shown only as
`safe_excerpt`, a short excerpt without control or format characters.

Nothing here applies an intent or touches the network: resolving a declared host through the
egress proxy, capping the serialized `http.post` body and applying intents are for the sinks
and the runtime.
"""

import json
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from horae.contracts.intents import INTENT_TYPES, HttpPost, Intent, IntentType, intent_adapter
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import StepError
from horae.contracts.types import MAX_ITEMS, url_host

MAX_INTENTS = MAX_ITEMS
"""Upper bound on the intents one plugin may propose in one step."""

RejectionCode = Literal[
    "batch_invalid",
    "batch_too_large",
    "intent_invalid",
    "intent_undeclared",
    "host_undeclared",
]
"""Why a proposal was rejected."""

STEP_ERROR_KIND = "intent_rejected"
"""`StepError.kind` of a step whose intents were rejected."""

_EXCERPT = 24
_MAX_MESSAGE = 200
_MAX_STEP_MESSAGE = 4096
_MAX_ERRORS_SHOWN = 3


def safe_excerpt(value: object, limit: int = _EXCERPT) -> str:
    """A short, quoted excerpt of an untrusted string, safe to log or show.

    Control, format (e.g. bidi overrides, zero-width) and other non-printing characters
    (Unicode category `C*`) are dropped, then the text is cut to `limit` characters. A value
    that is not a string is shown as its type name only."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    kept: list[str] = []
    truncated = False
    for char in value:
        if unicodedata.category(char).startswith("C"):
            continue
        if len(kept) == limit:
            truncated = True
            break
        kept.append(char)
    return "'" + "".join(kept) + ("…" if truncated else "") + "'"


@dataclass(frozen=True)
class IntentRejection:
    """One problem with a plugin's proposed intents."""

    plugin: str
    """Name of the proposing plugin (from its manifest, so trusted)."""
    index: int | None
    """Position of the intent in the batch; `None` for a problem with the batch itself."""
    intent_type: IntentType | None
    """The intent's type, when it is one of the closed set."""
    code: RejectionCode
    message: str
    """A short reason that never echoes untrusted content."""

    def describe(self) -> str:
        """One line: `#<index> <type> <code>: <message>`."""
        where = "batch" if self.index is None else f"#{self.index}"
        what = f" {self.intent_type}" if self.intent_type else ""
        return f"{where}{what} {self.code}: {self.message}"


class IntentRejected(Exception):
    """A plugin's intents for a step were rejected; the step fails and nothing is applied."""

    def __init__(self, plugin: str, rejections: tuple[IntentRejection, ...]) -> None:
        self.plugin = plugin
        self.rejections = rejections
        super().__init__(self.step_error().message)

    def step_error(self) -> StepError:
        """The rejection as the failed step's `StepError` for the run record."""
        head = f"plugin {self.plugin!r} proposed {len(self.rejections)} rejected intent(s)"
        lines = [head, *(r.describe() for r in self.rejections)]
        message = "\n".join(lines)
        if len(message) > _MAX_STEP_MESSAGE:
            message = message[: _MAX_STEP_MESSAGE - 1] + "…"
        return StepError(kind=STEP_ERROR_KIND, message=message)


def _schema_reason(error: ValidationError) -> str:
    """Where and how a payload failed, from pydantic's locations and error types only.

    pydantic's `msg` is not used: custom validators may quote the input. Keys in a location
    can be untrusted (an unknown key), so they go through `safe_excerpt`."""
    parts: list[str] = []
    errors = error.errors(include_url=False, include_input=False, include_context=False)
    for item in errors[:_MAX_ERRORS_SHOWN]:
        loc = item["loc"][1:]  # the first element is the discriminator tag, i.e. the type
        path = ".".join(
            str(p) if isinstance(p, int) else safe_excerpt(p, limit=16).strip("'") for p in loc
        )
        parts.append(f"{path or '<intent>'}: {item['type']}")
    if len(errors) > _MAX_ERRORS_SHOWN:
        parts.append(f"and {len(errors) - _MAX_ERRORS_SHOWN} more")
    return _cap("schema validation failed: " + "; ".join(parts))


def _cap(message: str) -> str:
    return message if len(message) <= _MAX_MESSAGE else message[: _MAX_MESSAGE - 1] + "…"


def _check_one(
    payload: object, manifest: PluginManifest, hosts: frozenset[str]
) -> Intent | tuple[IntentType | None, RejectionCode, str]:
    if not isinstance(payload, dict):
        return (
            None,
            "intent_invalid",
            f"an intent must be an object, got <{type(payload).__name__}>",
        )
    raw_type = payload.get("type")
    if not isinstance(raw_type, str) or raw_type not in INTENT_TYPES:
        shown = safe_excerpt(raw_type)
        return None, "intent_invalid", f"intent type {shown} is not in the closed set"
    intent_type: IntentType = raw_type  # narrowed by the membership check
    if intent_type not in manifest.permissions.intents:
        return intent_type, "intent_undeclared", "not in the plugin's permissions.intents"
    try:
        intent = intent_adapter.validate_python(payload)
    except ValidationError as error:
        return intent_type, "intent_invalid", _schema_reason(error)
    if isinstance(intent, HttpPost):
        try:
            json.dumps(intent.body, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return intent_type, "intent_invalid", "body is not standard JSON"
        host = url_host(intent.url).lower()
        if host not in hosts:
            shown = safe_excerpt(host, limit=64)
            return intent_type, "host_undeclared", f"URL host {shown} is not in permissions.network"
    return intent


def validate_intents(payloads: object, manifest: PluginManifest) -> tuple[Intent, ...]:
    """Validate the intents `manifest`'s plugin proposed for one step, all or nothing.

    Returns the typed intents in order, or raises `IntentRejected` listing every problem."""
    plugin = manifest.name

    def reject(code: RejectionCode, message: str) -> IntentRejected:
        return IntentRejected(plugin, (IntentRejection(plugin, None, None, code, message),))

    if not isinstance(payloads, list):
        raise reject("batch_invalid", f"intents must be a list, got <{type(payloads).__name__}>")
    if len(payloads) > MAX_INTENTS:
        raise reject("batch_too_large", f"more than {MAX_INTENTS} intents in one step")

    hosts = frozenset(host.lower() for host in manifest.permissions.network)
    intents: list[Intent] = []
    rejections: list[IntentRejection] = []
    for index, payload in enumerate(payloads):
        outcome = _check_one(payload, manifest, hosts)
        if isinstance(outcome, tuple):
            intent_type, code, message = outcome
            rejections.append(IntentRejection(plugin, index, intent_type, code, message))
        else:
            intents.append(outcome)
    if rejections:
        raise IntentRejected(plugin, tuple(rejections))
    return tuple(intents)


__all__ = [
    "MAX_INTENTS",
    "STEP_ERROR_KIND",
    "IntentRejected",
    "IntentRejection",
    "RejectionCode",
    "safe_excerpt",
    "validate_intents",
]
