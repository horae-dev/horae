# SPDX-License-Identifier: Apache-2.0
"""Obsidian Tasks syntax, and the escaping of untrusted task titles (threat model T02).

A task title is **data** (`horae.contracts.intents`): a sink writing Obsidian Tasks syntax must
not let it set a due date, a recurrence, a priority, a tag or a link. Tasks reads its fields
from the raw Markdown line (`- [ ] Pay the bill 📅 2026-10-22 ⏰ 19:00`), so a title holding
`📅 2020-01-01 🔁 every day` would otherwise schedule the task. Schemas decision 50 records the
rules below and why.

`escape_title(title)` makes a title inert, and is **total** (any string gives a title):

- every Tasks signifier (`SIGNIFIERS`: due, scheduled, start, created, done, cancelled,
  recurrence, the ⏰ reminder, the five priorities, id, depends-on, on-completion, with their
  variant forms) becomes a numeric character reference (`&#x1F4C5;`): Obsidian still shows the
  emoji in reading view, but the line no longer holds it, so Tasks does not parse it. A
  variation selector (U+FE0F) right after a signifier is dropped with it;
- the ASCII punctuation that starts Markdown or Obsidian markup (`MARKDOWN`: emphasis, code,
  links and wikilinks, embeds, tags, highlights, comments, math, block ids, HTML, inline
  fields `[key:: value]` / `(key:: value)`) gets a backslash (CommonMark escapes);
- **pairs**: a `:` right after a `:` gets a backslash too (no Dataview `::` is left in the
  line, also after an escape the title already held: `\\::` gives `\\:\\:`). The second `%` of
  `%%` (an Obsidian comment) and the second `=` of `==` (a highlight) become character
  references (`&#x25;`, `&#x3D;`): those two are Obsidian extensions, not CommonMark, and
  nothing documents that Obsidian honours a backslash before them, while a reference leaves no
  `%%` or `==` in the line even for a parser that drops backslashes first;
- **autolinks**: the `:` ending a URL scheme (`https:`, `obsidian:`, `javascript:`… a run of
  `A-Z a-z 0-9 + . -` holding a letter, followed by a non-space: `10:30` and `Re: x` are left
  alone) and the `.` of a `www.` prefix (not preceded by a letter or digit) get a backslash,
  so a bare URL is not turned into a link;
- line breaks, tabs and other whitespace controls (and U+2028, U+2029) become spaces, other
  control and format characters (bidi overrides, zero-width characters, ZWJ) are dropped, runs
  of spaces are collapsed, and the result is stripped. Nothing printable left: the title is
  `UNTITLED` (`(untitled)`, escaped);
- the result is cut, **after** escaping, on an escape boundary, to `max_length` characters
  (500: the `Title` contract), ending with `…` when cut. The cut never falls before a combining
  mark, a variation selector or an emoji modifier (it backs off to the character they belong
  to); it is not a full grapheme-cluster segmentation (a regional-indicator flag may be split).

**Idempotent.** An escape the function writes is kept as it is (`\\*`, `&#x1F4C5;`, `&#x25;`),
so escaping twice is escaping once: an intent builder escapes (`horae_sdk.intents.task_create`)
and a sink escapes again (`task_line`), and neither has to know whether the other did. The
price: a title that already holds a backslash before punctuation (`\\#`), or a reference in
the form this module writes (uppercase hex, of a signifier, `%` or `=`), keeps it: it shows as
that character, never as markup. Any other reference (`&amp;`, lowercase `&#x1f4c5;`) has its
`&` escaped and shows literally.

`plain_text` gives a display string (references and backslash escapes resolved) for
plain-text targets only (a push notification, a log): it is **not** an exact inverse of
`escape_title` (line breaks, controls and cut text are gone), and its result must never be
fed to a Markdown or Tasks target.
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
_PAIRED = frozenset("%=")
"""Doubled, an Obsidian extension (`%%` comment, `==` highlight): the second is a reference."""
_REFERABLE = frozenset(SIGNIFIERS) | _PAIRED
_REFERENCE = re.compile(r"&#x([0-9A-F]{2,5});")
_SCHEME = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
"""The characters of a URL scheme (RFC 3986); a scheme starts with a letter."""
_ATTACHED = frozenset({"Mn", "Mc", "Me", "Sk"})
"""Categories never separated from the character before them by a cut (combining marks,
variation selectors, emoji modifiers)."""
MAX_TITLE = 500
UNTITLED = "\\(untitled\\)"
"""The title of a task whose title has nothing printable (`(untitled)`, escaped)."""
_ELLIPSIS = "…"


def _reference(char: str) -> str:
    return f"&#x{ord(char):X};"


def _pieces(title: str) -> list[str]:
    """The escaped title as a list of pieces (each one character or one escape).

    Every rule that looks back looks at the last piece **written** (not the last character
    read), so an escape the title already held, or a character dropped in between, cannot
    hide a pair (`\\::`, `:<ZWSP>:`, `\\%%`)."""
    pieces: list[str] = []
    run = ""
    """The last 4 URL-scheme characters written since the last other piece (`www`)."""
    lettered = False
    """Whether that run of URL-scheme characters holds an ASCII letter (`https`, `-x1`)."""
    index = 0
    while index < len(title):
        char = title[index]
        nxt = title[index + 1] if index + 1 < len(title) else ""
        last = pieces[-1][-1] if pieces else ""
        reference = _REFERENCE.match(title, index) if char == "&" else None
        if char == "\\" and nxt in _PAIRED and last == nxt:  # `%\%`, `\%\%`: no pair left
            piece, index = _reference(nxt), index + 2
        elif char == "\\" and nxt in _PUNCTUATION:
            piece, index = char + nxt, index + 2
        elif reference is not None and chr(int(reference.group(1), 16)) in _REFERABLE:
            piece, index = reference.group(0), reference.end()
        elif char in SIGNIFIERS:
            piece, index = _reference(char), index + (2 if nxt == _VS16 else 1)
        elif char in _PAIRED and last == char:
            piece, index = _reference(char), index + 1
        elif char == ":" and (last == ":" or (lettered and nxt != "" and not nxt.isspace())):
            piece, index = "\\:", index + 1
        elif char == "." and run[-3:].lower() == "www" and not run[:-3].isalnum():
            piece, index = "\\.", index + 1
        elif char in MARKDOWN:
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
        if piece == " " and (not pieces or pieces[-1] == " "):
            continue
        if piece in _SCHEME:
            run = (run + piece)[-4:]
            lettered = lettered or piece.isalpha()
        else:
            run, lettered = "", False
        pieces.append(piece)
    while pieces and pieces[-1] == " ":
        pieces.pop()
    return pieces


def _attached(piece: str) -> bool:
    return len(piece) == 1 and unicodedata.category(piece) in _ATTACHED


def escape_title(title: str, *, max_length: int = MAX_TITLE) -> str:
    """`title` made inert for an Obsidian Tasks line (see the module docstring): total, a
    title with nothing printable is `UNTITLED`."""
    pieces = _pieces(title) or [UNTITLED]
    if sum(map(len, pieces)) <= max_length:
        return "".join(pieces)
    kept: list[str] = []
    size = len(_ELLIPSIS)
    for piece in pieces:
        if size + len(piece) > max_length:
            break
        kept.append(piece)
        size += len(piece)
    if len(kept) < len(pieces) and _attached(pieces[len(kept)]):
        # never cut a mark from its base: drop the base (and the marks already kept) too
        while kept and _attached(kept[-1]):
            kept.pop()
        if kept:
            kept.pop()
    return "".join(kept).rstrip() + _ELLIPSIS


_ESCAPE_OR_REFERENCE = re.compile(r"\\([!-/:-@\[-`{-~])|&#x([0-9A-F]{2,5});")


def plain_text(escaped: str) -> str:
    """A display string for an escaped title: backslash escapes and the references
    `escape_title` writes resolved, in one left-to-right pass.

    For **plain-text display only** (a push notification, a log line): never feed the result
    to a Markdown or Tasks target (it is the unescaped, hostile text again). It is **not** an
    exact inverse of `escape_title`: line breaks, controls and format characters, runs of
    spaces and a cut tail are gone for good."""

    def resolve(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        code = int(match.group(2), 16)
        return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else match.group(0)

    return _ESCAPE_OR_REFERENCE.sub(resolve, escaped)


def _real_date(text: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def task_line(
    title: str,
    *,
    due: str | date | None = None,
    time: str | None = None,
    done: bool = False,
) -> str:
    """One Obsidian Tasks line, `- [ ] <escaped title> 📅 <due> ⏰ <time>`, the title always
    escaped (`escape_title`, idempotent: an already escaped title is unchanged).

    `due` is a real date, `YYYY-MM-DD` (or a `date`), `time` `HH:MM` and requires `due` (the
    `task.create` contract); `ValueError` otherwise."""
    due_text = due.isoformat() if isinstance(due, date) else due
    if due_text is not None and not _real_date(due_text):
        raise ValueError("due must be a real date, YYYY-MM-DD")
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
    "UNTITLED",
    "escape_title",
    "plain_text",
    "task_line",
    "task_line_for",
]
