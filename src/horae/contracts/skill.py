# SPDX-License-Identifier: Apache-2.0
"""Skill manifest, `skill.yaml` (PLAN.md §2.1 tenet 2, §3.4, §8 M2; #27).

A skill is a versioned folder shipped by a plugin, at
`<plugin>/skills/<namespace>/<skill>/` for the skill `<namespace>/<skill>`. It holds:

- `skill.yaml` (this model): name, version, description, the **input view** (which fields of
  the step input the model sees), the files below and the intents the skill may propose;
- the prompt (`prompt.md` by default): plain UTF-8 text, sent verbatim (no template syntax in
  v1, `{{` and `}}` are reserved);
- the output JSON Schema (`output.schema.json` by default): draft 2020-12, bounded (every
  string has `maxLength`, every array `maxItems`, every object `additionalProperties: false`,
  every number a minimum and a maximum), without regular expressions, and with the reserved
  top-level `intents` array exactly when the skill declares intents.

The file rules (sizes, symlinks, the bounded schema) are checked by the loader
(`horae.skills`), not by this model: they need the files.

**Input view paths.** A path names fields from the root of the step input: `name` selects a
key of an object, `[]` every element of an array, joined by `.`, e.g. `tasks[].title`,
`[].data.events[].start` (the root of a `collect` output is an array). A path selects the whole
value it ends on. Names are `[A-Za-z_][A-Za-z0-9_-]*`, so a key with another character cannot be
selected in v1. Two paths may not overlap (one a prefix of the other) or disagree on the shape
of a value (`a.b` and `a[]`): pydantic-only rules, the JSON Schema checks the syntax only.
The bracket-quoted form (`["a.b"]`) is reserved for keys with other characters.

`input_view` is either a list of paths or a mapping `path: {options}` (#34 declares field
classes there: `events[].attendees[]: {class: person}`). Options are free-form but bounded (at
most 16 per path, scalar values or short lists); `class`, when given, is one of
`FIELD_CLASSES`: before a cloud model, the fields of a class are de-identified
(`horae.llm.deid`, #34; `amount` also takes `buckets: [...]`). Options join the skill's digest
when present.

**Intents.** `intents` is either a list of intent types or a mapping `type: max` (#35): the
skill may propose at most `max` intents of that type per step (the engine enforces it,
`intent_rejected`). Omitted: the skill proposes none.

**Links.** `links` (schemas decision 55) is `input` (the default: a URL in note text must come
from the viewed input, decision 46) or `none`: the action guard then refuses every link,
autolink and bare URL in the `note.*` text the skill proposes, even one the input holds. A
skill that writes no link (a daily brief) should say `none`: the input allowance only lets
an attacker's URL through.
"""

import itertools
from collections.abc import Iterable
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, Field, Strict, StringConstraints, model_validator

from horae.contracts.intents import IntentType
from horae.contracts.types import (
    MAX_ITEMS,
    Contract,
    Line,
    SemVer,
    SkillRef,
    StrictBool,
    StrictFloat,
    StrictInt,
    UniqueList,
)

_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_STEP = rf"({_NAME}|\[\])"
VIEW_PATH_PATTERN = rf"{_STEP}(\.{_NAME}|\[\])*"
"""The syntax of a view path, without anchors (shared with a plugin's `personal_fields`)."""
MAX_VIEW_PATH = 256

ViewPath = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{VIEW_PATH_PATTERN}$", max_length=MAX_VIEW_PATH),
]
"""One input view path, e.g. `tasks[].title` (see the module docstring)."""

SkillFile = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", max_length=128),
]
"""A file of the skill folder: one path segment (no `/`, `\\`, `..` or leading dot)."""

FIELD_CLASSES = ("person", "email", "phone", "place", "amount", "free_text")
"""The field classes an input view option `class` may name (#34, `horae.llm.deid`)."""

OptionName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$", max_length=64)
]
OptionScalar = StrictBool | StrictInt | StrictFloat | Line
OptionValue = OptionScalar | Annotated[list[StrictInt | StrictFloat | Line], Field(max_length=64)]


def _known_class(options: dict[str, Any]) -> dict[str, Any]:
    value = options.get("class")
    if value is not None and value not in FIELD_CLASSES:
        raise ValueError(f"class must be one of {', '.join(FIELD_CLASSES)}")
    return options


ViewOptions = Annotated[
    dict[OptionName, OptionValue],
    Field(
        max_length=16,
        json_schema_extra={"properties": {"class": {"enum": list(FIELD_CLASSES)}}},
    ),
    AfterValidator(_known_class),
]
"""Options of one input view path, e.g. `{class: person}` (bounded, free-form in v1)."""

IntentMax = Annotated[int, Strict(), Field(ge=1, le=MAX_ITEMS)]

SkillLinks = Literal["input", "none"]
"""A skill's `links` option: `input` (URLs of the viewed input only) or `none` (no link in
note text)."""

FingerprintPrefix = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$", max_length=64)
]
"""The fixed start of a created task's fingerprint, e.g. `gmail:` (the `Fingerprint` alphabet)."""


class TaskFingerprintRule(Contract):
    """Where the ids behind a `task.create` fingerprint live in the viewed input (decision 67).

    A `task.create` the skill proposes must carry the fingerprint `prefix` + an id found at
    `ids` in the viewed input; any other fingerprint is a guard violation."""

    ids: ViewPath
    """A path of the skill's `input_view` (or below one) whose string values are the ids, e.g.
    `[].data.needs_interpretation[].id`. A list of strings is read as its elements."""
    prefix: FingerprintPrefix
    """What the fingerprint puts before the id, e.g. `gmail:`."""


