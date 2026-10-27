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

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every PR and push to `main`: lockfile check, ruff, mypy, pytest with coverage, gitleaks (full history) and CodeQL (python, actions). Python comes from `.python-version`. The coverage gate is `fail_under` in `[tool.coverage.report]` of `pyproject.toml` (85); change it there. Actions are pinned to commit SHAs (Renovate keeps them current).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
