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
`FIELD_CLASSES`. v1 does not act on options; they join the skill's digest when present.

**Intents.** `intents` is either a list of intent types or a mapping `type: max` (#35): the
skill may propose at most `max` intents of that type per step (the engine enforces it,
`intent_rejected`). Omitted: the skill proposes none.
"""

import itertools
from typing import Annotated, Any, Self

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

ViewPath = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{_STEP}(\.{_NAME}|\[\])*$", max_length=256),
]
"""One input view path, e.g. `tasks[].title` (see the module docstring)."""

SkillFile = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", max_length=128),
]
"""A file of the skill folder: one path segment (no `/`, `\\`, `..` or leading dot)."""

FIELD_CLASSES = ("person", "email", "phone", "place", "amount", "free_text")
"""The field classes an input view option `class` may name (#34; unused in v1)."""

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
    mapping `path: {options}` (options unused in v1)."""
    prompt: SkillFile = PROMPT_FILE
    """The prompt file, in the skill folder."""
    output_schema: SkillFile = OUTPUT_SCHEMA_FILE
    """The output JSON Schema file, in the skill folder."""
    intents: UniqueList[IntentType] | dict[IntentType, IntentMax] = Field(default_factory=list)
    """Intents the skill may propose, a list of types or a mapping `type: max` (at most `max`
    per step): within the shipping plugin's `permissions.intents` (checked by the loader).
    Empty means the skill proposes none."""

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
    def _paths_are_disjoint(self) -> Self:
        # Not expressible in JSON Schema: checked by the pydantic model only.
        steps = sorted(view_steps(path) for path in self.input_view)
        for one, other in itertools.pairwise(steps):
            if other[: len(one)] == one:
                raise ValueError(
                    f"input_view: {_join(one)} already selects {_join(other)} (overlapping paths)"
                )
        for one in steps:
            for other in steps:
                common = 0
                while common < min(len(one), len(other)) and one[common] == other[common]:
                    common += 1
                if (
                    common < len(one)
                    and common < len(other)
                    and (one[common] == "[]") != (other[common] == "[]")
                ):
                    raise ValueError(
                        f"input_view: {_join(one)} and {_join(other)} disagree on whether "
                        "a value is an object or an array"
                    )
        return self


def _join(steps: tuple[str, ...]) -> str:
    text = ""
    for step in steps:
        text += step if step == "[]" or not text else f".{step}"
    return text
