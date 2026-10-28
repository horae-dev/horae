# SPDX-License-Identifier: Apache-2.0
"""How a note intent changes a note's bytes, for the built-in previews (pure functions).

This is the **local convention** of `horae.apply.local` and `horae.apply.testing`, until the
Obsidian plugin (PLAN.md §5) owns it:

- Horae only creates, or appends to, notes it owns: `*.assistant.md` (`not_owned`).
- `note.create` refuses an existing note (`note_exists`).
- `note.append_section` needs the note (`note_missing`) and appends, after a blank line,
  `## <heading>`, a blank line and the content.
- `note.replace_owned_block` replaces what is between `<!-- horae:begin <block> -->` and
  `<!-- horae:end <block> -->` lines (one pair, `block_ambiguous` otherwise); a missing note
  or block is created (at the end of the note). Content holding a marker line is refused
  (`content_has_marker`): untrusted text cannot move the block's boundaries.

Content always ends with a newline. Notes are UTF-8 (`note_not_text` otherwise); the line
endings of an existing note are kept as they are.
"""

import re

from horae.apply.sink import PreviewError
from horae.contracts.intents import NoteAppendSection, NoteCreate, NoteReplaceOwnedBlock

NoteIntent = NoteCreate | NoteAppendSection | NoteReplaceOwnedBlock

OWNED_SUFFIX = ".assistant.md"
_MARKER = re.compile(r"^<!-- horae:(?:begin|end) [a-z0-9-]+ -->\r?$", re.MULTILINE)


def begin_marker(block: str) -> str:
    return f"<!-- horae:begin {block} -->"


def end_marker(block: str) -> str:
    return f"<!-- horae:end {block} -->"


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _decode(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PreviewError("note_not_text", f"{path} is not UTF-8 text") from None


def _block(block: str, content: str) -> str:
    if _MARKER.search(content):
        raise PreviewError("content_has_marker", "the content holds a horae block marker")
    return f"{begin_marker(block)}\n{_with_newline(content)}{end_marker(block)}\n"


def _replace_block(text: str, intent: NoteReplaceOwnedBlock) -> str:
    begin = re.compile(rf"^{re.escape(begin_marker(intent.block))}\r?\n", re.MULTILINE)
    end = re.compile(rf"^{re.escape(end_marker(intent.block))}\r?(\n|$)", re.MULTILINE)
    starts, ends = list(begin.finditer(text)), list(end.finditer(text))
    new = _block(intent.block, intent.content)
    if not starts and not ends:
        if not text:
            return new
        return _with_newline(text) + "\n" + new
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() < starts[0].end():
        raise PreviewError(
            "block_ambiguous", f"{intent.path}: block {intent.block!r} is not one marker pair"
        )
    return text[: starts[0].start()] + new + text[ends[0].end() :]


def note_after(intent: NoteIntent, before: bytes | None) -> bytes:
    """The note's bytes once `intent` is applied to `before` (`None`: no such note)."""
    if not isinstance(intent, NoteReplaceOwnedBlock) and not intent.path.endswith(OWNED_SUFFIX):
        raise PreviewError("not_owned", f"{intent.path} is not a *{OWNED_SUFFIX} note")
    if isinstance(intent, NoteCreate):
        if before is not None:
            raise PreviewError("note_exists", f"{intent.path} already exists")
        return _with_newline(intent.content).encode()
    if isinstance(intent, NoteAppendSection):
        if before is None:
            raise PreviewError("note_missing", f"{intent.path} does not exist")
        text = _decode(before, intent.path)
        section = f"## {intent.heading}\n\n{_with_newline(intent.content)}"
        return ((_with_newline(text) + "\n" if text else "") + section).encode()
    text = "" if before is None else _decode(before, intent.path)
    return _replace_block(text, intent).encode()
