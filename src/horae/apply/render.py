# SPDX-License-Identifier: Apache-2.0
"""A `ChangeSet` for a terminal (`render_text`) or for scripts (`to_json`).

**Text** is colourless unless `color=True` (ANSI: removed lines red, added green, hunk
headers cyan, file headers bold). Everything in it that comes from a note, an intent or a
sink is untrusted, so control characters (except tab) and bidirectional-text controls are
shown escaped (`\\x1b`, `\\r`, `\\u202e`): a note cannot move the cursor, change colours or
reorder what the terminal shows.

**JSON** (`schema_version: 1`) has a fixed key order and is ASCII-only (`ensure_ascii`):
`run`, `refused`, `files` (with the raw unified `diff`, carriage returns kept), `tasks`,
`notifications`, `http_calls` and `problems`, each item with its `intent` index and
`applied_by`.
"""

import json
import re
from typing import Any

from horae.apply.plan import ChangeSet, FileDiff

JSON_SCHEMA_VERSION = 1
"""Version of the `horae diff --json` report."""

_UNSAFE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")
_ESCAPES = {"\r": "\\r", "\n": "\\n"}
_RED, _GREEN, _CYAN, _BOLD, _RESET = "\x1b[31m", "\x1b[32m", "\x1b[36m", "\x1b[1m", "\x1b[0m"


def safe(text: str) -> str:
    """`text` with control and bidi characters escaped, for a terminal."""

    def escape(match: re.Match[str]) -> str:
        char = match.group()
        if char in _ESCAPES:
            return _ESCAPES[char]
        code = ord(char)
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

    return _UNSAFE.sub(escape, text)


def _paint(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _diff_lines(diff: str, color: bool) -> list[str]:
    lines = []
    for raw in diff.split("\n")[:-1]:
        line = safe(raw)
        if raw.startswith(("--- ", "+++ ")):
            lines.append(_paint(line, _BOLD, color))
        elif raw.startswith("@@"):
            lines.append(_paint(line, _CYAN, color))
        elif raw.startswith("+"):
            lines.append(_paint(line, _GREEN, color))
        elif raw.startswith("-"):
            lines.append(_paint(line, _RED, color))
        else:
            lines.append(line)
    return lines


def _file_lines(item: FileDiff, color: bool) -> list[str]:
    path = safe(item.path)
    head = f"{path}: {item.status} (intent {item.intent}, {safe(item.applied_by)})"
    lines = [_paint(head, _BOLD, color)]
    if item.overlaps:
        others = ", ".join(str(i) for i in item.overlaps)
        lines.append(f"  note: intents {others} also change it; diffed against the current note")
    size = f"{item.before_size or 0} -> {item.after_size} bytes"
    if item.binary:
        lines.append(f"Binary files a/{path} and b/{path} differ ({size})")
    elif item.too_large:
        lines.append(f"Too large to diff ({size}, sha256 {item.after_sha256})")
    elif item.diff is not None:
        lines.extend(_diff_lines(item.diff, color))
    return lines


def render_text(changes: ChangeSet, *, color: bool = False) -> str:
    """`changes` for a terminal, ending with a newline."""
    run = f"Run {safe(changes.run_id)} ({safe(changes.pipeline)}, {changes.run_status}"
    lines = [run + (", dry run)" if changes.dry_run else ")")]
    if changes.refused is not None:
        lines.append(f"Nothing to apply: {changes.refused}.")
        return "\n".join(lines) + "\n"
    if changes.empty and not changes.problems:
        lines.append("No changes.")
    for item in changes.files:
        lines.extend(["", *_file_lines(item, color)])
    if changes.tasks:
        lines.extend(["", _paint("Tasks:", _BOLD, color)])
        for task in changes.tasks:
            c = task.change
            state = safe(c.after) if c.before is None else f"{safe(c.before)} -> {safe(c.after)}"
            lines.append(f"  {c.action} {safe(c.fingerprint)}: {state} ({safe(task.applied_by)})")
    if changes.notifications:
        lines.extend(["", _paint("Notifications:", _BOLD, color)])
        for note in changes.notifications:
            n = note.notification
            text = safe(n.body) if n.title is None else f"{safe(n.title)}: {safe(n.body)}"
            link = "" if n.link is None else f" <{safe(n.link)}>"
            dup = " (already sent, skipped)" if note.duplicate else ""
            lines.append(f"  via {safe(note.applied_by)}: {text}{link}{dup}")
    if changes.http_calls:
        lines.extend(["", _paint("HTTP calls:", _BOLD, color)])
        for http in changes.http_calls:
            body = json.dumps(http.call.body, separators=(",", ":"), ensure_ascii=False)
            short = body if len(body) <= 200 else body[:200] + "…"
            lines.append(
                f"  {http.call.method} {safe(http.call.url)} via {safe(http.applied_by)}"
                f" ({len(body.encode())} bytes): {safe(short)}"
            )
    if changes.problems:
        lines.extend(["", _paint("Not applicable:", _BOLD, color)])
        for problem in changes.problems:
            via = "" if problem.applied_by is None else f" ({safe(problem.applied_by)})"
            lines.append(
                f"  intent {problem.intent}{via}: {safe(problem.kind)}: {safe(problem.message)}"
            )
    return "\n".join(lines) + "\n"


def to_dict(changes: ChangeSet) -> dict[str, Any]:
    """`changes` as plain JSON data, in the `to_json` key order."""
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "run": {
            "id": changes.run_id,
            "pipeline": changes.pipeline,
            "status": changes.run_status,
            "dry_run": changes.dry_run,
        },
        "refused": changes.refused,
        "files": [
            {
                "intent": f.intent,
                "applied_by": f.applied_by,
                "path": f.path,
                "status": f.status,
                "before": None
                if f.before_size is None
                else {"size": f.before_size, "sha256": f.before_sha256},
                "after": {"size": f.after_size, "sha256": f.after_sha256},
                "binary": f.binary,
                "too_large": f.too_large,
                "overlaps": list(f.overlaps),
                "diff": f.diff,
            }
            for f in changes.files
        ],
        "tasks": [
            {
                "intent": t.intent,
                "applied_by": t.applied_by,
                "fingerprint": t.change.fingerprint,
                "action": t.change.action,
                "before": t.change.before,
                "after": t.change.after,
            }
            for t in changes.tasks
        ],
        "notifications": [
            {
                "intent": n.intent,
                "applied_by": n.applied_by,
                "title": n.notification.title,
                "body": n.notification.body,
                "link": n.notification.link,
                "dedupe_key": n.dedupe_key,
                "duplicate": n.duplicate,
            }
            for n in changes.notifications
        ],
        "http_calls": [
            {
                "intent": h.intent,
                "applied_by": h.applied_by,
                "method": h.call.method,
                "url": h.call.url,
                "body": h.call.body,
            }
            for h in changes.http_calls
        ],
        "problems": [
            {"intent": p.intent, "applied_by": p.applied_by, "kind": p.kind, "message": p.message}
            for p in changes.problems
        ],
    }


def to_json(changes: ChangeSet) -> str:
    """`changes` as an indented JSON document (no trailing newline)."""
    return json.dumps(to_dict(changes), indent=2, ensure_ascii=True)
