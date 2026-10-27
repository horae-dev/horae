# SPDX-License-Identifier: Apache-2.0
"""Shared base model and constrained types for the Horae contracts.

Every scalar is strict: a string is never coerced to a number, a number never to a string,
and a bool is never accepted where an int or float is expected. The one exception is dates
and times, which are ISO 8601 strings on the wire (JSON, MCP, SQLite): they are accepted as
strings matching the patterns below, or as ``date`` / ``datetime`` objects from Python code.

Regular expressions avoid look-around, ``\\d``, ``\\s``, ``\\w`` and other shorthand
classes, and use explicit ASCII ranges (``[^\\x00-\\x20\\x7f]``), so that pydantic (Rust
``regex``) and ECMA-262 JSON Schema validators accept the same strings. One known
difference remains with Python's ``jsonschema``, which evaluates ``pattern`` with ``re.search``:
there ``$`` also matches before a trailing newline, so ``"abc\\n"`` passes ``^[a-z]+$``.
ECMA-262 and pydantic reject it. See ``schemas/README.md``.
"""

import datetime
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    Strict,
    StringConstraints,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

T = TypeVar("T")

MAX_ITEMS = 256
"""Upper bound for lists and maps in every contract."""

MAX_CONTENT = 1024 * 1024
"""Upper bound for note content: 1 MiB, counted in Unicode code points (characters)."""


class Contract(BaseModel):
    """Base for every contract model: unknown keys are rejected.

    Strictness is per field (see the module docstring), not global, so that ISO 8601 date
    strings validate through ``validate_python`` as well as ``validate_json``.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


@dataclass(frozen=True, eq=False)
class JsonSchemaExtra:
    """Annotation that adds keywords to a type's generated JSON Schema."""

    keywords: dict[str, Any]

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema.update(self.keywords)
        return schema


def _unique[I](items: list[I]) -> list[I]:
    try:
        seen: set[Any] = set()
        for item in items:
            if item in seen:
                raise ValueError(f"duplicate item {item!r}")
            seen.add(item)
    except TypeError:  # unhashable items: quadratic fallback
        for index, item in enumerate(items):
            if item in items[:index]:
                raise ValueError(f"duplicate item {item!r}") from None
    return items


UniqueList = Annotated[
    list[T],
    AfterValidator(_unique),
    Field(max_length=MAX_ITEMS, json_schema_extra={"uniqueItems": True}),
]
"""A list of at most 256 unique items (`uniqueItems` in JSON Schema)."""

BoundedList = Annotated[list[T], Field(max_length=MAX_ITEMS)]
"""A list of at most 256 items."""

StrictInt = Annotated[int, Strict()]
StrictFloat = Annotated[float, Strict(), Field(allow_inf_nan=False)]
"""A finite number. An int is accepted, a bool or a numeric string is not."""
StrictBool = Annotated[bool, Strict()]


def _exact_int(value: Any) -> Any:
    if type(value) is not int:
        raise ValueError("expected an integer")
    return value


SchemaVersion1 = Annotated[Literal[1], BeforeValidator(_exact_int)]
"""`schema_version: 1`. `true` and `1.0` are rejected (both equal 1 in Python)."""

Count = Annotated[int, Strict(), Field(ge=0)]
"""A non-negative integer."""

# --- text ------------------------------------------------------------------------------------

_CTRL = r"\x00-\x1f\x7f"
_VISIBLE = r"[^\x00-\x20\x7f]"
"""Any character except control characters and whitespace (space included)."""

Line = Annotated[str, Strict(), StringConstraints(pattern=rf"^[^{_CTRL}]+$", max_length=500)]
"""One line of text, at most 500 characters, with no control characters (no newline)."""

Title = Line
"""A title: one line of text, at most 500 characters."""

Heading = Annotated[str, Strict(), StringConstraints(pattern=rf"^[^{_CTRL}]+$", max_length=200)]
"""A Markdown heading text without the leading `#`: one line, at most 200 characters."""

Token = Annotated[str, Strict(), StringConstraints(pattern=rf"^{_VISIBLE}+$", max_length=128)]
"""A short identifier with no whitespace or control characters, e.g. a model name."""

