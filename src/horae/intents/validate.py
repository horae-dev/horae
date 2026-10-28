# SPDX-License-Identifier: Apache-2.0
"""Validate the intents a step proposes (PLAN.md §2.1 tenets 3 and 4, §3.3; ADR 0002).

`validate_intents(payloads, permissions=..., source=...)` takes the raw intents one step
returned (already parsed JSON, e.g. from an MCP tool result or the LLM's structured output),
the permissions that bound them and the name of their `source`: the plugin's name, or
`skill:<name>` for the LLM step. `validate_plugin_intents(payloads, manifest)` is the same
check for a plugin, from its manifest. Both return the typed intents, or raise
`IntentRejected` with one `IntentRejection` per problem. Each intent is checked, in order:

1. it is an object whose `type` is in the closed set (`intent_invalid` otherwise);
2. its type is in `permissions.intents` (`intent_undeclared`). This comes before every other
   check, so an undeclared intent is reported as such whatever its content;
3. it has at most 16 keys (`intent_invalid`), checked before pydantic so a huge
   object costs nothing to reject;
4. it validates against its model, the source of the JSON Schema in `schemas/intents/`
   (`intent_invalid`);
5. for `http.post`, first the host: as parsed by `url_host` (the one URL parser of the
   contracts), it must be one of `permissions.network`: exact match, no suffix or wildcard
   matching, any port (`host_undeclared`). Then the body (`intent_invalid`): plain JSON data
   (objects with string keys, arrays, strings, integers, finite floats, booleans, `null`),
   nested at most `MAX_BODY_DEPTH` deep, whose compact serialization is at most `MAX_BODY`
   UTF-8 bytes.

**Batch semantics: all or nothing.** One step's proposals are one batch. If any intent is
rejected, the whole batch is rejected and the step fails: nothing from it is journalled or
applied, not even the valid intents. Every intent is still checked, so the rejection lists
every problem, not only the first. The batch itself must be a list (`batch_invalid`) of at
most `MAX_INTENTS` intents (`batch_too_large`). `MAX_INTENTS` bounds **one step**: the
engine (#17) must also keep the run-wide total within `RunRecord.intents`'s bound,
`horae.contracts.MAX_RUN_INTENTS`.

**What the engine records.** Rejected proposals never enter `RunRecord.intents`: the
`StepError` from `IntentRejected.step_error()` (kind `STEP_ERROR_KIND`) is the only record of
them. For a `host_undeclared` rejection the engine also records
`Violation(kind="egress_blocked")`.

**Messages are safe to log and show.** A value from the payload is echoed only when it
matches `^[A-Za-z0-9._-]{1,32}$` (an identifier-alphabet token: no whitespace, control,
format, line-separator or markup characters); any other value is shown as
`<invalid: N chars>`, or `<type>` when it is not a string. Schema errors show only model
field names and pydantic error types. Each rejection echoes at most one such token and never a
space, so a batch of hostile intents cannot spell a phrase in the step error.

Nothing here applies an intent or touches the network: resolving a declared host through the
egress proxy, refusing redirects and applying intents are for the sinks and the runtime.
"""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from horae.contracts.intents import (
    INTENT_MODELS,
    INTENT_TYPES,
    HttpPost,
    Intent,
    IntentType,
    intent_adapter,
)
from horae.contracts.plugin import Permissions, PluginManifest
from horae.contracts.run import StepError
from horae.contracts.types import MAX_BODY, MAX_ITEMS, url_host

MAX_INTENTS = MAX_ITEMS
"""Upper bound on the intents one step may propose. Per step only: the run-wide total is
bounded by `horae.contracts.MAX_RUN_INTENTS`, which the engine enforces."""

MAX_BODY_DEPTH = 64
"""Deepest nesting of arrays and objects allowed in an `http.post` body."""

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

_MAX_KEYS = 16
"""More keys than any intent model has fields; a larger object is rejected unparsed."""

_ECHO = re.compile(r"[A-Za-z0-9._-]{1,32}")
_EXCERPT = 24
_MAX_MESSAGE = 200
_MAX_STEP_MESSAGE = 4096
_MAX_ERRORS_SHOWN = 3

_SCALARS = (str, int, bool, type(None))


