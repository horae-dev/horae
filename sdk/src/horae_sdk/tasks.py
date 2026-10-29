# SPDX-License-Identifier: Apache-2.0
"""Obsidian Tasks syntax, and the escaping of untrusted task titles (threat model T02).

A task title is **data** (`horae.contracts.intents`): a sink writing Obsidian Tasks syntax must
not let it set a due date, a recurrence, a priority, a tag or a link. Tasks reads its fields
from the raw Markdown line (`- [ ] Pay the bill 📅 2026-10-22 ⏰ 19:00`), so a title holding
`📅 2020-01-01 🔁 every day` would otherwise schedule the task.

`escape_title(title)` makes a title inert:

- every Tasks signifier (`SIGNIFIERS`: due, scheduled, start, created, done, cancelled,
  recurrence, the ⏰ reminder, the five priorities, id, depends-on, on-completion, with their
  variant forms) becomes a numeric character reference (`&#x1F4C5;`): Obsidian still shows the
  emoji in reading view, but the line no longer holds it, so Tasks does not parse it. A
  variation selector (U+FE0F) right after a signifier is dropped with it;
- the ASCII punctuation that starts Markdown or Obsidian markup (`MARKDOWN`: emphasis, code,
  links and wikilinks, embeds, tags, highlights, comments, math, block ids, HTML, inline
  fields `[key:: value]` / `(key:: value)`) gets a backslash (CommonMark escapes), and the
  second colon of `::` too (a Dataview inline field);
- line breaks, tabs and other whitespace controls (and U+2028, U+2029) become spaces, other
  control and format characters (bidi overrides, zero-width characters) are dropped, runs of
  spaces are collapsed, and the result is stripped;
- the result is cut, on an escape boundary, to `max_length` characters (500: the `Title`
  contract), ending with `…` when cut.

**Idempotent.** An escape the function writes is kept as it is (`\\*`, `&#x1F4C5;`), so
escaping twice is escaping once: an intent builder escapes (`horae_sdk.intents.task_create`)
and a sink escapes again (`task_line`), and neither has to know whether the other did. The
price: a title that already holds a backslash before punctuation, or a signifier reference,
keeps it as an escape (it shows the character, never markup).

`unescape_title` gives back what a reader sees (the references and backslashes resolved), for
other targets (a notification) and for tests.
"""

import re
import unicodedata
from collections.abc import Mapping
from datetime import date

SIGNIFIERS: dict[str, str] = {
    "\U0001f4c5": "due",
    "\U0001f4c6": "due",
    "\U0001f5d3": "due",
    "\u23f3": "scheduled",
    "\u231b": "scheduled",
    "\U0001f6eb": "start",
    "\u2795": "created",
    "\u2705": "done",
    "\u274c": "cancelled",
    "\U0001f501": "recurrence",
    "\u23f0": "reminder",
    "\U0001f53a": "highest priority",
    "\u23eb": "high priority",
    "\U0001f53c": "medium priority",
    "\U0001f53d": "low priority",
    "\u23ec": "lowest priority",
    "\U0001f194": "id",
    "\u26d4": "depends on",
    "\U0001f3c1": "on completion",
}
"""Every Obsidian Tasks signifier (and the ⏰ reminder of PLAN.md §5), with what it means."""

DUE = "\U0001f4c5"
REMINDER = "\u23f0"
_VS16 = "\ufe0f"
MARKDOWN = frozenset("\\`*_[]()<>#|~=!$%^{}&")
"""ASCII punctuation escaped with a backslash."""
_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
"""What CommonMark lets a backslash escape (an existing pair is kept as it is)."""
_REFERENCE = re.compile(r"&#x([0-9A-F]{4,5});")
MAX_TITLE = 500
_ELLIPSIS = "…"


def _reference(char: str) -> str:
    return f"&#x{ord(char):X};"


