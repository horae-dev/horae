# SPDX-License-Identifier: Apache-2.0
"""How a note intent changes a note's bytes: the core's note conventions (pure functions).

The core owns these conventions (schemas decision 32, D1/D2): the dry-run diff computes every
note change with `note_after`, and a sink's `apply` (#22) must produce the same bytes.

- **Ownership.** A note Horae owns is one whose file name ends in `.assistant.md`, decided on
  the path the sink resolved without following symlinks (a sink refuses symlinks and hard
  links, `horae.apply.sink`). Horae never creates content in a note it does not own:
  `note.create` and `note.append_section` need an owned note, and `note.replace_owned_block`
  on any other note only replaces a block that is already there (`not_owned` for a missing
  note or block).
- `note.create` refuses an existing note (`note_exists`).
- `note.append_section` needs the note (`note_missing`) and appends, after a blank line,
  `## <heading>`, a blank line and the content.
- `note.replace_owned_block` replaces the lines from `<!-- horae:begin <block> -->` to
  `<!-- horae:end <block> -->` (both included). In an owned note a missing note or block is
  created (the block at the end of the note).
- **Markers.** One recogniser (`MARKER`, `horae:begin` / `horae:end`, any case, optionally
  followed by a block name) serves both directions. In a note, every line on which it names
  the block counts: there must be exactly one begin and one end line, in that order, each
  exactly the canonical marker (surrounding spaces and a final `\\r` aside), else
  `block_ambiguous`. In untrusted text (content, heading) it must not appear at all
  (`content_has_marker`), so a proposer can neither move a block's boundaries nor plant a
  marker for later.
- **Line breaks.** Untrusted text may only break lines with `\\n` or `\\r\\n`: a lone `\\r`,
  NEL (U+0085), LINE SEPARATOR (U+2028) or PARAGRAPH SEPARATOR (U+2029) is refused
  (`content_line_break`), since editors disagree on whether they end a line. Inserted lines
  use the note's line ending (that of its first line; `\\n` for a new or one-line note), and
  the rest of the note is kept byte for byte.

Content always ends with a line ending. Notes are UTF-8 (`note_not_text` otherwise).
"""

import re
from pathlib import PurePosixPath

from horae.apply.sink import SinkError
from horae.contracts.intents import NoteAppendSection, NoteCreate, NoteReplaceOwnedBlock

NoteIntent = NoteCreate | NoteAppendSection | NoteReplaceOwnedBlock

OWNED_SUFFIX = ".assistant.md"
MARKER = re.compile(r"horae:(begin|end)(?:\s+([a-z0-9-]+))?", re.IGNORECASE)
"""The block-marker recogniser, for notes and for untrusted text alike."""
_BAD_BREAK = re.compile("\r(?!\n)|[\u0085\u2028\u2029]")


def begin_marker(block: str) -> str:
    return f"<!-- horae:begin {block} -->"


def end_marker(block: str) -> str:
    return f"<!-- horae:end {block} -->"


def is_owned(path: str) -> bool:
    """Whether Horae owns the note at `path` (its file name ends in `.assistant.md`)."""
    return PurePosixPath(path).name.endswith(OWNED_SUFFIX)


def check_text(text: str, what: str = "content") -> None:
    """Refuse untrusted `text` holding a block marker or an ambiguous line break."""
    if MARKER.search(text):
        raise SinkError("content_has_marker", f"the {what} holds a horae block marker")
    if _BAD_BREAK.search(text):
        raise SinkError(
            "content_line_break",
            f"the {what} holds a lone carriage return, NEL, or a Unicode line or paragraph"
            " separator",
        )


def _eol(text: str) -> str:
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def _lines_of(content: str, eol: str) -> str:
    """`content` (already checked) ending with a line break, with `eol` line endings."""
    text = content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.replace("\n", eol)


def _terminated(text: str, eol: str) -> str:
    return text if text.endswith("\n") else text + eol


def _decode(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SinkError("note_not_text", f"{path} is not UTF-8 text") from None


def _find_block(text: str, intent: NoteReplaceOwnedBlock) -> tuple[int, int] | None:
    """The span of the block's lines in `text` (end included), `None` if it has no marker."""
    begin, end = begin_marker(intent.block), end_marker(intent.block)
    begins: list[tuple[int, int]] = []
    ends: list[tuple[int, int]] = []
    offset = 0
    for line in _split(text):
        stop = offset + len(line)
        names = [m for m in MARKER.finditer(line) if (m.group(2) or "").lower() == intent.block]
        if names:
            bare = line.rstrip("\n").rstrip("\r").strip(" \t")
            if len(names) != 1 or bare not in (begin, end):
                raise SinkError(
                    "block_ambiguous",
                    f"{intent.path}: a marker of block {intent.block!r} is malformed",
                )
            (begins if bare == begin else ends).append((offset, stop))
        offset = stop
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0][0] < begins[0][1]:
        raise SinkError(
            "block_ambiguous", f"{intent.path}: block {intent.block!r} is not one marker pair"
        )
    return begins[0][0], ends[0][1]


def _split(text: str) -> list[str]:
    """`text` split after each `\\n` only (a `\\r` stays in its line)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _block(intent: NoteReplaceOwnedBlock, eol: str) -> str:
    content = _lines_of(intent.content, eol)
    return f"{begin_marker(intent.block)}{eol}{content}{end_marker(intent.block)}{eol}"


def note_after(intent: NoteIntent, before: bytes | None) -> bytes:
    """The note's bytes once `intent` is applied to `before` (`None`: no such note).

    Raises `SinkError` when the intent cannot be applied to that note."""
    check_text(intent.content)
    owned = is_owned(intent.path)
    not_owned = SinkError("not_owned", f"{intent.path} is not a *{OWNED_SUFFIX} note")
    if isinstance(intent, NoteCreate):
        if not owned:
            raise not_owned
        if before is not None:
            raise SinkError("note_exists", f"{intent.path} already exists")
        return _lines_of(intent.content, "\n").encode()
    text = None if before is None else _decode(before, intent.path)
    if isinstance(intent, NoteAppendSection):
        check_text(intent.heading, "heading")
        if not owned:
            raise not_owned
        if text is None:
            raise SinkError("note_missing", f"{intent.path} does not exist")
        eol = _eol(text)
        section = f"## {intent.heading}{eol}{eol}{_lines_of(intent.content, eol)}"
        return ((_terminated(text, eol) + eol if text else "") + section).encode()
    if text is None:
        if not owned:
            raise SinkError("not_owned", f"{intent.path} does not exist and Horae does not own it")
        return _block(intent, "\n").encode()
    eol = _eol(text)
    span = _find_block(text, intent)
    if span is None:
        if not owned:
            raise SinkError(
                "not_owned",
                f"{intent.path} has no block {intent.block!r} and Horae does not own it",
            )
        return ((_terminated(text, eol) + eol if text else "") + _block(intent, eol)).encode()
    start, stop = span
    return (text[:start] + _block(intent, eol) + text[stop:]).encode()