def _echo(value: object) -> str:
    """`value` quoted if it is an identifier-alphabet token, else only its size or type."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    if _ECHO.fullmatch(value):
        return f"'{value}'"
    return f"<invalid: {len(value)} chars>"


def safe_excerpt(value: object, limit: int = _EXCERPT) -> str:
    """A short, quoted excerpt of an untrusted string, for logs.

    Control, format (bidi overrides, zero-width…) and other non-printing characters (Unicode
    category `C*`) and separators other than U+0020 (`Z*`: line and paragraph separators,
    no-break and other spaces) are dropped, then the text is cut to `limit` characters. A
    value that is not a string is shown as its type name only.

    This removes characters that forge lines or hide text; it does not make the words safe.
    Rejection messages do not use it: they echo only identifier-alphabet tokens."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    kept: list[str] = []
    truncated = False
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("C") or (category.startswith("Z") and char != " "):
            continue
        if len(kept) == limit:
            truncated = True
            break
        kept.append(char)
    return "'" + "".join(kept) + ("…" if truncated else "") + "'"


@dataclass(frozen=True)
class IntentRejection:
    """One problem with a step's proposed intents."""

    source: str
    """Who proposed the intents: a plugin name or `skill:<name>` (set by the engine, so
    trusted)."""
    index: int | None
    """Position of the intent in the batch; `None` for a problem with the batch itself."""
    intent_type: IntentType | None
    """The intent's type, when it is one of the closed set."""
    code: RejectionCode
    message: str
    """A short reason that echoes only identifier-alphabet tokens of the payload."""

    def describe(self) -> str:
        """One line: `#<index> <type> <code>: <message>`."""
        where = "batch" if self.index is None else f"#{self.index}"
        what = f" {self.intent_type}" if self.intent_type else ""
        return f"{where}{what} {self.code}: {self.message}"


class IntentRejected(Exception):
    """A step's intents were rejected; the step fails and nothing is applied."""

    def __init__(self, source: str, rejections: tuple[IntentRejection, ...]) -> None:
        super().__init__(source, rejections)  # `args` rebuilds the exception when unpickled
        self.source = source
        self.rejections = rejections

    def __str__(self) -> str:
        return self.step_error().message

    def step_error(self) -> StepError:
        """The rejection as the failed step's `StepError`: the only record of the rejected
        proposals in the run record."""
        head = f"{self.source!r} proposed {len(self.rejections)} rejected intent(s)"
        lines = [head, *(r.describe() for r in self.rejections)]
        message = "\n".join(lines)
        if len(message) > _MAX_STEP_MESSAGE:
            message = message[: _MAX_STEP_MESSAGE - 1] + "…"
        return StepError(kind=STEP_ERROR_KIND, message=message)


def _schema_reason(intent_type: IntentType, error: ValidationError) -> str:
    """Where and how a payload failed, from pydantic's locations and error types only.

    pydantic's `msg` is not used: custom validators may quote the input. A location segment
    is shown only if it is a field name of the intent's model; anything else (an unknown
    key, an index, a union member tag) is shown as `<key>`."""
    fields = INTENT_MODELS[intent_type].model_fields
    parts: list[str] = []
    errors = error.errors(include_url=False, include_input=False, include_context=False)
    for item in errors[:_MAX_ERRORS_SHOWN]:
        loc = item["loc"][1:]  # the first element is the discriminator tag, i.e. the type
        path = ".".join(p if isinstance(p, str) and p in fields else "<key>" for p in loc)
        parts.append(f"{path or '<intent>'}: {item['type']}")
    if len(errors) > _MAX_ERRORS_SHOWN:
        parts.append(f"and {len(errors) - _MAX_ERRORS_SHOWN} more")
    return _cap("schema validation failed: " + "; ".join(parts))


def _cap(message: str) -> str:
    return message if len(message) <= _MAX_MESSAGE else message[: _MAX_MESSAGE - 1] + "…"


