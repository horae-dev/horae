# Plugin template

[`horae-dev/template-plugin`](https://github.com/horae-dev/template-plugin) is a GitHub
template repository for a new Horae plugin written with [`horae-sdk`](../../sdk/README.md).
"Use this template" gives a repository whose CI passes unchanged; you then rename the example
with `scripts/rename.py` (its CI still passes) and write your own tools.

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
  actions pinned by commit, `permissions: contents: read`, no persisted credentials. In the
  template repository only, a **Template copy** job `git archive`s the commit into a fresh
  repository, renames it with `scripts/rename.py` and runs the same checks on the copy, so a
  renamed repository is known to pass; downstream copies skip it and CI OK counts that skip as
  a pass.
- **`scripts/rename.py <dist-name> <package> <plugin-name>`** (standard library only): renames
  the package and every reference to the example (distribution name, imports, the plugin's
  `NAME`, which namespaces its refs and fingerprints, the recordings, docstrings, titles), runs
  `uv lock`, regenerates `plugin.yaml`, re-records the recordings outside CI and fails if a
  reference is left.
- **`graft.yaml` and `AGENTS.md`**: the same review policy and working rules as the core
  (`plugin.yaml`, workflows, `auth/`, `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`
  and `renovate.json` are sensitive),
  plus `.pre-commit-config.yaml` (ruff, gitleaks), `renovate.json` (7-day minimum release
  age) and a pull request template.

## Using it

The template's [README](https://github.com/horae-dev/template-plugin#use-this-template) has
the steps: create the repository, run `scripts/rename.py` (the README also lists the manual
rename, which must include `uv lock` and re-recording), write the tools and their
permissions, regenerate `plugin.yaml`, record fixtures (`HORAE_SDK_RECORD=1 uv run pytest`,
refused in CI) and run the CI commands locally.

Commits carry Graft trailers (`git commit --trailer ...`). Work done in one repository for an
issue of another, such as this page for a template issue, writes the task as
`Graft-Task: <owner>/<repo>#<n>` (for example `horae-dev/template-plugin#2`) rather than `#<n>`.

## horae-sdk before PyPI

Until `horae-sdk` is published, the template takes it from this repository's `main` branch
in `[tool.uv.sources]` (`branch = "main"`, `subdirectory = "sdk"`), and its `uv.lock` pins the
exact commit, so CI's `uv lock --check` and every install use the same SDK until the lock
changes. Renovate's weekly lock file maintenance moves the lock to the latest `main` (a git
commit has no release date, so the 7-day age cannot apply: review that pull request like any
dependency bump). Installing from git clones this repository, so it must stay public. After
the first `Release SDK` run (`sdk-v<version>` tag), remove the git source from the template so
the requirement resolves from PyPI.

Nothing here checks the template against an SDK change yet: before changing the SDK's public
API, run the template's tests against it by hand (in a template checkout, point
`[tool.uv.sources]` at the branch with the change, `uv lock`, then `uv run pytest`). A follow-up should add a core CI job that does this on every SDK change.
