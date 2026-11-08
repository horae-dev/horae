# SPDX-License-Identifier: Apache-2.0
"""Obsidian Tasks syntax, and the escaping of untrusted task titles (threat model T02).

A task title is **data** (`horae.contracts.intents`): a sink writing Obsidian Tasks syntax must
not let it set a due date, a recurrence, a priority, a tag or a link. Tasks reads its fields
from the raw Markdown line (`- [ ] Pay the bill ⏰ 19:00 📅 2026-10-22`), so a title holding
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

import hashlib
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


_LINK_FORBIDDEN = (
    r"\x00-\x20\x7f-\xa0\xad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u1680"
    r"\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u2066-\u206f\u3000\ufeff\ufff9-\ufffb"
)
"""The Basic Multilingual Plane characters of the categories `Cc`, `Cf`, `Zs`, `Zl`, `Zp`,
as escapes (a copy of the core's `TASK_LINK_FORBIDDEN`)."""
_TASK_LINK = re.compile(rf"https://[^{_LINK_FORBIDDEN}/?#@\\%\[\]]+([/?#][^{_LINK_FORBIDDEN}\\]*)?")
_LINK_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Cs"})
_IP_HEX = re.compile(r"0x[0-9a-f]*")
MAX_LINK = 2048
MAX_DESTINATION = 4096
"""The longest `link_destination` that `task_line` writes: a longer one (a link of mostly
non-ASCII characters, each up to 12 characters once encoded) is left out of the line, so a
task line stays well under a reader's size cap (plugin-obsidian analyses at most 16 KiB)."""
LINK_LABEL = "link"
"""The fixed text of a task's rendered link: never taken from the title or the URL."""
_LINK_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#@!&'*+,;=%"
)
_HEX = frozenset("0123456789ABCDEFabcdef")
_ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")


def _ip_host(host: str) -> bool:
    """`host` ends in a number (WHATWG URL): a browser would read it as an IP address."""
    name = unicodedata.normalize("NFKC", host).lower().replace("\u3002", ".")
    labels = name.split(".")
    if len(labels) > 1 and labels[-1] == "":
        labels.pop()
    last = labels[-1]
    return (last.isascii() and last.isdigit()) or _IP_HEX.fullmatch(last) is not None


def is_task_link(link: object) -> bool:
    """`link` is a valid `task.create` link (the core's `TaskLink`): an absolute `https://`
    URL of at most `MAX_LINK` characters; a non-empty host that is a name, not an IP literal
    (`127.0.0.1`, `[::1]`); no user info (`@`), `%` or brackets in the authority; no
    backslash, and no control, format (bidi, zero-width), space or separator character or
    lone surrogate anywhere."""
    if not (
        isinstance(link, str) and len(link) <= MAX_LINK and _TASK_LINK.fullmatch(link) is not None
    ):
        return False
    if any(unicodedata.category(char) in _LINK_CATEGORIES for char in link):
        return False
    host = re.split(r"[/?#]", link[len("https://") :], maxsplit=1)[0].split(":", 1)[0]
    return bool(host) and not _ip_host(host)


def link_destination(link: str) -> str:
    """`link` (a valid task link, `ValueError` otherwise) as an inert Markdown link
    destination, the same URL for a browser:

    - every character outside the URL characters that are plain in a Markdown destination
      (`A-Z a-z 0-9 - . _ ~ : / ? # @ ! & ' * + , ; = %`) is percent-encoded as UTF-8: the
      parentheses, brackets and angle brackets that would end or open a link, a backtick (code
      span), `$` (math), `"`, `{`, `}`, `|`, `^` and every non-ASCII character (so no Tasks
      signifier, zero-width or bidi character reaches the line);
    - a `%` not followed by two hex digits becomes `%25`, so no `%%` (an Obsidian comment);
    - the `&` of an HTML entity (`&lt;`, `&#x2E;`) becomes `%26`: Markdown decodes entities in
      a destination, which could otherwise change the path after routing checked it
      (`&#x2E;&#x2E;` as `..`);
    - the second `:` of `::` and the second `=` of `==` become `%3A` / `%3D`, so no Dataview
      inline field or highlight pair is left (as in `escape_title`).

    Idempotent: an encoded destination is unchanged. It percent-decodes to the same string as
    `link` does (`unquote(link_destination(link)) == unquote(link)`)."""
    if not is_task_link(link):
        raise ValueError(
            "link must be an https:// URL with a host name (no IP), without user info, "
            "whitespace, control or format character or backslash"
        )
    pieces: list[str] = []
    for index, char in enumerate(link):
        last = pieces[-1][-1] if pieces else ""
        following = link[index + 1 : index + 3]
        if char == "%" and not (len(following) == 2 and set(following) <= _HEX):
            piece = "%25"
        elif char == "&" and _ENTITY.match(link, index):
            piece = "%26"
        elif char in ":=" and last == char:
            piece = "%3A" if char == ":" else "%3D"
        elif char in _LINK_SAFE:
            piece = char
        else:
            piece = "".join(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        pieces.append(piece)
    return "".join(pieces)


BLOCK_ID_PREFIX = "h-"
"""The prefix of the block id `task_block_id` gives (Horae's, so no user block id collides)."""
BLOCK_ID_HEX = 12
"""How many hex digits of the fingerprint's SHA-256 the block id keeps (48 bits)."""
_FINGERPRINT = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def task_block_id(fingerprint: str) -> str:
    """The Obsidian block id that links a Tasks line Horae wrote back to its task:
    `h-` and the first 12 hex digits of the SHA-256 of `fingerprint` (UTF-8), e.g.
    `h-3f2a9c01b7de`. The line ends with it as `^h-3f2a9c01b7de`: Obsidian hides it in reading
    view, Tasks ignores it, and it survives edits to the text. It is a one-way digest: a
    reader keeps its own index from block id to fingerprint. `fingerprint` must be a
    `task.create` fingerprint (`^[A-Za-z0-9._:-]{1,128}$`); `ValueError` otherwise."""
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):  # type: ignore[redundant-expr]
        raise ValueError("fingerprint must match ^[A-Za-z0-9._:-]{1,128}$")
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return BLOCK_ID_PREFIX + digest[:BLOCK_ID_HEX]


