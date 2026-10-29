# SPDX-License-Identifier: Apache-2.0
"""Builders of the closed set of intents (ADR 0002, `schemas/intents/`).

A plugin never writes: it proposes intents in its result, and the core validates them against
its manifest's `permissions.intents` (all or nothing per step), shows the dry-run diff,
journals and applies them. Each builder returns the intent as a JSON-ready `dict`, checked
here against the cheap rules of the contract (`IntentError`, a `ValueError`, otherwise), so a
mistake fails in the plugin's own tests rather than as a rejected step. The core stays the
judge: it checks everything again.

`task_create` **escapes the title** for Obsidian Tasks by default (`horae_sdk.tasks`,
threat model T02): a title from an untrusted source (a mail subject, an issue title) cannot
set a date, a recurrence, a priority, a tag or a link. The escaping is idempotent, so a
Tasks sink escaping again changes nothing. Pass `escape=False` only for a task that never
reaches Tasks syntax; the title is then still made one line (control characters refused).
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal

from horae_sdk.tasks import MAX_TITLE, escape_title

Intent = dict[str, Any]
INTENT_TYPES = (
    "note.create",
    "note.append_section",
    "note.replace_owned_block",
    "task.create",
    "task.update_status",
    "notify.push",
    "http.post",
)
"""The closed set (`horae.contracts.intents.IntentType`)."""

MAX_HEADING = 200
MAX_BODY = 4096
MAX_CONTENT = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FINGERPRINT = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SEGMENT = r"[^./\\\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]{0,254}"
_NOTE_PATH = re.compile(rf"{_SEGMENT}(/{_SEGMENT})*")
_BLOCK = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_LINK = re.compile(r"(https|obsidian)://[^\x00-\x20\x7f]+")


_ANY = re.compile(r".+", re.DOTALL)
_HTTPS = re.compile(r"https://[^\x00-\x20\x7f]+")


def _matches(pattern: re.Pattern[str], value: object, limit: int = 2048) -> bool:
    """`value` is a string of at most `limit` characters matching `pattern` (checked at run
    time too: a plugin's own code may not be type-checked)."""
    return isinstance(value, str) and len(value) <= limit and pattern.fullmatch(value) is not None


class IntentError(ValueError):
    """The builder's arguments do not make a valid intent (the message says which field)."""


def _line(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value or _CONTROL.search(value) or len(value) > limit:
        raise IntentError(f"{what} must be one line of 1 to {limit} characters")
    return value


def _date(value: object, what: str) -> str:
    text = value.isoformat() if isinstance(value, date) else value
    if not isinstance(text, str) or not _DATE.fullmatch(text):
        raise IntentError(f"{what} must be a date, YYYY-MM-DD")
    return text


def _path(path: object, intent: Intent) -> Intent:
    """`intent` with `path` (omitted: the core fills it from the `act` entry)."""
    if path is not None:
        if not _matches(_NOTE_PATH, path, 1024):
            raise IntentError("path must be a relative note path with no hidden segment")
        intent["path"] = path
    return intent


def _content(content: object) -> str:
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        raise IntentError(f"content must be text of at most {MAX_CONTENT} characters")
    return content


def fingerprint(*parts: str) -> str:
    """A stable task fingerprint from `parts` (e.g. `fingerprint("github", "pr", "42")` is
    `github:pr:42`): the parts joined with `:`, characters outside `A-Z a-z 0-9 . _ : -`
    replaced with `-`; longer than 128 characters, `<first part>:sha256:<digest prefix>`."""
    if not parts or not all(_matches(_ANY, part, MAX_CONTENT) for part in parts):
        raise IntentError("a fingerprint needs at least one non-empty part")
    joined = ":".join(re.sub(r"[^A-Za-z0-9._-]", "-", part) for part in parts)
    if len(joined) <= 128:
        return joined
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    head = re.sub(r"[^A-Za-z0-9._-]", "-", parts[0])[:48]
    return f"{head}:sha256:{digest[:64]}"


def note_create(content: str, *, path: str | None = None) -> Intent:
    """`note.create`: a note with `content` (Markdown, at most 1 MiB)."""
    return _path(path, {"type": "note.create", "content": _content(content)})


def note_append_section(heading: str, content: str, *, path: str | None = None) -> Intent:
    """`note.append_section`: a section under `heading` (one line, no leading `#`)."""
    intent: Intent = {
        "type": "note.append_section",
        "heading": _line(heading, "heading", MAX_HEADING),
        "content": _content(content),
    }
    return _path(path, intent)


def note_replace_owned_block(block: str, content: str, *, path: str | None = None) -> Intent:
    """`note.replace_owned_block`: the content of the block Horae owns, `block` its id."""
    if not _matches(_BLOCK, block):
        raise IntentError("block must match ^[a-z0-9][a-z0-9-]{0,63}$")
    intent: Intent = {"type": "note.replace_owned_block", "block": block}
    intent["content"] = _content(content)
    return _path(path, intent)


def task_create(
    title: str,
    *,
    fingerprint: str,
    due: str | date | None = None,
    time: str | None = None,
    escape: bool = True,
) -> Intent:
    """`task.create`. The title is escaped for Obsidian Tasks unless `escape=False` (see the
    module docstring) and cut to 500 characters; `time` (`HH:MM`) requires `due`."""
    if not isinstance(title, str):
        raise IntentError("title must be a string")
    if escape:
        try:
            title = escape_title(title, max_length=MAX_TITLE)
        except ValueError:
            raise IntentError("title is empty once escaped") from None
    intent: Intent = {"type": "task.create", "title": _line(title, "title", MAX_TITLE)}
    if not _matches(_FINGERPRINT, fingerprint):
        raise IntentError("fingerprint must be 1 to 128 of A-Z a-z 0-9 . _ : -")
    intent["fingerprint"] = fingerprint
    if due is not None:
        intent["due"] = _date(due, "due")
    if time is not None:
        if due is None:
            raise IntentError("time requires due")
        if not _matches(_CLOCK, time):
            raise IntentError("time must be HH:MM (24 h)")
        intent["time"] = time
    return intent


def task_update_status(
    task: str,
    status: Literal["todo", "done", "cancelled", "snoozed"],
    *,
    until: str | date | datetime | None = None,
) -> Intent:
    """`task.update_status` of the task with fingerprint `task`; `until` (a date or an RFC 3339
    date-time with an offset) is required for `snoozed` and only allowed there."""
    if not _matches(_FINGERPRINT, task):
        raise IntentError("task must be a fingerprint")
    if status not in ("todo", "done", "cancelled", "snoozed"):
        raise IntentError("status must be todo, done, cancelled or snoozed")
    intent: Intent = {"type": "task.update_status", "task": task, "status": status}
    if (status == "snoozed") != (until is not None):
        raise IntentError("until is required for snoozed and only allowed there")
    if isinstance(until, datetime):
        if until.tzinfo is None:
            raise IntentError("until must have a time zone")
        until = until.isoformat(timespec="seconds").replace("+00:00", "Z")
    elif isinstance(until, date):
        until = until.isoformat()
    if until is not None:
        if not (_matches(_DATE, until) or _matches(_TIMESTAMP, until)):
            raise IntentError("until must be YYYY-MM-DD or an RFC 3339 date-time with an offset")
        intent["until"] = until
    return intent


def notify_push(body: str, *, title: str | None = None, link: str | None = None) -> Intent:
    """`notify.push`: `body` (at most 4096 characters), an optional one-line `title` and an
    optional `https://` or `obsidian://` `link` (the core also checks it against the
    pipeline's trusted `link_prefix`)."""
    if not _matches(_ANY, body, MAX_BODY):
        raise IntentError(f"body must be 1 to {MAX_BODY} characters")
    intent: Intent = {"type": "notify.push", "body": body}
    if title is not None:
        intent["title"] = _line(title, "title", MAX_TITLE)
    if link is not None:
        if not _matches(_LINK, link):
            raise IntentError("link must be an https:// or obsidian:// URL without whitespace")
        intent["link"] = link
    return intent


def http_post(
    url: str, body: Mapping[str, Any] | list[Any] | str | int | float | bool | None
) -> Intent:
    """`http.post` of the JSON `body` to the `https` `url`, whose host must be in the plugin's
    `permissions.network` (the core checks the host and the body)."""
    if not _matches(_HTTPS, url):
        raise IntentError("url must be an https:// URL")
    return {
        "type": "http.post",
        "url": url,
        "body": dict(body) if isinstance(body, Mapping) else body,
    }


__all__ = [
    "INTENT_TYPES",
    "Intent",
    "IntentError",
    "fingerprint",
    "http_post",
    "note_append_section",
    "note_create",
    "note_replace_owned_block",
    "notify_push",
    "task_create",
    "task_update_status",
]
