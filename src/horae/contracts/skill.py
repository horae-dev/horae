# SPDX-License-Identifier: Apache-2.0
"""Skill manifest, `skill.yaml` (PLAN.md §2.1 tenet 2, §3.4, §8 M2; #27).

A skill is a versioned folder shipped by a plugin, at
`<plugin>/skills/<namespace>/<skill>/` for the skill `<namespace>/<skill>`. It holds:

- `skill.yaml` (this model): name, version, description, the **input view** (which fields of
  the step input the model sees), the files below and the intents the skill may propose;
- the prompt (`prompt.md` by default): plain UTF-8 text, sent verbatim (no template syntax in
  v1, `{{` and `}}` are reserved);
- the output JSON Schema (`output.schema.json` by default): draft 2020-12, bounded (every
  string has `maxLength`, every array `maxItems`, every object `additionalProperties: false`).

The file rules (sizes, symlinks, the bounded schema) are checked by the loader
(`horae.skills`), not by this model: they need the files.

**Input view paths.** A path names fields from the root of the step input: `name` selects a
key of an object, `[]` every element of an array, joined by `.`, e.g. `tasks[].title`,
`[].data.events[].start` (the root of a `collect` output is an array). A path selects the whole
value it ends on. Names are `[A-Za-z_][A-Za-z0-9_-]*`, so a key with another character cannot be
selected in v1. Two paths may not overlap (one a prefix of the other) or disagree on the shape
of a value (`a.b` and `a[]`): pydantic-only rules, the JSON Schema checks the syntax only.
"""

import itertools
from typing import Annotated, Self

from pydantic import Field, Strict, StringConstraints, model_validator

from horae.contracts.intents import IntentType
from horae.contracts.types import Contract, Line, SemVer, SkillRef, UniqueList

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
    input_view: Annotated[UniqueList[ViewPath], Field(min_length=1)]
    """The only fields of the step input the model sees (an allow-list)."""
    prompt: SkillFile = PROMPT_FILE
    """The prompt file, in the skill folder."""
    output_schema: SkillFile = OUTPUT_SCHEMA_FILE
    """The output JSON Schema file, in the skill folder."""
    intents: UniqueList[IntentType] = Field(default_factory=list)
    """Intents the skill may propose: within the shipping plugin's `permissions.intents`
    (checked by the loader). Empty means the skill proposes none."""

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
