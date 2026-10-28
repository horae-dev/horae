# SPDX-License-Identifier: Apache-2.0
"""Loading a skill folder: every file is read and checked once, at load time.

Reads are race-safe: the loader opens the plugin root, then walks `skills/<namespace>/<skill>`
one component at a time relative to the previous one (`dir_fd`), each opened
`O_DIRECTORY | O_NOFOLLOW`, and opens each file of the folder (one path segment) relative to
the skill folder with `O_NOFOLLOW | O_NONBLOCK`, checked with `fstat` (a regular file with one
hard link). A symbolic link anywhere in the skill tree (the `skills` folder, the namespace, the
skill folder or a file) is refused, so swapping a path for a link between a check and the read
cannot redirect it, and a hard link to a file elsewhere is refused too.
"""

import errno
import json
import math
import os
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from horae.config.documents import ProblemsError, parse_yaml, validate
from horae.contracts.intents import IntentType
from horae.contracts.skill import SkillManifest
from horae.contracts.types import SkillRef
from horae.engine.canonical import canonical_json, sha256_hex
from horae.engine.ports import ResolvedPlugin, SkillUnavailable
from horae.skills._schema import SchemaProblem, check_output_schema
from horae.skills._view import InputView

SKILLS_DIR = "skills"
"""Where a plugin keeps its skills: `<plugin>/skills/<namespace>/<skill>/`."""
SKILL_FILE = "skill.yaml"
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 64 * 1024
"""Largest prompt, in UTF-8 bytes."""
MAX_SCHEMA_BYTES = 64 * 1024
"""Largest output schema file, in bytes."""

_SKILL_REF: TypeAdapter[str] = TypeAdapter(SkillRef)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

PROMPT_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
"""Unicode categories refused in a prompt (but tab and newline): controls, format characters
(bidirectional controls, zero-width characters, U+00AD, U+180E, the tag characters U+E0000 to
U+E007F), private use, surrogates, unassigned code points and the line and paragraph
separators U+2028 / U+2029."""
_INVISIBLE = frozenset(
    {0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)}
)
"""Invisible characters of other categories: the combining grapheme joiner, the Hangul
fillers and the variation selectors (U+FE00 to U+FE0F, U+E0100 to U+E01EF)."""


def prompt_problem(text: str) -> tuple[int, str] | None:
    """The first refused character of `text`, as `(line, "U+XXXX")`, or `None`."""
    for position, char in enumerate(text):
        if char in "\t\n":
            continue
        if unicodedata.category(char) in PROMPT_FORBIDDEN_CATEGORIES or ord(char) in _INVISIBLE:
            return text.count("\n", 0, position) + 1, f"U+{ord(char):04X}"
    return None


class SkillError(SkillUnavailable):
    """A skill that cannot be used: missing, unreadable or invalid files.

    The message starts with the skill and names the file. It may echo content of the
    plugin's own files (e.g. the YAML problem in `skill.yaml`, an output schema's keyword),
    never instance data or secrets; the CLI prints it through `horae.apply.render.safe`."""

    def __init__(self, skill: str, message: str) -> None:
        super().__init__(f"skill {skill}: {message}")
        self.skill = skill
        self.problem = message


@dataclass(frozen=True)
class Skill:
    """A loaded, validated skill (`horae.engine.LoadedSkill`)."""

    manifest: SkillManifest
    prompt: str
    """The prompt, verbatim."""
    schema_json: bytes = field(repr=False)
    """The output schema, as canonical JSON (`output_schema` parses a fresh copy)."""
    input_view: InputView = field(repr=False)
    digest: str
    """SHA-256 of the canonical JSON of `{name, version, prompt, output_schema, input_view,
    intents}` (and `view_options` / `intent_limits` when the manifest has any): a change to any
    file that changes what is sent changes it."""
    output_bound: int
    """The output schema's size bound, in UTF-8 bytes (`check_output_schema`)."""

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def intents(self) -> tuple[IntentType, ...]:
        return self.manifest.intent_types

    @property
    def intent_limits(self) -> Mapping[str, int]:
        """The most intents of each type the skill may propose per step (map form)."""
        return self.manifest.intent_limits

    @property
    def view_paths(self) -> tuple[str, ...]:
        return self.input_view.paths

    @property
    def output_schema(self) -> dict[str, Any]:
        """A fresh deep copy of the output schema: mutating it changes nothing here."""
        result: dict[str, Any] = json.loads(self.schema_json)
        return result

    def view(self, data: Any) -> Any:
        """`data` restricted to the input view (`InputView.apply`)."""
        return self.input_view.apply(data)


