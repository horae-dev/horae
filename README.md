<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# horae

Core runtime, CLI, HTTP API, MCP server, SDK and docs for Horae, a predictable self-hosted personal agent.

> 🚧 **Planned.** Nothing to install yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What Horae is

Horae is a **predictable, self-hosted personal agent**:

- **Pipelines, not agent loops**: `collect → transform → llm → act → notify`, declared in YAML.
- **One bounded LLM step**: a skill, an output schema, a model alias, a budget and **no tools**.
- **Plugins return intents; the core applies them** with a dry-run diff, a journal and `horae undo`.
- **Declared permissions**: hosts, secrets, paths and intents, approved at install and enforced at run time.
- **A git marketplace**: plugins pinned to a commit in `horae.lock`; nothing updates itself.
- **Bring your own model**: Anthropic API, your own `claude -p`, OpenAI-compatible endpoints or Ollama.

Read the [plan](docs/PLAN.md), the [roadmap](docs/ROADMAP.md) and the [estimation](docs/ESTIMATION.md).

## Planned work

| Milestone | Epic |
|---|---|
| M0 Foundations | **Foundations** — Repository, CI, ADRs, JSON Schemas, threat model and the agent workflow that every other epic relies on. |
| M1 Walking skeleton | **Pipeline engine and plugin host** — Load an instance, run pipelines, record runs, apply intents with dry-run, journal, undo and replay; talk to plugins over MCP stdio. |
| M2 LLM step | **Bounded LLM step** — Skills with output schemas, model backends (anthropic, claude-cli, openai-compatible, ollama), cache, budgets, de-identification and untrusted-content guards. |
| M3 Dogfood | **Runtime and delivery** — HTTP API, signed action links, scheduler, container image, Coolify template and the plugin SDK. |
| M4 Marketplace | **Marketplace client** — Install, upgrade, remove and audit plugins from git with pinned commits, per-plugin venvs and approved permissions. |
| M5 Hard isolation | **Hard isolation** — Per-plugin containers, egress allow-list proxy, declared mounts, secret redaction and a malicious-plugin test suite. |
| M5 Hard isolation | **Horae MCP server** — Read-only MCP tools for other agents and an approval-gated pipeline.run. |
| M8 Public v0.1 | **Docs and v0.1 release** — Docs site, release automation, signed artifacts and the v0.1 release. |
| M0 Foundations | **Human operations** — Tasks only a human can do: accounts, keys, GitHub App installs, server resources and device tests. |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Getting started

From a checkout (`uv sync`; nothing is published yet), the M1 walking skeleton in a new
directory: a pipeline that reads synthetic data and writes today's owned note in the
instance's own `vault/`, through the two built-in plugins. No LLM, no secret, no network.

```sh
uv run horae init ~/horae-demo            # horae.yaml, a pipeline, horae.lock, vault/, plugins/
uv run horae doctor -i ~/horae-demo       # "OK: 1 pipeline, 2 plugins, 0 model aliases"
uv run horae run daily-fixture -i ~/horae-demo --dry-run   # the diff; nothing written
uv run horae run daily-fixture -i ~/horae-demo             # writes vault/Daily/<today>.assistant.md
uv run horae runs -i ~/horae-demo                          # runs, newest first
uv run horae diff <run-id> -i ~/horae-demo     # what a stored (dry) run would change
uv run horae replay <run-id> -i ~/horae-demo   # re-executes the run from its record and compares
uv run horae undo <run-id> -i ~/horae-demo     # restores every note the run changed, byte for byte
uv run horae usage -i ~/horae-demo             # LLM cost per pipeline and day (the usage ledger)
uv run horae cache purge --all -i ~/horae-demo # forget cached LLM outputs (the next run asks again)
uv run horae eval daily-brief/rank-and-word --plugin-dir tests/fixtures/plugins  # golden cases, offline
```

`-i` defaults to the current directory (or `HORAE_INSTANCE`). Every command takes `--json`
for scripting (one JSON document on stdout, `schema_version: 1`, errors too) and prints no
colour unless asked. `horae run` has no LLM backend in M1: a pipeline with an `llm` step
fails with `llm_unavailable` (M2 adds the backends). SIGINT or SIGTERM stops a run
cooperatively and names it: before the apply its partial record is saved `failed`; once the
apply has started it finishes (each sink call is bounded by its timeout) and the record is
saved before the run is reported interrupted (a second signal is ignored).

