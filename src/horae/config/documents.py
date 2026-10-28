# SPDX-License-Identifier: Apache-2.0
"""Safe YAML loading that remembers where every value is, and precise validation errors.

Only the YAML safe schema is accepted (no Python or custom tags). Anchors, aliases and merge
keys (`<<`) are rejected: they make a file harder to review and can expand exponentially.
Duplicate keys, which PyYAML silently accepts, are rejected too, and so is nesting deeper than
`MAX_DEPTH` (checked while composing, before any recursion can run out of stack).

Validation errors are reported as `Problem`s with a file, line and column: at the value when
the value is wrong, at the key when a key is unknown, missing (its parent) or invalid. A
mapping key that is not a valid name is never echoed (it may be a secret pasted in the wrong
place): it is shown as `<key at line N>`. Messages are written for people: no regular
expressions, and a union (a pipeline step, a secret reference) gives one message.
"""

import os
import re
import stat
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, StringConstraints, ValidationError

from horae.contracts import instance as _instance
from horae.contracts import lockfile as _lockfile
from horae.contracts import pipeline as _pipeline
from horae.contracts import types as _types

MAX_FILE_BYTES = 1024 * 1024
"""Largest configuration file read, in bytes."""

MAX_DEPTH = 64
"""Deepest nesting of mappings and sequences accepted."""

Loc = tuple[str | int, ...]
"""A path into a document, e.g. `("steps", 2, "llm", "model")`."""

_MERGE_TAG = "tag:yaml.org,2002:merge"
_COLLECTION_START = (yaml.SequenceStartEvent, yaml.MappingStartEvent)


class _Loader(yaml.SafeLoader):
    """The safe loader, refusing aliases and nesting deeper than `MAX_DEPTH`."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None, None, "anchors and aliases are not supported", event.start_mark
            )
        collection = self.check_event(*_COLLECTION_START)
        if collection:
            self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                event = self.peek_event()  # type: ignore[no-untyped-call]
                raise yaml.composer.ComposerError(
                    None, None, f"nested deeper than {MAX_DEPTH} levels", event.start_mark
                )
            return super().compose_node(parent, index)
        finally:
            if collection:
                self._depth -= 1


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


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$")


@dataclass(frozen=True)
class YamlDocument:
    """A parsed YAML file and the position of each key and value in it."""

    path: Path
    data: Any
    positions: dict[Loc, Position]
    """For a mapping entry, the position of its key; otherwise of the value itself."""
    values: dict[Loc, Position] = field(default_factory=dict)
    """The position of each value (for a mapping entry, where its value starts)."""

    def position(self, loc: Sequence[str | int], *, value: bool = False) -> Position:
        """Position of `loc` (its key, or its value if `value`), or of its deepest ancestor
        that exists in the file."""
        key = tuple(loc)
        while True:
            if value and key in self.values:
                return self.values[key]
            if key in self.positions:
                return self.positions[key]
            key = key[:-1]

    def problem(self, loc: Sequence[str | int], message: str, *, value: bool = True) -> Problem:
        """A problem at the value of `loc` (or at its key, if not `value`)."""
        pos = self.position(loc, value=value)
        return Problem(str(self.path), pos.line, pos.column, message)

    def key_text(self, loc: Sequence[str | int]) -> str:
        """The last key of `loc` as it may be shown: quoted, or `<key at line N>`."""
        key = loc[-1]
        if isinstance(key, int) or _SAFE_KEY.match(key):
            return repr(key)
        return f"<key at line {self.position(loc).line}>"


def _position(mark: Any) -> Position:
    return Position(mark.line + 1, mark.column + 1)


def _shown_key(key: Hashable, mark: Any) -> str:
    if isinstance(key, str) and _SAFE_KEY.match(key):
        return repr(key)
    return f"<key at line {mark.line + 1}>"


def _walk(
    loader: _Loader,
    node: yaml.Node,
    loc: Loc,
    positions: dict[Loc, Position],
    values: dict[Loc, Position],
) -> None:
    if isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            positions[(*loc, index)] = values[(*loc, index)] = _position(item.start_mark)
            _walk(loader, item, (*loc, index), positions, values)
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
                shown = _shown_key(key, key_node.start_mark)
                raise _NodeError(
                    key_node, f"duplicate key {shown} (first defined at line {first[key].line})"
                )
            first[key] = key_pos
            child = (*loc, key) if isinstance(key, str | int) else loc
            positions[child] = key_pos
            values[child] = _position(value_node.start_mark)
            _walk(loader, value_node, child, positions, values)


class _NodeError(Exception):
    def __init__(self, node: yaml.Node, message: str) -> None:
        super().__init__(message)
        self.node = node
        self.message = message


def _yaml_problem(path: Path, exc: yaml.YAMLError) -> Problem:
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(part for part in (exc.context, exc.problem) if part) or "invalid YAML"
        if mark is None:
            return Problem(str(path), None, None, message)
        pos = _position(mark)
        return Problem(str(path), pos.line, pos.column, message)
    return Problem(str(path), None, None, f"invalid YAML: {exc}")


def parse_yaml(text: str, path: Path) -> YamlDocument:
    """Parse one YAML document safely, or raise `ProblemsError` with its position."""
    loader = _Loader(text)
    problem: Problem | None = None
    try:
        node = loader.get_single_node()
        if node is None:
            return YamlDocument(path, None, {(): Position(1, 1)}, {(): Position(1, 1)})
        root = _position(node.start_mark)
        positions: dict[Loc, Position] = {(): root}
        values: dict[Loc, Position] = {(): root}
        _walk(loader, node, (), positions, values)
        data = loader.construct_document(node)
    except _NodeError as exc:
        pos = _position(exc.node.start_mark)
        problem = Problem(str(path), pos.line, pos.column, exc.message)
    except yaml.YAMLError as exc:
        problem = _yaml_problem(path, exc)
    except RecursionError:
        problem = Problem(str(path), None, None, f"nested deeper than {MAX_DEPTH} levels")
    finally:
        loader.dispose()
    if problem is not None:
        raise ProblemsError([problem])
    return YamlDocument(path, data, positions, values)


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


def _read_bytes(path: Path) -> bytes | str:
    """The content of `path`, or why it cannot be read. Opened once, never blocks."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return "file not found"
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not a regular file"
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    finally:
        os.close(fd)
    if size > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES} bytes"
    return b"".join(chunks)


