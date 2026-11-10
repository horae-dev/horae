# Horae

Horae is a **predictable, self-hosted personal agent**. You declare a pipeline in YAML
(`collect → transform → llm → act → notify`); Horae runs it on a schedule, and every change it
makes can be previewed, journaled and undone.

!!! note "Early software"
    Horae is pre-release. This site describes the code on `main`; the design decisions it
    points to are **provisional** until the maintainer accepts them, and some features are
    still planned (the pages say which). Check the [roadmap](ROADMAP.md).

## Where to start

| I want to... | Read |
|---|---|
| understand the vocabulary | [Concepts](concepts.md) |
| run Horae with Docker or on Coolify | [Quick start](quickstart.md) |
| write a plugin | [Writing a plugin](writing-a-plugin.md) |
| know what Horae defends against | [Security model](security.md) |
| look up a command | [CLI reference](reference/cli.md) |
| operate a running instance | [Docker Compose](operations/compose.md), [Coolify](operations/coolify.md), [backups](operations/retention-and-backups.md) |

## The ideas in one minute

- **Pipelines, not agent loops.** The control flow is written down, never chosen by a model
  ([ADR 0001](adr/0001-pipelines-not-agent-loops.md)).
- **One bounded LLM step.** A skill, an output schema, a model alias and a budget, and no
  tools.
- **Plugins propose; the core applies.** A plugin returns *intents*; the core validates them,
  shows a dry-run diff, journals them and applies them ([ADR 0002](adr/0002-intents-applied-by-the-core.md)).
- **Declared permissions.** Hosts, secrets, paths and intents are approved at install and pinned
  in `horae.lock`.
- **Nothing updates itself.** Plugins come from git at a pinned commit
  ([ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md)).
- **Bring your own model**: the Anthropic API, your own `claude -p`, OpenAI-compatible
  endpoints or Ollama.

Source and issues: [github.com/horae-dev/horae](https://github.com/horae-dev/horae).
