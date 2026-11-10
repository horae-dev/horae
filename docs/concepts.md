# Concepts

The words used across this site. Each section says where to read more. The authoritative
designs are the [ADRs](adr/README.md) and the [plan](PLAN.md); the contracts are in the
[schemas](reference/schemas.md).

!!! warning "Provisional"
    The decisions behind these concepts (the numbered decisions in the
    [schemas reference](reference/schemas.md), and the design notes) are **provisional**
    until the maintainer accepts them. Where a page says "planned", the code is not on
    `main` yet.

## Instance

An **instance** is one installation of Horae: a directory holding `horae.yaml` (models,
secrets references, scheduler, HTTP settings), a `pipelines/` folder, a `horae.lock` file, a
state directory (the SQLite store) and, for note sinks, a vault. `horae init <dir>` creates an
example one, and `-i <dir>` (or `HORAE_INSTANCE`) tells every command which one to use.

An instance is **not relocatable** in v1: the lock holds absolute paths. `horae doctor`
reports what no longer matches after a move.

## Pipeline

A **pipeline** is a YAML file in `pipelines/`: a name, an optional `trigger` (a cron
expression), then a list of steps. A run executes the steps in order. There is no loop, no
planner and no tool selection by a model ([ADR 0001](adr/0001-pipelines-not-agent-loops.md)).

```yaml
name: daily-fixture
steps:
  - collect:
      - fixture-source.serve: {}
  - act:
      - file-sink.write_note: { path: "Daily/{{date}}.assistant.md", mode: replace-owned }
```

Each execution is a **run**: the input snapshot, each step's output, the proposed and applied
changes, the cost and a status (`ok`, `noop`, `degraded` or `failed`). `horae runs` lists
them, `horae replay` re-executes one from its record, and `horae undo` reverses it.

## Steps

| Step | What it does | Who runs the code |
|---|---|---|
| `collect` | reads data from sources (a calendar, an issue tracker, a vault) | a plugin |
| `transform` | normalizes or filters the data, deterministically | a plugin |
| `llm` | one bounded model call: a skill, a model alias, a budget; **no tools** | the core |
| `act` | applies the intents collected so far, through sinks | the core and a sink plugin |
| `notify` | sends a notification | a notifier plugin |

A pipeline may contain **one** `llm` step. Its output must validate against the skill's
schema, or it is never applied (after one repair retry the run is `degraded`). With
`on_unchanged_input: skip`, unchanged input costs nothing: the model is not called.

## Intents

An **intent** is a proposed change, from a closed set with schemas: `note.create`,
`note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`,
`notify.push` and `http.post` (only to declared hosts). Plugins and the LLM never write
anything themselves: they return intents, and the core checks each against the plugin's
approved permissions, shows a dry-run diff (`horae run --dry-run`), journals it and hands it
to a sink ([ADR 0002](adr/0002-intents-applied-by-the-core.md)).

For a `note.*` intent the core computes the exact bytes to write, and the sink writes them
atomically only if the note still has the content the core saw (compare-and-swap).

## Sinks

A **sink** is a plugin that applies intents: it writes notes in a vault, edits one line of a
task, or posts to a declared host. A **notifier** is a sink for `notify.push`. A pipeline
never calls a sink directly: an `act` or `notify` entry names it, and the core calls it with
an already validated request. Horae writes only the files and blocks it owns (for example
`*.assistant.md`, or one task line under compare-and-swap).

## Skills

A **skill** is the prompt and the output schema of an `llm` step, shipped by a plugin or a
pipeline pack and named `<namespace>/<skill>` (for example `daily-brief/rank-and-word`). The
pipeline picks a skill and a model alias; the alias is resolved in `horae.yaml` to a backend
(`anthropic`, `claude-cli`, `openai-compatible` or `ollama`). Outputs are cached on (skill
version, model, normalized input) and counted in a usage ledger (`horae usage`).
`horae eval` runs a skill's golden cases offline.

All content a plugin collects is **untrusted data**: it can fill fields, never choose an
action, a recipient or a URL.

## The lock

`horae.lock` records each installed plugin's name, version, repository, **commit SHA** and the
**permissions approved** for it: hosts, local-network endpoints, secrets, paths and intents.
The core enforces the approved list, not the plugin's manifest: a plugin that asks for more
on upgrade needs a new approval, and nothing updates itself. The
[lockfile's canonical form](reference/schemas.md) is part of the schemas.

## The marketplace

The **marketplace** is a git repository holding an index (`marketplace.json`) that maps a
plugin name and version to a repository and a full commit SHA, with a tier (`first-party` or
`community`). Anyone can run their own index; `horae plugin search` reads the ones listed in
`horae.yaml`, and `horae plugin resolve <name>@<version>` prints the repository and pinned
commit ([ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md)).

!!! note "Status"
    Search and resolve are on `main`. Installing, upgrading, removing and auditing plugins
    (`horae plugin add | upgrade | remove | audit`, with the permission prompt and the lock
    write) are the M4 milestone and still being built; see the [roadmap](ROADMAP.md). Until
    then, `horae.lock` is written by hand, as in the [walking-skeleton
    example](https://github.com/horae-dev/horae/tree/main/examples/walking-skeleton).

Listing a plugin: see [Writing a plugin](writing-a-plugin.md#6-publishing-to-the-marketplace).