PROMPT_FILE = "prompt.md"
OUTPUT_SCHEMA_FILE = "output.schema.json"


def view_steps(path: str) -> tuple[str, ...]:
    """The steps of a valid `ViewPath`: names and `[]`, e.g. `("tasks", "[]", "title")`."""
    steps: list[str] = []
    for part in path.split("."):
        name, _, rest = part.partition("[]")
        if name:
            steps.append(name)
        if part.endswith("[]"):
            steps.extend("[]" for _ in range(1 + rest.count("[]")))
    return tuple(steps)


class SkillManifest(Contract):
    """A skill's `skill.yaml`."""

    name: SkillRef
    """The skill, `<namespace>/<skill>`; its folder is `skills/<namespace>/<skill>/`."""
    version: SemVer
    """Skill version (semver). Part of the LLM cache key, with a digest of the files."""
    description: Line
    """One line, at most 500 characters."""
    input_view: (
        Annotated[UniqueList[ViewPath], Field(min_length=1)]
        | Annotated[dict[ViewPath, ViewOptions | None], Field(min_length=1, max_length=MAX_ITEMS)]
    )
    """The only fields of the step input the model sees (an allow-list): a list of paths, or a
    mapping `path: {options}` (`class`: de-identification before a cloud model, #34)."""
    prompt: SkillFile = PROMPT_FILE
    """The prompt file, in the skill folder."""
    output_schema: SkillFile = OUTPUT_SCHEMA_FILE
    """The output JSON Schema file, in the skill folder."""
    intents: UniqueList[IntentType] | dict[IntentType, IntentMax] = Field(default_factory=list)
    """Intents the skill may propose, a list of types or a mapping `type: max` (at most `max`
    per step): within the shipping plugin's `permissions.intents` (checked by the loader).
    Empty means the skill proposes none."""
    links: SkillLinks = "input"
    """Links in the skill's note text: `input` (only URLs of the viewed input, the default) or
    `none` (no link, autolink or bare URL at all; schemas decision 55)."""

    task_fingerprint: TaskFingerprintRule | None = None
    """Opt-in (decision 67): a `task.create` the skill proposes must carry the fingerprint
    `prefix` + an id read at `ids` in the viewed input. Omitted: a `task.create` fingerprint is
    free text, as before. Requires `task.create` in `intents`."""

    @property
    def view_paths(self) -> tuple[str, ...]:
        """The input view paths, sorted."""
        return tuple(sorted(self.input_view))

    @property
    def view_options(self) -> dict[str, dict[str, Any]]:
        """The non-empty options of the input view paths (mapping form), by path."""
        if isinstance(self.input_view, list):
            return {}
        return {path: dict(opts) for path, opts in sorted(self.input_view.items()) if opts}

    @property
    def intent_types(self) -> tuple[IntentType, ...]:
        """The intent types the skill may propose, sorted."""
        return tuple(sorted(self.intents))

    @property
    def intent_limits(self) -> dict[str, int]:
        """The maximum per step of each intent type (mapping form only)."""
        return {} if isinstance(self.intents, list) else dict(sorted(self.intents.items()))

    @model_validator(mode="after")
    def _fingerprint_rule_fits(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        rule = self.task_fingerprint
        if rule is None:
            return self
        if "task.create" not in self.intents:
            raise ValueError("task_fingerprint needs `task.create` in `intents`")
        wanted = view_steps(rule.ids)
        if not any(wanted[: len(view_steps(path))] == view_steps(path) for path in self.input_view):
            raise ValueError("task_fingerprint.ids must be an input_view path (or lie below one)")
        return self

    @model_validator(mode="after")
    def _paths_are_disjoint(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        problem = view_paths_problem(self.input_view)
        if problem is not None:
            raise ValueError(f"input_view: {problem}")
        return self


def shapes_disagree(one: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """Whether two paths' steps disagree on the shape of a value they share (`a.b` and
    `a[]`: one reads an object where the other reads an array)."""
    common = 0
    while common < min(len(one), len(other)) and one[common] == other[common]:
        common += 1
    return (
        common < len(one)
        and common < len(other)
        and (one[common] == "[]") != (other[common] == "[]")
    )


def view_paths_problem(paths: Iterable[str]) -> str | None:
    """Why a set of view paths is not usable together (one overlaps another, or two disagree
    on a value's shape), or `None`. Shared by a skill's `input_view` and a plugin's
    `personal_fields` (decision 68)."""
    steps = sorted(view_steps(path) for path in paths)
    for one, other in itertools.pairwise(steps):
        if other[: len(one)] == one:
            return f"{join_steps(one)} already selects {join_steps(other)} (overlapping paths)"
    for one in steps:
        for other in steps:
            if shapes_disagree(one, other):
                return (
                    f"{join_steps(one)} and {join_steps(other)} disagree on whether a value is "
                    "an object or an array"
                )
    return None


def outermost(paths: Iterable[str]) -> tuple[str, ...]:
    """The view paths, sorted, without one that lies under (or equals) another: the outer
    path already covers it."""
    kept: list[tuple[str, ...]] = []
    for steps in sorted({view_steps(path) for path in paths}):
        if not any(steps[: len(outer)] == outer for outer in kept):
            kept.append(steps)
    return tuple(join_steps(steps) for steps in kept)


def join_steps(steps: tuple[str, ...]) -> str:
    """The view path of `steps` (the inverse of `view_steps`)."""
    text = ""
    for step in steps:
        text += step if step == "[]" or not text else f".{step}"
    return text
