# SPDX-License-Identifier: Apache-2.0
"""Safe YAML loading that remembers where every value is, and precise validation errors.

Only the YAML safe schema is accepted (no Python or custom tags). Anchors, aliases and merge
keys (`<<`) are rejected: they make a file harder to review and can expand exponentially.
Duplicate keys, which PyYAML silently accepts, are rejected too.
"""

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

MAX_FILE_BYTES = 1024 * 1024
"""Largest configuration file read, in bytes."""

MAX_DEPTH = 64
"""Deepest nesting of mappings and sequences accepted."""

Loc = tuple[str | int, ...]
"""A path into a document, e.g. `("steps", 2, "llm", "model")`."""

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _Loader(yaml.SafeLoader):
    """The safe loader, refusing aliases where they appear."""

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None, None, "anchors and aliases are not supported", event.start_mark
            )
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class Problem:
    """One configuration problem, with the file and (when known) the 1-based line and column."""

    file: str
    line: int | None
    column: int | None
    message: str
    severity: Literal["error"] = field(default="error", compare=False)

    def __str__(self) -> str:
        where = self.file
        if self.line is not None:
            where += f":{self.line}"
            if self.column is not None:
                where += f":{self.column}"
        return f"{where}: {self.message}"

    def sort_key(self) -> tuple[str, int, int, str]:
        return (self.file, self.line or 0, self.column or 0, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "message": self.message,
        }


