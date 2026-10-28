# SPDX-License-Identifier: Apache-2.0
"""Skills: the prompt, input view and output schema of the bounded LLM step (#27).

A skill `<namespace>/<skill>` is the folder `skills/<namespace>/<skill>/` of the plugin whose
manifest lists it in `provides.skills` (`HostRegistry.plugin_for_skill`):

```
skills/daily-brief/rank-and-word/
  skill.yaml            # SkillManifest: name, version, description, input_view, intents
  prompt.md             # the prompt, verbatim
  output.schema.json    # draft 2020-12, bounded
```

`load_skill(plugin_root, skill)` reads and checks everything once, at load time, and raises
`SkillError` on any problem:

- `skill.yaml` is valid YAML (the `horae.config` safe loader: no aliases, no duplicate keys)
  and a valid `SkillManifest` named as the skill; its `intents` are within the plugin's
  approved `permissions.intents`;
- the folder is reached from the plugin root one component at a time without following a
  symbolic link (`skills`, the namespace, the skill: a link anywhere is refused), and every
  file (`skill.yaml`, the prompt, the schema) is opened relative to it with `O_NOFOLLOW`: a
  regular file with exactly one hard link, of bounded size (`MAX_SKILL_FILE_BYTES`,
  `MAX_PROMPT_BYTES`, `MAX_SCHEMA_BYTES`). File names in `skill.yaml` are single path segments
  (`SkillFile`), so `../` cannot be written;
- the prompt is non-empty UTF-8 without a character of the categories
  `PROMPT_FORBIDDEN_CATEGORIES` (controls, format, private use, surrogates, unassigned, line
  and paragraph separators; tab and newline are allowed) or another invisible character
  (variation selectors, Hangul fillers), and without `{{` / `}}` (reserved for a later
  template syntax: v1 sends the prompt verbatim);
- the output schema is strict JSON (no duplicate keys, no `NaN`, no number that overflows to
  infinity) and passes `check_output_schema`: the draft 2020-12 metaschema, self-contained,
  free of regular expressions, bounded within `MAX_OUTPUT_BYTES`, with the reserved
  top-level `intents` array exactly when the skill declares intents.

Every problem is a `SkillError`. The loaded `Skill` has a `digest` (SHA-256 of its prompt,
schema, view and intents), which the engine puts in the LLM cache key with the version, an
`output_bound`, an `output_schema` (a fresh copy on each access) and `view(data)`, which
applies the input view (`InputView`). `DirectorySkills` is the engine's `SkillSource` over
installed plugins.
"""

from horae.skills._loader import (
    MAX_PROMPT_BYTES,
    MAX_SCHEMA_BYTES,
    MAX_SKILL_FILE_BYTES,
    PROMPT_FORBIDDEN_CATEGORIES,
    SKILL_FILE,
    SKILLS_DIR,
    DirectorySkills,
    Skill,
    SkillError,
    load_skill,
    skill_dir,
)
from horae.skills._schema import (
    INTENTS_PROPERTY,
    MAX_COMBINATOR_FANOUT,
    MAX_OUTPUT_BYTES,
    MAX_REF_HOPS,
    MAX_SCHEMA_DEPTH,
    SchemaProblem,
    check_output_schema,
)
from horae.skills._view import InputView

__all__ = [
    "INTENTS_PROPERTY",
    "MAX_COMBINATOR_FANOUT",
    "MAX_OUTPUT_BYTES",
    "MAX_PROMPT_BYTES",
    "MAX_REF_HOPS",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SKILL_FILE_BYTES",
    "PROMPT_FORBIDDEN_CATEGORIES",
    "SKILLS_DIR",
    "SKILL_FILE",
    "DirectorySkills",
    "InputView",
    "SchemaProblem",
    "Skill",
    "SkillError",
    "check_output_schema",
    "load_skill",
    "skill_dir",
]