Message = Annotated[str, Strict(), StringConstraints(min_length=1, max_length=4096)]
"""Free text (newlines allowed), at most 4096 characters."""

Content = Annotated[str, Strict(), StringConstraints(max_length=MAX_CONTENT)]
"""Markdown content, at most 1 MiB (1 048 576 characters)."""

# --- names -----------------------------------------------------------------------------------

PLUGIN_NAME = r"[a-z][a-z0-9-]*"

PluginName = Annotated[str, Strict(), StringConstraints(pattern=rf"^{PLUGIN_NAME}$", max_length=64)]
"""A plugin name, e.g. `github` or `mcp-brief`."""

PluginRef = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{PLUGIN_NAME}\.[a-z][a-z0-9_]*$", max_length=128),
]
"""A plugin tool, `<namespace>.<tool>`, e.g. `github.review_requests`.

The namespace is not necessarily the name of the plugin that provides the tool (PLAN.md
§3.2 calls `coach.brief`, served by `plugin-mcp-brief`): how a namespace resolves to a
plugin is defined by the engine, not by this contract."""

SkillRef = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{PLUGIN_NAME}/[a-z][a-z0-9_-]*$", max_length=128),
]
"""A skill, `<namespace>/<skill>`, e.g. `daily-brief/rank-and-word`. The namespace resolves
like a `PluginRef` namespace."""

PipelineName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)
]
"""A pipeline name, e.g. `daily-brief`."""

SemVer = Annotated[
    str,
    Strict(),
    StringConstraints(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
        ),
        max_length=128,
    ),
]
"""A semantic version, e.g. `0.1.0`."""

CommitSha = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
"""A full git commit id: 40 (SHA-1) or 64 (SHA-256) lowercase hex characters."""

Sha256Hex = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A SHA-256 digest, as 64 lowercase hex characters."""

Fingerprint = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128)
]
"""A task's stable identity, e.g. `calendar:3b1f0c`: 1 to 128 of `A-Z a-z 0-9 . _ : -`."""

BlockId = Annotated[str, Strict(), StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
"""The id of a block Horae owns inside a note, e.g. `horae-brief`."""

IanaTimezone = Annotated[
    str,
    Strict(),
    StringConstraints(
        pattern=r"^[A-Za-z][A-Za-z0-9_+-]{0,63}(/[A-Za-z0-9_+-]{1,64}){0,3}$", max_length=128
    ),
]
"""An IANA time zone name, e.g. `Europe/Paris` or `UTC`. Only the syntax is checked here;
the runtime checks that the zone exists."""

_UNSAFE_ENV_NAME = r"^(HORAE(_|$)|LD_|DYLD_|PYTHON|PATH$|HOME$)"


def _check_secret(name: str) -> str:
    if re.match(_UNSAFE_ENV_NAME, name):
        raise ValueError(f"{name!r} is reserved or unsafe as a secret name")
    return name


SecretName = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128),
    AfterValidator(_check_secret),
    JsonSchemaExtra({"not": {"pattern": _UNSAFE_ENV_NAME}}),
]
"""An environment-variable style secret name, e.g. `GITHUB_TOKEN`.

It starts with a letter (not `_`). `HORAE_*` is reserved for the core; `LD_*`, `DYLD_*`,
`PYTHON*`, `PATH` and `HOME` change how a process loads code and are rejected."""

# --- hosts and URLs --------------------------------------------------------------------------

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_TOP_LABEL = r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
HOST = rf"({_LABEL}\.)*{_TOP_LABEL}"
"""A lowercase DNS name whose last label starts with a letter, so no IPv4 literal (or
decimal/hex IP form) matches; IPv6 literals contain `:` and never match."""

_LOCALHOST = r"(^|\.)localhost$"


def _is_localhost(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def _check_host(host: str) -> str:
    if _is_localhost(host):
        raise ValueError("localhost is not an allowed host")
    return host


Hostname = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{HOST}$", max_length=253),
    AfterValidator(_check_host),
    JsonSchemaExtra({"not": {"pattern": _LOCALHOST}}),
]
"""A lowercase DNS host name, without scheme, port or wildcard, e.g. `api.github.com`.

Single-label names (`coach`, a Docker or Coolify service name) are allowed. IP literals,
`localhost` and `*.localhost` are rejected."""