**Scheduling** ([decision 49](schemas/README.md)). A pipeline's `trigger: {cron: "30 5 * * *"}`
(optional `timezone`, else the instance's) is run by `horae serve`, the long-running process
of a container, until SIGTERM (it then cancels and saves the runs in progress and exits 0;
`horae serve` is the one command without `--json`). Missed fire times are not caught up, and
a fire time while the pipeline is still running, here or in a manual `horae run`, is skipped
and logged. With `HORAE_SCHEDULER=external` (or `scheduler.mode: external`) no trigger
fires: Coolify Scheduled Tasks, cron or a systemd timer call `horae run <pipeline>`. Either
way, `scheduler.monitors.<pipeline>: {secret: PUSH_URL}` pings an Uptime Kuma push URL (or
`kind: healthchecks`), kept as a secret, `up` or `down` after each run; a failed ping is a
warning and never fails the run.

An instance is **not relocatable** in v1: `horae.lock` holds the vault's absolute path and
`plugins/` the absolute path of the interpreter that installed them. After copying or moving
one, `horae doctor` reports the paths that no longer match (a lock path that does not exist,
an own `vault/` the lock does not write to, a plugin command that is not executable); fix
the lock, or run `horae init` in the new place.

**Exit codes**, the same for every command ([decision 37](schemas/README.md)):

| Code | Meaning |
|---|---|
| 0 | ok: no problem, listed, planned, identical, undone, created |
| 1 | a configuration, store, plugin or runtime problem (doctor found problems, the run failed or timed out) |
| 2 | usage error |
| 3 | unknown or refused: unknown run or pipeline, a refused run, not comparable, a non-empty `init` target |
| 4 | different: the replay differs, the plan has problems, undo refused because a note changed |
| 5 | locked: the pipeline is running, or another process is applying changes |
| 70 | internal error (only its type is printed) |
| 130 / 143 | `horae run` interrupted by SIGINT / SIGTERM |

## Examples

- [`examples/walking-skeleton`](examples/walking-skeleton/): the M1 walking skeleton, an
  instance whose pipeline reads synthetic data and writes today's owned note, with dry run,
  undo and replay (`horae init` writes a copy of it). [`tests/test_e2e.py`](tests/test_e2e.py)
  runs it end to end with real plugin subprocesses, and
  [`tests/test_cli_e2e.py`](tests/test_cli_e2e.py) through the CLI.
- Built-in example plugins (`horae.plugins.builtin`):
  [`fixture-source`](src/horae/plugins/builtin/fixture_source/README.md), deterministic
  synthetic data from a JSON fixture file, and
  [`file-sink`](src/horae/plugins/builtin/file_sink/README.md), notes in a local vault, the
  core computing every note's bytes. They are small stdio MCP servers built with the `mcp`
  SDK, and a starting point for writing a plugin.

## Development

Setup, commands, conventions and the definition of done are in [AGENTS.md](AGENTS.md). Provenance trailers are added by `scripts/graft_trailers.py` (pre-commit `prepare-commit-msg`). `graft.yaml` is validated in tests against a vendored schema (`tests/fixtures/graft.v1.schema.json`, from mmornati/graft@3f40dd1).

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every PR and push to `main`: lockfile check, ruff, mypy, pytest with coverage, gitleaks (history of the checked-out ref) and CodeQL (python, actions). The aggregate job **CI OK** (`ci-ok`) fails if any other job failed, was cancelled or skipped; mark it as the single required status check in branch protection.

- Python comes from `.python-version`, installed by uv itself (`UV_PYTHON_PREFERENCE=only-managed`, cached via setup-uv `cache-python`). uv is pinned to an exact version in the workflow (`UV_VERSION`) and `[tool.uv] required-version` in `pyproject.toml` sets the minimum for local use. `UV_LOCKED=1` makes every `uv` command fail if `uv.lock` is out of date.
- The coverage gate is `fail_under` in `[tool.coverage.report]` of `pyproject.toml` (85); change it there.
- Actions are pinned to commit SHAs. Renovate keeps them current, and also bumps `UV_VERSION` and `GITLEAKS_VERSION` through regex custom managers (the `# renovate:` marker comments in the workflow).
- **Bumping gitleaks:** Renovate only updates the version. In the Renovate PR, update `GITLEAKS_SHA256` by hand with the `gitleaks_<version>_linux_x64.tar.gz` entry of the upstream `gitleaks_<version>_checksums.txt` release asset, otherwise the checksum step fails.
- **Historical leak:** if gitleaks flags a commit that is already in history (for example a revoked, synthetic or false-positive value), first revoke the secret if it was real. Then add the finding's fingerprint, printed as `Fingerprint: <commit>:<file>:<rule>:<line>` in the job output, as one line to `.gitleaksignore` at the repository root, with a comment explaining why. History is not rewritten.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