def read_yaml(path: Path) -> YamlDocument:
    """Read and parse `path`, or raise `ProblemsError`."""
    raw = _read_bytes(path)
    if isinstance(raw, str):
        raise ProblemsError([Problem(str(path), None, None, raw)])
    try:
        text: str | None = raw.decode("utf-8")
        bad_at = 0
    except UnicodeDecodeError as exc:
        text, bad_at = None, exc.start
    if text is None:
        line = raw[:bad_at].count(b"\n") + 1
        raise ProblemsError([Problem(str(path), line, None, "not valid UTF-8")])
    return parse_yaml(text, path)


# --- pydantic errors to problems -------------------------------------------------------------


def _pattern(annotation: Any) -> str:
    for meta in get_args(annotation)[1:]:
        if isinstance(meta, StringConstraints) and meta.pattern is not None:
            return str(meta.pattern)
    raise ValueError(f"{annotation!r} has no pattern")  # pragma: no cover


_ONE_LINE = "must be one line, without control characters"

PATTERN_MESSAGES: dict[str, str] = {
    _pattern(_types.Line): _ONE_LINE,
    _pattern(_types.Heading): _ONE_LINE,
    _pattern(_instance.InstancePath): "must be a path without control characters",
    _pattern(_types.Token): "must be one word: no spaces or control characters",
    _pattern(_types.PluginName): (
        "must match [a-z][a-z0-9-]*: a lowercase letter, then lowercase letters, digits or -"
    ),
    _pattern(_types.PluginRef): "must be a plugin tool, <namespace>.<tool> (e.g. tasks.normalize)",
    _pattern(_types.SkillRef): "must be a skill, <namespace>/<skill> (e.g. daily-brief/rank)",
    _pattern(_types.SemVer): "must be a semantic version, e.g. 1.2.3",
    _pattern(_types.CommitSha): "must be a full git commit SHA (40 or 64 lowercase hex digits)",
    _pattern(_types.Sha256Hex): "must be a SHA-256 digest (64 lowercase hex digits)",
    _pattern(_types.IanaTimezone): "must be an IANA time zone name, e.g. Europe/Paris",
    _pattern(_types.SecretName): (
        "must match [A-Z][A-Z0-9_]*: an upper-case letter, then upper-case letters, digits or _"
    ),
    _pattern(_types.Hostname): (
        "must be a lowercase DNS host name: no scheme, port, IP address or wildcard"
    ),
    _pattern(_types.HttpsUrl): (
        "must be an https URL without credentials, fragment, spaces or IP address"
    ),
    _pattern(_types.AbsolutePath): (
        "must be a normalised absolute path: no empty, `.` or `..` segment, no trailing /"
    ),
    _pattern(_instance.ModelAliasName): (
        "must match [a-z][a-z0-9._-]*: a lowercase letter, then lowercase letters, digits, "
        "., _ or -"
    ),
    _pattern(_instance.EnvVarName): (
        "must be an environment variable name: letters, digits and _, not starting with a digit"
    ),
    _pattern(_instance.SecretFileName): (
        "must be a single file name of letters, digits, ., _ or -, not starting with ."
    ),
    _pattern(_instance.BackendUrl): (
        "must be an https URL, or an http URL to a loopback, private or single-label host, "
        "without credentials or fragment"
    ),
    _pattern(_pipeline.CronExpression): (
        "must be a 5-field cron expression separated by single spaces, e.g. `30 5 * * *`"
    ),
    _pattern(_lockfile.GitRepoUrl): (
        "must be an https git URL, e.g. https://example.org/owner/repo.git"
    ),
}
"""Human messages for each pattern the loader can meet; never the regular expression."""

