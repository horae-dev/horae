# SPDX-License-Identifier: Apache-2.0
"""Plugin-declared personal fields, followed from a tool's output to the `llm` step (#162,
schemas decision 68).

A plugin declares, per source or transform, the view paths of its output that hold personal
data (`provides.personal_fields`, recorded in `horae.lock`). This module holds the pure
functions the engine and `horae eval` share:

- `under`: a tool's path seen from the step input (`[].data.<path>` after a `collect`);
- `values`: the personal values a tool's output holds at its declared paths (every string
  leaf and object key below them, and every whole number of at least `MIN_NUMBER_DIGITS`
  digits), and the paths whose shape the output contradicts;
- `merged_options`: a skill's input view options with the personal paths added as
  `{class: person}`, and the scan-only ones (`free_text:`, #165) as `{class: free_text}`, so
  the core applies them whatever the skill declares.
"""

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from horae.contracts.plugin import FREE_TEXT_FIELD, reduce_personal, split_personal
from horae.contracts.skill import view_steps

COLLECT_ROOT = "[].data"
"""Where a source's output sits in a `collect` step's output (`[{source, params, data}]`)."""

MIN_NUMBER_DIGITS = 6
"""Shortest whole number under a personal path collected as a personal value (#165): a phone
number, an identifier or an account number (6 digits and more) is then also looked for in
text and in other numbers. Shorter ones are not: a year (`2026`), a postcode-sized count, a
quantity or a day would match all over the input and replace ordinary numbers. A shorter
number is still pseudonymized where it sits (the path is classed `person`)."""


def under(root: str, path: str) -> str:
    """`path` (relative to a tool's output) seen from `root`: `under("[].data", "a[].b")` is
    `[].data.a[].b`, `under("[].data", "[].b")` is `[].data[].b`; an empty root is `path`.
    A scan-only path keeps its `free_text:` prefix."""
    if path.startswith(FREE_TEXT_FIELD):
        return FREE_TEXT_FIELD + under(root, path.removeprefix(FREE_TEXT_FIELD))
    if not root:
        return path
    return f"{root}{path}" if path.startswith("[]") else f"{root}.{path}"


def values(data: Any, paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """`(values, mismatched)`: every string leaf and object key at or below each personal
    path of `data` (NFC, in document order, without duplicates or empty strings), each whole
    number of at least `MIN_NUMBER_DIGITS` digits there (its decimal text), and the paths at
    which `data` holds a value of the wrong shape (an object where the path reads an array,
    a scalar where it reads either): such a field cannot be located, so a step that would
    send it to a cloud model fails closed. An absent key, or `null`, is not a mismatch. A
    scan-only path (`free_text:`) is checked for its shape, but its content is not a value."""
    found: dict[str, None] = {}
    mismatched: list[str] = []
    for path in paths:
        text = path.startswith(FREE_TEXT_FIELD)
        if _walk(data, view_steps(path.removeprefix(FREE_TEXT_FIELD)), found, collect=not text):
            mismatched.append(path)
    return list(found), mismatched


def _walk(value: Any, steps: tuple[str, ...], found: dict[str, None], *, collect: bool) -> bool:
    """Collect the leaves under `steps` (with `collect`); whether a value had the wrong
    shape."""
    level: list[Any] = [value]
    for step in steps:
        following: list[Any] = []
        for item in level:
            if item is None:
                continue
            if step == "[]":
                if not isinstance(item, list):
                    return True
                following.extend(item)
            else:
                if not isinstance(item, dict):
                    return True
                if step in item:
                    following.append(item[step])
        level = following
    stack = list(reversed(level)) if collect else []
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            _add(found, item)
        elif isinstance(item, int) and not isinstance(item, bool):
            digits = str(abs(item))
            if len(digits) >= MIN_NUMBER_DIGITS:
                found.setdefault(digits, None)
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            for key in item:
                _add(found, key)
            stack.extend(reversed(list(item.values())))
    return False


def _add(found: dict[str, None], text: str) -> None:
    text = unicodedata.normalize("NFC", text)
    if text.strip():
        found.setdefault(text, None)


def merged_options(
    options: Mapping[str, Any], personal: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """The skill's input view options with each personal path classed `person` (decision 68
    C): a path the skill already covers with a token class (`person`, `email`, `phone`,
    `place`) or `amount`, at it or above it, is left to the skill's class; otherwise the
    skill's classes below the path are dropped (the path covers them) and a `free_text` class
    at or above it is dropped (it means the same as no class: the string is scanned), so no
    classed path lies under another. A scan-only path (`free_text:`) is classed `free_text`
    (scanned, and the step de-identified) unless a classed path lies at, above or below it
    (the step is de-identified anyway, and every string is scanned)."""
    merged: dict[str, dict[str, Any]] = {
        path: dict(value) for path, value in options.items() if isinstance(value, Mapping)
    }
    paths, free_text = split_personal(personal)
    for path in paths:
        steps = view_steps(path)
        classed = {
            other: value["class"]
            for other, value in merged.items()
            if isinstance(value.get("class"), str)
        }
        if any(
            steps[: len(view_steps(other))] == view_steps(other) and name != "free_text"
            for other, name in classed.items()
        ):
            continue
        for other in classed:
            other_steps = view_steps(other)
            if steps[: len(other_steps)] == other_steps or other_steps[: len(steps)] == steps:
                merged[other] = {k: v for k, v in merged[other].items() if k != "class"}
        merged[path] = {**merged.get(path, {}), "class": "person"}
    for path in free_text:
        steps = view_steps(path)
        if any(
            steps[: len(other_steps)] == other_steps or other_steps[: len(steps)] == steps
            for other, value in merged.items()
            if isinstance(value.get("class"), str)
            for other_steps in (view_steps(other),)
        ):
            continue
        merged[path] = {**merged.get(path, {}), "class": "free_text"}
    return {path: value for path, value in sorted(merged.items()) if value}


__all__ = [
    "COLLECT_ROOT",
    "MIN_NUMBER_DIGITS",
    "merged_options",
    "reduce_personal",
    "under",
    "values",
]
