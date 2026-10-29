# Plugin template

[`horae-dev/template-plugin`](https://github.com/horae-dev/template-plugin) is a GitHub
template repository for a new Horae plugin written with [`horae-sdk`](../../sdk/README.md).
"Use this template" gives a repository whose CI passes unchanged; you then rename the example
and write your own tools.

## What it holds

- **An example plugin** (`src/example_plugin/`): an MCP stdio server with one source,
  `example.inbox`, that proposes a `task.create` per message. The message subject is untrusted
  and reaches the title only through `intents.task_create`, which escapes it for Obsidian Tasks
  (threat model T02, [schemas decision 50](../../schemas/README.md)). The service client is a
  `Protocol`: the plugin ships with a stand-in, the tests use a fake one with synthetic data.
- **`plugin.yaml`**, written by `python -m example_plugin --manifest` and checked in a test by
  the SDK's manifest helpers (`horae_sdk.manifest`), both as written on disk and against the
  code's registered tools and `Permissions`. It declares only the `task.create` intent.
- **Recorded-input tests**: `tests/recordings/*.json` replayed through MCP with
  `horae_sdk.testing.replay_all` (including a hostile subject), and a test that starts the
  plugin over stdio as the plugin host does.
- **CI** (`.github/workflows/ci.yml`): `uv lock --check`, ruff, mypy strict, pytest with an
  85% coverage gate, gitleaks over the full history, and an aggregate **CI OK** check;
  actions pinned by commit, `permissions: contents: read`, no persisted credentials.
- **`graft.yaml` and `AGENTS.md`**: the same review policy and working rules as the core
  (`plugin.yaml`, workflows, `auth/`, `pyproject.toml` and `renovate.json` are sensitive),
  plus `.pre-commit-config.yaml` (ruff, gitleaks), `renovate.json` (7-day minimum release
  age) and a pull request template.

## Using it

The template's [README](https://github.com/horae-dev/template-plugin#use-this-template) has
the steps: create the repository, rename `example` / `example_plugin`, write the tools and
their permissions, regenerate `plugin.yaml`, record fixtures (`HORAE_SDK_RECORD=1 uv run
pytest`, refused in CI) and run the CI commands locally.

## horae-sdk before PyPI

Until `horae-sdk` is published, the template takes it from this repository in
`[tool.uv.sources]`, pinned to a full commit of `main` (`subdirectory = "sdk"`). Renovate bumps
the pin weekly (a git commit has no release date, so the 7-day age cannot apply); its pull
request needs `uv lock`. After the first `Release SDK` run (`sdk-v<version>` tag), remove the
git source and the Renovate manager from the template so the requirement resolves from PyPI.
A change to the SDK's public API should be checked against the template's tests.