_EXPECTED = {
    "model_type": "a mapping",
    "model_attributes_type": "a mapping",
    "dict_type": "a mapping",
    "is_instance_of": "a mapping",
    "string_type": "a string",
    "list_type": "a list",
    "int_type": "an integer",
    "float_type": "a number",
    "bool_type": "true or false",
}
"""Type errors, and what was expected instead."""

_STEP_KINDS = tuple(next(iter(m.model_fields)) for m in get_args(_pipeline.Step))
_UNION_NOUNS = {
    frozenset(_STEP_KINDS): "step kind",
    frozenset(("env", "file")): "secret source",
}


def _message(error: Any) -> str:
    kind: str = error["type"]
    if kind in _EXPECTED:
        return f"expected {_EXPECTED[kind]}"
    if kind == "string_pattern_mismatch":
        pattern = (error.get("ctx") or {}).get("pattern")
        return PATTERN_MESSAGES.get(str(pattern), "has an invalid format")
    if kind == "extra_forbidden":
        return "unknown key"
    message = str(error["msg"])
    return message.removeprefix("Value error, ")


@dataclass
class _Located:
    path: Loc
    """Where in the data the error is."""
    kind: str
    message: str
    at_key: bool
    """Whether the error concerns the key at `path` (unknown, invalid or missing key)."""
    branches: list[tuple[Loc, str]]
    """The union members the error went through: (where in the data, member tag)."""
    mismatch: tuple[Loc, str] | None = None
    """The union member this error proves does not match at all, if any."""
    missing: str | None = None
    """For a missing key, its name."""
    bad_key: bool = False
    """Whether the key at `path` itself is invalid (so it is never shown)."""


def _locate(data: Any, error: Any) -> _Located:
    loc: tuple[str | int, ...] = error["loc"]
    kind: str = error["type"]
    current = data
    path: list[str | int] = []
    branches: list[tuple[Loc, str]] = []
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
            return _Located(tuple(path), kind, _message(error), True, branches, bad_key=True)
        if last and kind == "missing" and isinstance(current, dict):
            located = _Located(
                tuple(path),
                kind,
                f"missing required key {part!r}",
                True,
                branches,
                missing=str(part),
            )
            if branches and branches[-1][0] == tuple(path) and loc[index - 1] == branches[-1][1]:
                located.mismatch = branches[-1]
            return located
        # A union member tag, e.g. `CollectStep` or `str`, or a function validator name.
        branches.append((tuple(path), str(part)))
    located = _Located(tuple(path), kind, _message(error), kind == "extra_forbidden", branches)
    if (
        branches
        and kind in _EXPECTED
        and branches[-1][0] == tuple(path)
        and loc[-1] == branches[-1][1]
    ):
        located.mismatch = branches[-1]
    return located