def task_line(
    title: str,
    *,
    due: str | date | None = None,
    time: str | None = None,
    link: str | None = None,
    done: bool = False,
    fingerprint: str | None = None,
) -> str:
    """One Obsidian Tasks line, `- [ ] <escaped title> [link](<url>) ⏰ <time> 📅 <due>
    ^h-<hash>`, the title always escaped (`escape_title`, idempotent: an already escaped title
    is unchanged).

    `due` is a real date, `YYYY-MM-DD` (or a `date`), `time` `HH:MM` and requires `due`, and
    `link` a valid task link (`is_task_link`, the `task.create` contract); `ValueError`
    otherwise. The link is written right after the title as a Markdown link with the fixed
    text `LINK_LABEL` and the destination `link_destination(link)`: the title cannot open or
    close it (its brackets and parentheses are escaped), and Tasks reads it as part of the
    description, never as a field (schemas decision 52). A destination longer than
    `MAX_DESTINATION` characters is left out (the line is written without the link).

    The `⏰` reminder (Horae's, not a Tasks field) comes **before** the Tasks date fields:
    Tasks reads fields from the end of the line and stops at a signifier it does not know, so
    `⏰` after `📅` would hide the due date from Tasks queries. With `fingerprint`, the line
    ends with the block id `^<task_block_id(fingerprint)>` (Obsidian requires a block id last
    on its line)."""
    due_text = due.isoformat() if isinstance(due, date) else due
    if due_text is not None and not _real_date(due_text):
        raise ValueError("due must be a real date, YYYY-MM-DD")
    if time is not None and (
        due_text is None or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", time)
    ):
        raise ValueError("time must be HH:MM and requires due")
    block = None if fingerprint is None else task_block_id(fingerprint)
    destination = None if link is None else link_destination(link)
    parts = [f"- [{'x' if done else ' '}]", escape_title(title)]
    if destination is not None and len(destination) <= MAX_DESTINATION:
        parts.append(f"[{LINK_LABEL}]({destination})")
    if time is not None:
        parts.append(f"{REMINDER} {time}")
    if due_text is not None:
        parts.append(f"{DUE} {due_text}")
    if block is not None:
        parts.append(f"^{block}")
    return " ".join(parts)


def task_line_for(intent: Mapping[str, object], *, block_id: bool = False) -> str:
    """`task_line` for a `task.create` intent (as the core sends it to a sink's `apply`); with
    `block_id`, the line ends with the block id of the intent's fingerprint
    (`task_block_id`)."""
    if intent.get("type") != "task.create":
        raise ValueError("not a task.create intent")
    title, due, time = intent.get("title"), intent.get("due"), intent.get("time")
    link, key = intent.get("link"), intent.get("fingerprint")
    if not isinstance(title, str):
        raise ValueError("the intent has no title")
    if not (
        (due is None or isinstance(due, str))
        and (time is None or isinstance(time, str))
        and (link is None or isinstance(link, str))
    ):
        raise ValueError("due, time and link must be strings")
    if block_id and not isinstance(key, str):
        raise ValueError("the intent has no fingerprint")
    fingerprint = key if block_id and isinstance(key, str) else None
    return task_line(title, due=due, time=time, link=link, fingerprint=fingerprint)


__all__ = [
    "BLOCK_ID_HEX",
    "BLOCK_ID_PREFIX",
    "DUE",
    "LINK_LABEL",
    "MARKDOWN",
    "MAX_DESTINATION",
    "MAX_LINK",
    "MAX_TITLE",
    "REMINDER",
    "SIGNIFIERS",
    "UNTITLED",
    "escape_title",
    "is_task_link",
    "link_destination",
    "plain_text",
    "task_block_id",
    "task_line",
    "task_line_for",
]