_PORT = r"(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3})"
_URL_REST = r"[^\x00-\x20\x7f\\#]"
_URL_LOCALHOST = r"^https://([^/?:]*\.)?localhost([:/?]|$)"
_URL_HOST = re.compile(r"https://([^/?:]+)")


def url_host(url: str) -> str:
    """The host of an `HttpsUrl`, as the runtime must compare it with `permissions.network`."""
    match = _URL_HOST.match(url)
    if match is None:
        raise ValueError(f"not an https URL: {url!r}")
    return match.group(1)


def _check_url(url: str) -> str:
    _check_host(url_host(url))
    return url


HttpsUrl = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^https://{HOST}(:{_PORT})?([/?]{_URL_REST}*)?$", max_length=2048),
    AfterValidator(_check_url),
    JsonSchemaExtra({"not": {"pattern": _URL_LOCALHOST}}),
]
"""An absolute `https` URL whose host is a `Hostname` (no IP literal, no `localhost`), with
an optional port. No user info, fragment, backslash, whitespace or control characters.
Kept as a plain string, never normalised; use `url_host` to read the host."""

# --- paths -----------------------------------------------------------------------------------

_NOTE_SEGMENT = rf"[^/\\{_CTRL}.][^/\\{_CTRL}]{{0,254}}"


NotePath = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{_NOTE_SEGMENT}(/{_NOTE_SEGMENT})*$", max_length=1024),
]
"""A note path relative to the sink's root (for example the vault), e.g. `Daily/x.md`.

POSIX separators only: no leading `/`, no `\\`, no control characters, no empty segment, and
no segment starting with `.` (so no `.`, `..`, `.obsidian/` or other hidden files). At most
255 characters per segment and 1024 in total."""

_ABS_DOT_SEGMENT = r"/\.\.?(/|$)"


def _check_abs_path(path: str) -> str:
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError(f"{path!r} is not a normalised path")
    return path


AbsolutePath = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^(/[^/\\{_CTRL}]{{1,255}})+$", max_length=1024),
    AfterValidator(_check_abs_path),
    JsonSchemaExtra({"not": {"pattern": _ABS_DOT_SEGMENT}}),
]
"""A normalised absolute POSIX path, e.g. `/vault/Daily`: no `.` or `..` segment, no empty
segment or trailing `/`, not `/` itself, no `\\` or control characters."""

# --- dates and times -------------------------------------------------------------------------

_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
DATE_PATTERN = rf"^{_DATE}$"
DATETIME_PATTERN = (
    rf"^{_DATE}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}(\.[0-9]{{1,6}})?(Z|[+-][0-9]{{2}}:[0-9]{{2}})$"
)


def _iso_date(value: Any) -> Any:
    if isinstance(value, str):
        if not re.fullmatch(DATE_PATTERN, value):
            raise ValueError("expected a date as YYYY-MM-DD")
        return value
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    raise ValueError("expected a date as YYYY-MM-DD")


def _iso_datetime(value: Any) -> Any:
    if isinstance(value, str):
        if not re.fullmatch(DATETIME_PATTERN, value):
            raise ValueError("expected an RFC 3339 date-time with a UTC offset")
        return value
    if isinstance(value, datetime.datetime):
        return value
    raise ValueError("expected an RFC 3339 date-time with a UTC offset")


IsoDate = Annotated[
    datetime.date, BeforeValidator(_iso_date), JsonSchemaExtra({"pattern": DATE_PATTERN})
]
"""A calendar date, `YYYY-MM-DD`."""

Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_iso_datetime),
    JsonSchemaExtra({"pattern": DATETIME_PATTERN}),
]
"""An RFC 3339 date-time with seconds and an explicit offset (`Z` or `+hh:mm`), e.g.
`2026-10-27T05:30:00Z`. Upper-case `T` and `Z` only; naive date-times are rejected."""