def skill_dir(plugin_root: Path, skill: str) -> Path:
    """`<plugin_root>/skills/<namespace>/<skill>` for the skill `<namespace>/<skill>`."""
    namespace, name = skill.split("/", 1)
    return plugin_root / SKILLS_DIR / namespace / name


def _is_symlink(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_at(name: str, flags: int, dir_fd: int, what: str, skill: str) -> int:
    """`os.open(name, flags, dir_fd=dir_fd)`, every failure a `SkillError` about `what`."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise SkillError(skill, f"{what} is missing") from None
    except NotADirectoryError:
        if _is_symlink(name, dir_fd):  # `O_DIRECTORY | O_NOFOLLOW` on a link (macOS)
            raise SkillError(skill, f"{what} is a symbolic link") from None
        raise SkillError(skill, f"{what} is not a folder") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SkillError(skill, f"{what} is a symbolic link") from None
        raise SkillError(skill, f"{what} cannot be read ({error.strerror})") from None


def _open_folder(plugin_root: Path, skill: str) -> int:
    """A descriptor of the skill folder, walked from the plugin root without following links."""
    try:
        fd = os.open(plugin_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise SkillError(skill, f"no folder {SKILLS_DIR}/{skill} in the plugin") from None
    path = ""
    for part in (SKILLS_DIR, *skill.split("/", 1)):
        path = f"{path}/{part}" if path else part
        try:
            child = _open_at(part, _DIR_FLAGS, fd, path, skill)
        finally:
            os.close(fd)
        fd = child
    return fd


def _read(folder_fd: int, name: str, limit: int, skill: str) -> bytes:
    """The bytes of `name` in the skill folder: a regular file with one hard link, opened
    without following a link, at most `limit` bytes."""
    fd = _open_at(name, _FILE_FLAGS, folder_fd, name, skill)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SkillError(skill, f"{name} is not a regular file")
        if info.st_nlink != 1:
            raise SkillError(skill, f"{name} has more than one hard link")
        chunks: list[bytes] = []
        size = 0
        while size <= limit and (chunk := os.read(fd, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise SkillError(skill, f"{name} is larger than {limit} bytes")
    return data


def _text(data: bytes, name: str, skill: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError(skill, f"{name} is not valid UTF-8") from None


def _manifest(folder_fd: int, where: Path, skill: str) -> SkillManifest:
    data = _read(folder_fd, SKILL_FILE, MAX_SKILL_FILE_BYTES, skill)
    try:
        manifest = validate(
            parse_yaml(_text(data, SKILL_FILE, skill), where / SKILL_FILE), SkillManifest
        )
    except ProblemsError as error:
        first = error.problems[0]
        line = SKILL_FILE if first.line is None else f"{SKILL_FILE}:{first.line}"
        more = f" (and {len(error.problems) - 1} more)" if len(error.problems) > 1 else ""
        raise SkillError(skill, f"{line}: {first.message}{more}") from None
    if manifest.name != skill:
        raise SkillError(skill, f"{SKILL_FILE} names the skill {manifest.name}")
    return manifest


def _prompt(folder_fd: int, name: str, skill: str) -> str:
    text = _text(_read(folder_fd, name, MAX_PROMPT_BYTES, skill), name, skill)
    if not text.strip():
        raise SkillError(skill, f"{name} is empty")
    bad = prompt_problem(text)
    if bad is not None:
        line, code = bad
        raise SkillError(skill, f"{name}:{line}: control or invisible character {code}")
    if "{{" in text or "}}" in text:
        raise SkillError(skill, f"{name}: `{{{{` and `}}}}` are reserved (no templates in v1)")
    return text


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text[:40]} is not a finite number")
    return value


def _schema(
    folder_fd: int, name: str, skill: str, proposes_intents: bool
) -> tuple[dict[str, Any], int]:
    text = _text(_read(folder_fd, name, MAX_SCHEMA_BYTES, skill), name, skill)
    try:
        schema = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
            parse_float=_finite_float,
        )
    except RecursionError:
        raise SkillError(skill, f"{name} nests too deeply") from None
    except ValueError as error:
        raise SkillError(skill, f"{name} is not valid JSON ({str(error)[:200]})") from None
    try:
        bound = check_output_schema(schema, proposes_intents=proposes_intents)
    except SchemaProblem as problem:
        raise SkillError(skill, f"{name}: {problem}") from None
    return schema, bound


def _digest(manifest: SkillManifest, prompt: str, schema: dict[str, Any]) -> str:
    content: dict[str, Any] = {
        "name": manifest.name,
        "version": manifest.version,
        "prompt": prompt,
        "output_schema": schema,
        "input_view": list(manifest.view_paths),
        "intents": list(manifest.intent_types),
    }
    if manifest.view_options:
        content["view_options"] = manifest.view_options
    if manifest.intent_limits:
        content["intent_limits"] = manifest.intent_limits
    return sha256_hex(content)


def load_skill(
    plugin_root: Path, skill: str, *, allowed_intents: Iterable[str] | None = None
) -> Skill:
    """Load and validate the skill `skill` (`<namespace>/<skill>`) of the plugin installed at
    `plugin_root`; raise `SkillError` for any problem.

    `allowed_intents` is the plugin's `permissions.intents` (as approved in `horae.lock`):
    the skill may declare only those. `None` skips that check."""
    try:
        _SKILL_REF.validate_python(skill)
    except ValidationError:
        raise SkillError(skill, "not a skill name (`<namespace>/<skill>`)") from None
    folder_fd = _open_folder(plugin_root, skill)
    try:
        manifest = _manifest(folder_fd, skill_dir(plugin_root, skill), skill)
        if allowed_intents is not None:
            extra = sorted(set(manifest.intent_types) - set(allowed_intents))
            if extra:
                raise SkillError(
                    skill,
                    f"intents {', '.join(extra)} are not in the plugin's permissions.intents",
                )
        prompt = _prompt(folder_fd, manifest.prompt, skill)
        schema, bound = _schema(
            folder_fd, manifest.output_schema, skill, bool(manifest.intent_types)
        )
    finally:
        os.close(folder_fd)
    try:
        schema_json = canonical_json(schema)
        digest = _digest(manifest, prompt, schema)
    except (RecursionError, ValueError) as error:  # a lone surrogate in an annotation
        raise SkillError(skill, f"cannot be digested ({type(error).__name__})") from None
    return Skill(manifest, prompt, schema_json, InputView(manifest.view_paths), digest, bound)


class DirectorySkills:
    """A `SkillSource` over installed plugin folders, by plugin name.

    Each skill is loaded once per instance (one run): a run sees the files as they were when
    it planned. A plugin without a folder (`None`) ships no loadable skill."""

    def __init__(self, roots: Mapping[str, Path | None]) -> None:
        self._roots = dict(roots)
        self._loaded: dict[tuple[str, str], Skill] = {}

    def load_skill(self, skill: str, plugin: ResolvedPlugin) -> Skill:
        name = plugin.manifest.name
        key = (name, skill)
        if key not in self._loaded:
            root = self._roots.get(name)
            if root is None:
                raise SkillError(skill, f"plugin {name} has no installed folder")
            allowed = plugin.manifest.permissions.intents
            self._loaded[key] = load_skill(root, skill, allowed_intents=allowed)
        return self._loaded[key]