class ProblemsError(Exception):
    """One or more configuration problems."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(str(p) for p in self.problems))


@dataclass(frozen=True)
class Position:
    line: int
    column: int


@dataclass(frozen=True)
class YamlDocument:
    """A parsed YAML file and the position of each value in it."""

    path: Path
    data: Any
    positions: dict[Loc, Position]
    """For a mapping entry, the position of its key; otherwise of the value itself."""

    def position(self, loc: Sequence[str | int]) -> Position:
        """Position of `loc`, or of its deepest ancestor that exists in the file."""
        key = tuple(loc)
        while key not in self.positions:
            key = key[:-1]
        return self.positions[key]

    def problem(self, loc: Sequence[str | int], message: str) -> Problem:
        pos = self.position(loc)
        return Problem(str(self.path), pos.line, pos.column, message)


def _position(mark: Any) -> Position:
    return Position(mark.line + 1, mark.column + 1)


def _walk(
    loader: _Loader,
    node: yaml.Node,
    loc: Loc,
    positions: dict[Loc, Position],
) -> None:
    if len(loc) > MAX_DEPTH:
        raise _NodeError(node, f"nested deeper than {MAX_DEPTH} levels")
    if isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            positions[(*loc, index)] = _position(item.start_mark)
            _walk(loader, item, (*loc, index), positions)
    elif isinstance(node, yaml.MappingNode):
        first: dict[Hashable, Position] = {}
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                raise _NodeError(key_node, "merge keys (<<) are not supported")
            if not isinstance(key_node, yaml.ScalarNode):
                raise _NodeError(key_node, "mapping keys must be plain values")
            key = loader.construct_object(key_node)
            key_pos = _position(key_node.start_mark)
            if key in first:
                raise _NodeError(
                    key_node, f"duplicate key {key!r} (first defined at line {first[key].line})"
                )
            first[key] = key_pos
            child = (*loc, key) if isinstance(key, str | int) else loc
            positions[child] = key_pos
            _walk(loader, value_node, child, positions)


class _NodeError(Exception):
    def __init__(self, node: yaml.Node, message: str) -> None:
        super().__init__(message)
        self.node = node
        self.message = message


def parse_yaml(text: str, path: Path) -> YamlDocument:
    """Parse one YAML document safely, or raise `ProblemsError` with its position."""
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return YamlDocument(path, None, {(): Position(1, 1)})
        positions: dict[Loc, Position] = {(): _position(node.start_mark)}
        _walk(loader, node, (), positions)
        data = loader.construct_document(node)
    except _NodeError as exc:
        pos = _position(exc.node.start_mark)
        raise ProblemsError([Problem(str(path), pos.line, pos.column, exc.message)]) from None
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(part for part in (exc.context, exc.problem) if part) or "invalid YAML"
        if mark is None:
            raise ProblemsError([Problem(str(path), None, None, message)]) from None
        pos = _position(mark)
        raise ProblemsError([Problem(str(path), pos.line, pos.column, message)]) from None
    except yaml.YAMLError as exc:
        raise ProblemsError([Problem(str(path), None, None, f"invalid YAML: {exc}")]) from None
    finally:
        loader.dispose()
    return YamlDocument(path, data, positions)


def read_yaml(path: Path) -> YamlDocument:
    """Read and parse `path`, or raise `ProblemsError`."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ProblemsError(
                [Problem(str(path), None, None, f"larger than {MAX_FILE_BYTES} bytes")]
            )
        raw = path.read_bytes()
    except OSError as exc:
        raise ProblemsError(
            [Problem(str(path), None, None, f"cannot be read ({exc.strerror})")]
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        raise ProblemsError([Problem(str(path), line, None, "not valid UTF-8")]) from None
    return parse_yaml(text, path)


# --- pydantic errors to positions ------------------------------------------------------------


@dataclass
class _Located:
    path: list[str | int]
    message: str
    branches: list[tuple[Loc, str]]
    mismatch: tuple[Loc, str] | None


def _locate(data: Any, error: Any) -> _Located:
    loc: tuple[str | int, ...] = error["loc"]
    kind: str = error["type"]
    current = data
    path: list[str | int] = []
    branches: list[tuple[Loc, str]] = []
    mismatch: tuple[Loc, str] | None = None
    for index, part in enumerate(loc):
        last = index == len(loc) - 1
        if isinstance(current, dict) and part in current:
            current = current[part]
            path.append(part)
            continue
        if (
            isinstance(current, list)
            and isinstance(part, int)
            and not isinstance(part, bool)
            and 0 <= part < len(current)
        ):
            current = current[part]
            path.append(part)
            continue
        if part == "[key]":
            continue
        if last and kind == "missing" and isinstance(current, dict):
            if branches and branches[-1][0] == tuple(path) and loc[index - 1] == branches[-1][1]:
                mismatch = branches[-1]
            return _Located([*path, part], f"missing required key {part!r}", branches, mismatch)
        # A union member tag, e.g. `CollectStep` or `str`, or a function validator name.
        branches.append((tuple(path), str(part)))
        if last and kind.endswith("_type"):
            mismatch = branches[-1]
    return _Located(path, str(error["msg"]), branches, mismatch)


def _display(path: Sequence[str | int]) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(document)"


def validation_problems(doc: YamlDocument, error: ValidationError) -> list[Problem]:
    """Turn a pydantic error into problems at the right line.

    For a union (a pipeline step, a secret reference...), the errors of the members that do
    not match at all (a missing tag key, a wrong type) are dropped when another member came
    closer, so that `{collect: [...]}` reports the `collect` error only."""
    located = [_locate(doc.data, e) for e in error.errors(include_url=False, include_input=False)]
    members: dict[Loc, set[str]] = {}
    mismatched: dict[Loc, set[str]] = {}
    for item in located:
        for at, tag in item.branches:
            members.setdefault(at, set()).add(tag)
        if item.mismatch is not None:
            mismatched.setdefault(item.mismatch[0], set()).add(item.mismatch[1])

    def dropped(item: _Located) -> bool:
        return any(
            tag in mismatched.get(at, set()) and members[at] - mismatched[at]
            for at, tag in item.branches
        )

    kept = [item for item in located if not dropped(item)] or located
    problems = {doc.problem(item.path, f"{_display(item.path)}: {item.message}") for item in kept}
    return sorted(problems, key=Problem.sort_key)


def validate[M: BaseModel](doc: YamlDocument, model: type[M]) -> M:
    """Validate `doc` against `model`, or raise `ProblemsError` with file and line."""
    data = {} if doc.data is None else doc.data
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ProblemsError(validation_problems(doc, exc)) from None