def _pieces(title: str) -> list[str]:
    """The escaped title as a list of pieces (each one character or one escape)."""
    pieces: list[str] = []
    index = 0
    previous = ""
    while index < len(title):
        char = title[index]
        nxt = title[index + 1] if index + 1 < len(title) else ""
        reference = _REFERENCE.match(title, index) if char == "&" else None
        if char == "\\" and nxt in _PUNCTUATION:
            piece, index = char + nxt, index + 2
        elif reference is not None and chr(int(reference.group(1), 16)) in SIGNIFIERS:
            piece, index = reference.group(0), reference.end()
        elif char in SIGNIFIERS:
            piece, index = _reference(char), index + (2 if nxt == _VS16 else 1)
        elif char in MARKDOWN or (char == ":" and previous == ":"):
            piece, index = "\\" + char, index + 1
        else:
            category = unicodedata.category(char)
            index += 1
            if char.isspace() or category in {"Zl", "Zp"}:
                piece = " "
            elif category in {"Cc", "Cf", "Cs"}:
                continue
            else:
                piece = char
        previous = char
        if piece == " " and (not pieces or pieces[-1] == " "):
            continue
        pieces.append(piece)
    while pieces and pieces[-1] == " ":
        pieces.pop()
    return pieces


def escape_title(title: str, *, max_length: int = MAX_TITLE) -> str:
    """`title` made inert for an Obsidian Tasks line (see the module docstring).

    Raises `ValueError` when nothing printable is left."""
    pieces = _pieces(title)
    if not pieces:
        raise ValueError("the task title is empty once escaped")
    if sum(map(len, pieces)) <= max_length:
        return "".join(pieces)
    kept: list[str] = []
    size = len(_ELLIPSIS)
    for piece in pieces:
        if size + len(piece) > max_length:
            break
        kept.append(piece)
        size += len(piece)
    return "".join(kept).rstrip() + _ELLIPSIS


def unescape_title(escaped: str) -> str:
    """What a reader sees of an escaped title (references and backslash escapes resolved)."""
    text = _REFERENCE.sub(lambda match: chr(int(match.group(1), 16)), escaped)
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)


def task_line(
    title: str,
    *,
    due: str | date | None = None,
    time: str | None = None,
    done: bool = False,
) -> str:
    """One Obsidian Tasks line, `- [ ] <escaped title> 📅 <due> ⏰ <time>`, the title always
    escaped (`escape_title`, idempotent: an already escaped title is unchanged).

    `due` is `YYYY-MM-DD` (or a `date`), `time` `HH:MM` and requires `due` (the `task.create`
    contract); `ValueError` otherwise."""
    due_text = due.isoformat() if isinstance(due, date) else due
    if due_text is not None and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", due_text):
        raise ValueError("due must be YYYY-MM-DD")
    if time is not None and (
        due_text is None or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", time)
    ):
        raise ValueError("time must be HH:MM and requires due")
    parts = [f"- [{'x' if done else ' '}]", escape_title(title)]
    if due_text is not None:
        parts.append(f"{DUE} {due_text}")
    if time is not None:
        parts.append(f"{REMINDER} {time}")
    return " ".join(parts)


def task_line_for(intent: Mapping[str, object]) -> str:
    """`task_line` for a `task.create` intent (as the core sends it to a sink's `apply`)."""
    if intent.get("type") != "task.create":
        raise ValueError("not a task.create intent")
    title, due, time = intent.get("title"), intent.get("due"), intent.get("time")
    if not isinstance(title, str):
        raise ValueError("the intent has no title")
    if not (due is None or isinstance(due, str)) or not (time is None or isinstance(time, str)):
        raise ValueError("due and time must be strings")
    return task_line(title, due=due, time=time)


__all__ = [
    "DUE",
    "MARKDOWN",
    "MAX_TITLE",
    "REMINDER",
    "SIGNIFIERS",
    "escape_title",
    "task_line",
    "task_line_for",
    "unescape_title",
]
