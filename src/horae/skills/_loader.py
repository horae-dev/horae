# SPDX-License-Identifier: Apache-2.0
"""Loading a skill folder: every file is read and checked once, at load time."""

import json
import os
import re
import stat
import types
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from horae.config.documents import ProblemsError, parse_yaml, validate
from horae.contracts.intents import IntentType
from horae.contracts.skill import SkillManifest
from horae.contracts.types import SkillRef
from horae.engine.canonical import sha256_hex
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
# C0 controls but tab and newline, DEL, C1 controls, and the Unicode bidirectional and
# invisible format characters that can make a prompt read differently than it runs.
_PROMPT_FORBIDDEN = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)


class SkillError(SkillUnavailable):
    """A skill that cannot be used: missing, unreadable or invalid files.

    The message starts with the skill and names the file, never its content."""

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
    schema: Mapping[str, Any] = field(repr=False)
    input_view: InputView = field(repr=False)
    digest: str
    """SHA-256 of the canonical JSON of `{name, version, prompt, output_schema, input_view,
    intents}`: a change to any file that changes what is sent changes it."""
    output_bound: int
    """The output schema's size bound (`check_output_schema`)."""

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def intents(self) -> tuple[IntentType, ...]:
        return tuple(self.manifest.intents)

    @property
    def output_schema(self) -> Mapping[str, Any]:
        """A read-only view of the output schema (`json.loads(json.dumps(...))` to copy)."""
        return types.MappingProxyType(self.schema)

    def view(self, data: Any) -> Any:
        """`data` restricted to the input view (`InputView.apply`)."""
        return self.input_view.apply(data)


def skill_dir(plugin_root: Path, skill: str) -> Path:
    """`<plugin_root>/skills/<namespace>/<skill>` for the skill `<namespace>/<skill>`."""
    namespace, name = skill.split("/", 1)
    return plugin_root / SKILLS_DIR / namespace / name


def _read(folder: Path, name: str, limit: int, skill: str) -> bytes:
    """The bytes of `folder/name`: a regular file whose real path is inside `folder` (a real
    path), at most `limit` bytes."""
    path = folder / name
    try:
        real = path.resolve(strict=True)
    except FileNotFoundError:
        raise SkillError(skill, f"{name} is missing") from None
    except (OSError, RuntimeError) as error:
        raise SkillError(skill, f"{name} cannot be read ({type(error).__name__})") from None
    if not real.is_relative_to(folder):
        raise SkillError(skill, f"{name} is a symbolic link out of the skill folder")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as error:
        raise SkillError(skill, f"{name} cannot be read ({error.strerror})") from None
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:  # pragma: no cover (fstat of an open descriptor)
        regular = False
    if not regular:
        os.close(fd)
        raise SkillError(skill, f"{name} is not a regular file")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise SkillError(skill, f"{name} is larger than {limit} bytes")
    return data


def _text(data: bytes, name: str, skill: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError(skill, f"{name} is not valid UTF-8") from None


def _manifest(folder: Path, skill: str) -> SkillManifest:
    data = _read(folder, SKILL_FILE, MAX_SKILL_FILE_BYTES, skill)
    try:
        manifest = validate(
            parse_yaml(_text(data, SKILL_FILE, skill), folder / SKILL_FILE), SkillManifest
        )
    except ProblemsError as error:
        first = error.problems[0]
        where = SKILL_FILE if first.line is None else f"{SKILL_FILE}:{first.line}"
        more = f" (and {len(error.problems) - 1} more)" if len(error.problems) > 1 else ""
        raise SkillError(skill, f"{where}: {first.message}{more}") from None
    if manifest.name != skill:
        raise SkillError(skill, f"{SKILL_FILE} names the skill {manifest.name}")
    return manifest


def _prompt(folder: Path, name: str, skill: str) -> str:
    text = _text(_read(folder, name, MAX_PROMPT_BYTES, skill), name, skill)
    if not text.strip():
        raise SkillError(skill, f"{name} is empty")
    bad = _PROMPT_FORBIDDEN.search(text)
    if bad is not None:
        line = text.count("\n", 0, bad.start()) + 1
        code = f"U+{ord(bad.group()):04X}"
        raise SkillError(skill, f"{name}:{line}: control or bidirectional character {code}")
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


def _schema(folder: Path, name: str, skill: str) -> tuple[dict[str, Any], int]:
    text = _text(_read(folder, name, MAX_SCHEMA_BYTES, skill), name, skill)
    try:
        schema = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except RecursionError:
        raise SkillError(skill, f"{name} nests too deeply") from None
    except ValueError as error:
        raise SkillError(skill, f"{name} is not valid JSON ({str(error)[:200]})") from None
    try:
        bound = check_output_schema(schema)
    except SchemaProblem as problem:
        raise SkillError(skill, f"{name}: {problem}") from None
    return schema, bound


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
    try:
        root = plugin_root.resolve(strict=True)
        folder = skill_dir(root, skill).resolve(strict=True)
    except (OSError, RuntimeError):
        raise SkillError(skill, f"no folder {SKILLS_DIR}/{skill} in the plugin") from None
    if not folder.is_relative_to(root) or not folder.is_dir():
        raise SkillError(skill, f"{SKILLS_DIR}/{skill} is not a folder inside the plugin")
    manifest = _manifest(folder, skill)
    if allowed_intents is not None:
        extra = sorted(set(manifest.intents) - set(allowed_intents))
        if extra:
            raise SkillError(
                skill, f"intents {', '.join(extra)} are not in the plugin's permissions.intents"
            )
    prompt = _prompt(folder, manifest.prompt, skill)
    schema, bound = _schema(folder, manifest.output_schema, skill)
    view = InputView(manifest.input_view)
    digest = sha256_hex(
        {
            "name": manifest.name,
            "version": manifest.version,
            "prompt": prompt,
            "output_schema": schema,
            "input_view": list(view.paths),
            "intents": sorted(manifest.intents),
        }
    )
    return Skill(manifest, prompt, schema, view, digest, bound)


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
