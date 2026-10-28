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
- every file (`skill.yaml`, the prompt, the schema) is a regular file whose real path is inside
  the real skill folder (a symbolic link out of it is refused, and so is a skill folder that
  resolves outside the plugin), opened without following a final symlink, of bounded size
  (`MAX_SKILL_FILE_BYTES`, `MAX_PROMPT_BYTES`, `MAX_SCHEMA_BYTES`). File names in
  `skill.yaml` are single path segments (`SkillFile`), so `../` cannot be written;
- the prompt is non-empty UTF-8 without control characters (but tab and newline) or Unicode
  bidirectional / invisible format characters, and without `{{` / `}}` (reserved for a later
  template syntax: v1 sends the prompt verbatim);
- the output schema is strict JSON (no duplicate keys, no `NaN`) and passes
  `check_output_schema`: the draft 2020-12 metaschema, self-contained, bounded, within
  `MAX_OUTPUT_CHARS`.

The loaded `Skill` has a `digest` (SHA-256 of its prompt, schema, view and intents), which the
engine puts in the LLM cache key with the version, and `view(data)`, which applies the input
view (`InputView`). `DirectorySkills` is the engine's `SkillSource` over installed plugins.
"""

from horae.skills._loader import (
    MAX_PROMPT_BYTES,
    MAX_SCHEMA_BYTES,
    MAX_SKILL_FILE_BYTES,
    SKILL_FILE,
    SKILLS_DIR,
    DirectorySkills,
    Skill,
    SkillError,
    load_skill,
    skill_dir,
)
from horae.skills._schema import (
    MAX_OUTPUT_CHARS,
    MAX_SCHEMA_DEPTH,
    SchemaProblem,
    check_output_schema,
)
from horae.skills._view import InputView

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_PROMPT_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SKILL_FILE_BYTES",
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
