# Horae JSON Schemas

Machine-readable contracts for the documents Horae reads and writes ([PLAN.md](../docs/PLAN.md) §3).
All schemas use JSON Schema **draft 2020-12**.

| Schema | File | `$id` | Validates |
|---|---|---|---|
| Pipeline | [`pipeline/v1.json`](pipeline/v1.json) | `https://horae.dev/schemas/pipeline/v1.json` | `pipelines/*.yaml` (§3.2) |
| Plugin manifest | [`plugin-manifest/v1.json`](plugin-manifest/v1.json) | `https://horae.dev/schemas/plugin-manifest/v1.json` | `plugin.yaml` (§3.3) |
| Intent (any) | [`intent/v1.json`](intent/v1.json) | `https://horae.dev/schemas/intent/v1.json` | one intent, discriminated by `type` |
| One intent | `intents/<type>/v1.json` | `https://horae.dev/schemas/intents/<type>/v1.json` | `note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`, `notify.push`, `http.post` |
| Lockfile | [`lockfile/v1.json`](lockfile/v1.json) | `https://horae.dev/schemas/lockfile/v1.json` | `horae.lock` (§4) |
| Run record | [`run-record/v1.json`](run-record/v1.json) | `https://horae.dev/schemas/run-record/v1.json` | one run (§3.1, §2.1.6) |

## Source of truth

The pydantic models in [`src/horae/contracts/`](../src/horae/contracts/) are the source of truth.
The files here are **generated**; do not edit them by hand:

```
uv run python -m horae.contracts.export          # regenerate schemas/
uv run python -m horae.contracts.export --check  # exit 1 if schemas/ is stale
```

`tests/test_contracts.py` fails if the committed files differ from what the models generate,
and checks that the schema and the models agree on every example.

## Versioning

- Each schema has a versioned `$id`, `https://horae.dev/schemas/<name>/v<N>.json`, and lives at
  `<name>/v<N>.json`. The `$id` is an **identifier only**: nothing is served at that URL today.
- A breaking change adds `v<N+1>.json` next to the old file; compatible additions stay in `v<N>`.
- Files that Horae writes itself carry `schema_version` (`horae.lock`, run records). Files that
  people write (pipelines, `plugin.yaml`) do not, so the PLAN.md examples validate unchanged.

## Rules worth knowing

- Unknown keys are rejected everywhere (`additionalProperties: false`), except the free-form
  parameters of a plugin call and recorded data (source outputs, LLM requests and responses).
- A pipeline step is an object with exactly one key: `collect`, `transform`, `llm`, `act` or
  `notify`. Each lists plugin calls, either bare (`tasks.normalize`) or with parameters
  (`calendar.events: { days: 2 }`).
- The `llm` step has no `tools` key, and requires a budget with both `usd` and `tokens_out`.
- Plugin permissions (`network`, `secrets`, `paths`, `intents`) default to empty: no access.
- `format` keywords (`date`, `date-time`) are annotations unless your validator asserts formats
  (`FormatChecker` in Python's `jsonschema`). The pydantic models always check them.

## Examples

[`examples/<name>/valid/`](examples/) and [`examples/<name>/invalid/`](examples/) hold at least one
passing and one failing example per schema. `examples/pipeline/valid/daily-brief.yaml` and
`examples/plugin-manifest/valid/github.yaml` are the PLAN.md §3.2 and §3.3 examples, verbatim (a
test checks this). All other examples are synthetic.

## Open questions

PLAN.md leaves these open; v1 takes the minimal option and may change before v0.1:

1. **Intent names.** §2.1 says `create_task`, `append_note`, `notify`; §3.3 lists the closed set
   `note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`,
   `task.update_status`, `notify.push`, `http.post`. v1 uses the §3.3 names.
2. **Intent fields** are not specified anywhere. v1 uses the minimum each sink needs (see the
   per-intent schemas): e.g. `task.create` has `title`, optional `due`, `time` (`HH:MM`, the ⏰
   reminder) and `fingerprint`; `task.update_status` uses the fingerprint as task id and the
   statuses `todo`, `done`, `cancelled` (no `snoozed`); `notify.push` has `body`, optional
   `title` and `link`; `http.post` has `url` and a JSON `body` (no headers).
3. **Trigger.** Only `cron` (five fields, no timezone) is defined, and it is required. A pipeline
   run only by external triggers (`horae run`) still declares one.
4. **`on_unchanged_input`** accepts only `skip`: §2.2 forbids an LLM call on unchanged input, so
   no alternative was defined.
5. **LLM timeout.** §2.1 lists a timeout per LLM step but the §3.2 example has none, so
   `timeout_s` is optional and the core supplies a default.
6. **One LLM step per pipeline** "by default" is not enforced by the schema.
7. **Transforms in `provides`.** §3.1 lists sources, sinks, notifiers, skills and pipeline packs,
   but §3.2 uses plugin transforms (`tasks.normalize`), so `provides.transforms` was added.
8. **`permissions.paths`** is `{}` in §3.3 but its shape is not described. v1 uses
   `{ read: [...], write: [...] }`.
9. **`permissions.network`** takes bare lowercase host names: no port, scheme or wildcard.
10. **Remote runtime.** §3.3 mentions streamable HTTP; v1 names it `protocol: mcp-http` with a
    `url`. `runtime` is optional so pipeline packs and skill-only plugins need none.
11. **No `description`** (or license, author…) in `plugin.yaml`, since §3.3 shows none.
12. **`horae.lock` format.** §4 lists its content (name, version, SHA, approved permissions) but
    not its syntax; the schema validates the parsed document (the example is YAML) and adds the
    git `repo`.
13. **Run record.** §3.1 lists its parts but not their shape. v1 records plugin versions, input
    snapshots, one record per step, LLM calls (request, response, usage, `cached`), proposed
    intents with their origin, applied changes (detail is in the journal), total cost and an
    error. Untrusted-content tagging and the input hash are not part of v1.
14. **`$id` host.** `horae.dev` is a placeholder identifier; whether the project owns or will serve
    it is undecided.