def _body_problem(body: object) -> str | None:
    """Why `body` is not a plain, bounded JSON value; `None` if it is one.

    Walks the value iteratively (no recursion), so any nesting is safe to check. Each value
    serializes to at least one byte, so more than `MAX_BODY` values is already too large."""
    stack: list[tuple[object, int]] = [(body, 0)]
    seen = 0
    while stack:
        value, depth = stack.pop()
        seen += 1
        if seen > MAX_BODY:
            return f"body is larger than {MAX_BODY} bytes"
        if type(value) in _SCALARS:
            continue
        if type(value) is float:
            if not math.isfinite(value):
                return "body contains a non-finite number"
            continue
        if type(value) is not dict and type(value) is not list:
            return f"body contains a <{type(value).__name__}>, not a JSON value"
        if depth == MAX_BODY_DEPTH:
            return f"body is nested deeper than {MAX_BODY_DEPTH}"
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                return "body has an object key that is not a string"
            stack.extend((item, depth + 1) for item in value.values())
        elif type(value) is list:
            stack.extend((item, depth + 1) for item in value)
    encoded = json.dumps(body, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_BODY:
        return f"body is larger than {MAX_BODY} bytes"
    return None


def _check_one(
    payload: object, index: int, permissions: Permissions, source: str, hosts: frozenset[str]
) -> tuple[Intent, None] | tuple[None, IntentRejection]:
    """The typed intent, or why `payload` is rejected: exactly one of the two is set."""

    def reject(
        intent_type: IntentType | None, code: RejectionCode, message: str
    ) -> tuple[None, IntentRejection]:
        return None, IntentRejection(source, index, intent_type, code, message)

    if not isinstance(payload, dict):
        return reject(
            None, "intent_invalid", f"an intent must be an object, got <{type(payload).__name__}>"
        )
    raw_type = payload.get("type")
    if not isinstance(raw_type, str) or raw_type not in INTENT_TYPES:
        return reject(None, "intent_invalid", f"intent type {_echo(raw_type)} is not in the set")
    intent_type: IntentType = raw_type  # narrowed by the membership check
    if intent_type not in permissions.intents:
        return reject(intent_type, "intent_undeclared", "not in permissions.intents")
    if len(payload) > _MAX_KEYS:
        return reject(intent_type, "intent_invalid", f"too many keys (more than {_MAX_KEYS})")
    try:
        intent = intent_adapter.validate_python(payload)
    except ValidationError as error:
        return reject(intent_type, "intent_invalid", _schema_reason(intent_type, error))
    if isinstance(intent, HttpPost):
        host = url_host(intent.url)  # lowercase: `HttpsUrl` only accepts lowercase hosts
        if host not in hosts:
            message = f"URL host {_echo(host)} is not in permissions.network"
            return reject(intent_type, "host_undeclared", message)
        problem = _body_problem(intent.body)
        if problem is not None:
            return reject(intent_type, "intent_invalid", problem)
    return intent, None


def validate_intents(
    payloads: object, *, permissions: Permissions, source: str
) -> tuple[Intent, ...]:
    """Validate the intents one step proposed, all or nothing.

    `permissions` bound what may be proposed; `source` names the proposer in rejections (a
    plugin name, or `skill:<name>` for the LLM step). Returns the typed intents in order, or
    raises `IntentRejected` listing every problem."""

    def reject(code: RejectionCode, message: str) -> IntentRejected:
        return IntentRejected(source, (IntentRejection(source, None, None, code, message),))

    if not isinstance(payloads, list):
        raise reject("batch_invalid", f"intents must be a list, got <{type(payloads).__name__}>")
    if len(payloads) > MAX_INTENTS:
        raise reject("batch_too_large", f"more than {MAX_INTENTS} intents in one step")

    # `Hostname` is lowercase by contract; lowering here keeps the comparison
    # case-insensitive even for a `Permissions` built without validation.
    hosts = frozenset(host.lower() for host in permissions.network)
    intents: list[Intent] = []
    rejections: list[IntentRejection] = []
    for index, payload in enumerate(payloads):
        intent, rejection = _check_one(payload, index, permissions, source, hosts)
        if rejection is not None:
            rejections.append(rejection)
        if intent is not None:
            intents.append(intent)
    if rejections:
        raise IntentRejected(source, tuple(rejections))
    return tuple(intents)


def validate_plugin_intents(payloads: object, manifest: PluginManifest) -> tuple[Intent, ...]:
    """`validate_intents` for a plugin: its manifest's permissions, its name as the source."""
    return validate_intents(payloads, permissions=manifest.permissions, source=manifest.name)


__all__ = [
    "MAX_BODY_DEPTH",
    "MAX_INTENTS",
    "STEP_ERROR_KIND",
    "IntentRejected",
    "IntentRejection",
    "RejectionCode",
    "safe_excerpt",
    "validate_intents",
    "validate_plugin_intents",
]