def _display(doc: YamlDocument, path: Loc, masked: set[Loc]) -> str:
    out = ""
    for depth, part in enumerate(path):
        if isinstance(part, int):
            out += f"[{part}]"
            continue
        prefix = path[: depth + 1]
        shown = part
        if prefix in masked or not _SAFE_KEY.match(part):
            shown = f"<key at line {doc.position(prefix).line}>"
        out += f".{shown}" if out else shown
    return out or "(document)"


def _at(data: Any, path: Loc) -> Any:
    for part in path:
        data = data[part]
    return data


def _collapsed(doc: YamlDocument, at: Loc, items: Iterable[_Located]) -> str:
    """One message for a union where no member matches."""
    tags: list[str] = []
    expected: list[str] = []
    for item in items:
        if item.mismatch is None or item.mismatch[0] != at:
            continue
        if item.missing is not None and item.missing not in tags:
            tags.append(item.missing)
        elif item.kind in _EXPECTED and _EXPECTED[item.kind] not in expected:
            expected.append(_EXPECTED[item.kind])
    value = _at(doc.data, at)
    if tags and isinstance(value, dict):
        noun = _UNION_NOUNS.get(frozenset(tags), "key")
        choices = ", ".join(tags)
        unknown = [key for key in value if key not in tags]
        if unknown:
            shown = doc.key_text((*at, unknown[0])) if isinstance(unknown[0], str) else "?"
            return f"unknown {noun} {shown} (expected one of: {choices})"
        return f"expected exactly one {noun}: {choices}"
    return "expected " + " or ".join(expected or ["a different value"])


def validation_problems(doc: YamlDocument, error: ValidationError) -> list[Problem]:
    """Turn a pydantic error into problems at the right line.

    For a union (a pipeline step, a secret reference...), the members that do not match at
    all (a missing tag key, a wrong type) are dropped when another member came closer, so
    that `{collect: [...]}` reports the `collect` error only. When no member matches, the
    union gives one problem (`unknown step kind 'colect' (expected one of: ...)`)."""
    located = [_locate(doc.data, e) for e in error.errors(include_url=False, include_input=False)]
    members: dict[Loc, set[str]] = {}
    mismatched: dict[Loc, set[str]] = {}
    for item in located:
        for at, tag in item.branches:
            members.setdefault(at, set()).add(tag)
        if item.mismatch is not None:
            mismatched.setdefault(item.mismatch[0], set()).add(item.mismatch[1])
    masked = {item.path for item in located if item.bad_key}

    def dropped(item: _Located) -> bool:
        return any(
            tag in mismatched.get(at, set()) and members[at] - mismatched[at]
            for at, tag in item.branches
        )

    def no_match(item: _Located) -> Loc | None:
        """The outermost union this error is in where no member matches."""
        for at, _tag in item.branches:
            if members[at] == mismatched.get(at):
                return at
        return None

    kept = [item for item in located if not dropped(item)]
    problems: set[Problem] = set()
    for item in kept:
        union = no_match(item)
        if union is not None:
            message = _collapsed(doc, union, kept)
            problems.add(doc.problem(union, f"{_display(doc, union, masked)}: {message}"))
            continue
        shown = item.path if item.missing is None else (*item.path, item.missing)
        where = _display(doc, shown, masked)
        problems.add(doc.problem(item.path, f"{where}: {item.message}", value=not item.at_key))
    return sorted(problems, key=Problem.sort_key)


def validate[M: BaseModel](doc: YamlDocument, model: type[M]) -> M:
    """Validate `doc` against `model`, or raise `ProblemsError` with file and line."""
    data = {} if doc.data is None else doc.data
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = validation_problems(doc, exc)
    raise ProblemsError(problems)
