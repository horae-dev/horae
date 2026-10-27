"""Generate the initial content of every horae-dev repository under ./repos/<name>."""
import os as _os
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # paths below are relative to tools/planning

import os, shutil, subprocess, sys
from backlog import REPOS, EPICS, MILESTONES

PROJECT_URL = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/orgs/horae-dev/projects"
LOGO = "https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png"
LICENSE = subprocess.run(["gh", "api", "licenses/apache-2.0", "--jq", ".body"], capture_output=True, text=True, check=True).stdout

KIND = {
    "horae": "core", "marketplace": "index", "template-plugin": "template",
}
for k in REPOS:
    KIND.setdefault(k, "pack" if k.startswith("pack-") else "plugin")

EPICS_BY_REPO = {}
for key, repo, ms, title, summary in EPICS:
    EPICS_BY_REPO.setdefault(repo, []).append((ms, title, summary))

GITIGNORE = """__pycache__/
*.py[cod]
.venv/
.env
.env.*
!.env.example
dist/
build/
*.egg-info/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/
.pytest_cache/
state/
*.sqlite
*.sqlite-wal
*.sqlite-shm
.DS_Store
"""


def readme(name, desc):
    kind = KIND[name]
    epics = EPICS_BY_REPO.get(name, [])
    rows = "\n".join(f"| {ms} {MILESTONES[ms][0]} | **{t}** — {s} |" for ms, t, s in epics)
    extra = {
        "core": """## What Horae is

Horae is a **predictable, self-hosted personal agent**:

- **Pipelines, not agent loops**: `collect → transform → llm → act → notify`, declared in YAML.
- **One bounded LLM step**: a skill, an output schema, a model alias, a budget and **no tools**.
- **Plugins return intents; the core applies them** with a dry-run diff, a journal and `horae undo`.
- **Declared permissions**: hosts, secrets, paths and intents, approved at install and enforced at run time.
- **A git marketplace**: plugins pinned to a commit in `horae.lock`; nothing updates itself.
- **Bring your own model**: Anthropic API, your own `claude -p`, OpenAI-compatible endpoints or Ollama.

Read the [plan](docs/PLAN.md), the [roadmap](docs/ROADMAP.md) and the [estimation](docs/ESTIMATION.md).
""",
        "index": """## What this repository will hold

`marketplace.json`, the index Horae reads to install plugins by name. Every entry pins a plugin version to a **commit SHA**; CI clones each entry, checks the SHA and runs the plugin's tests against the latest core.
""",
        "template": """## What this repository will be

A GitHub template that gives you a working Horae plugin in minutes: `plugin.yaml` with declared permissions, an MCP stdio server built on `horae-sdk`, recorded-input tests, CI, `graft.yaml` and `AGENTS.md`.
""",
        "plugin": "## What this plugin will do\n\n" + desc + "\n\nIt will declare every host, secret, path and intent it uses in `plugin.yaml`, and it will only return **intents**: the Horae core validates and applies them.\n",
        "pack": "## What this pack will provide\n\n" + desc + "\n\nA pack is a ready-made pipeline (and its skill, if any) that you add to an instance with `horae plugin add`.\n",
    }[kind]
    return f"""<p align="center"><img src="{LOGO}" width="96" alt="Horae logo"></p>

# {name}

{desc}

> 🚧 **Planned.** Nothing to install yet. Follow progress on the [Horae roadmap project]({PROJECT_URL}).

{extra}
## Planned work

| Milestone | Epic |
|---|---|
{rows}

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project]({PROJECT_URL}).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
"""


def agents_md(name):
    kind = KIND[name]
    core = kind == "core"
    sens = ("`src/horae/permissions/`, `src/horae/plugins/host/`, `src/horae/sandbox/`, `src/horae/marketplace/`, "
            "`src/horae/api/act/`, `src/horae/llm/backends/`, `src/horae/secrets/`, `schemas/`, workflows, Dockerfile and `deploy/`") if core else \
           "`plugin.yaml` (permissions), anything under `auth/`, workflows"
    return f"""# AGENTS.md — how to work in `{name}`

This repository is built mostly by **AI coding agents** (Claude Code and others) supervised by a human maintainer. These rules apply to agents and humans alike. `CLAUDE.md` imports this file.

## Before you start
1. Work on **one issue at a time**. Only pick issues with the `agent-ready` label and executor `Agent` or `Agent + human check`.
   - `Human-led`: open a **draft PR with a short design note first** and wait for the maintainer before writing the implementation.
   - `Human-only`: do not pick it.
2. Read the issue's **acceptance criteria**, the linked epic, and the relevant section of the [plan](https://github.com/horae-dev/horae/blob/main/docs/PLAN.md).
3. If anything is ambiguous, **ask in the issue** instead of guessing. Unclear specs are the top cause of wasted agent work.

## Working rules
- Branch: `story/<issue-number>-<short-slug>`. One PR per issue; keep it under ~400 changed lines when possible.
- Python 3.12+, managed with **uv**. Style: ruff, mypy strict, pytest. (Commands are added by the scaffolding story; until then the repo has no code.)
- **Tests first** for behaviour: every acceptance criterion maps to at least one test.
- Never add a network host, secret, path, intent or LLM call that is not declared and justified in the PR.
- Never commit secrets, tokens, personal data or real hostnames. Fixtures must be synthetic.
- Do not change `graft.yaml`, `AGENTS.md` or CI permissions unless the issue asks for it.

## Commits and provenance (Graft)
Every commit made by an agent carries Graft trailers in its last paragraph:

```
Graft-Agent: claude-code
Graft-Model: <model id>
Graft-Task: #<issue number>
```

Set `GRAFT_AGENT`, `GRAFT_MODEL` and `GRAFT_TASK` and use Graft's `prepare-commit-msg` hook, or add the trailers yourself (`git commit --trailer ...`).

## Review
- Graft posts an intent card and applies [`graft.yaml`](graft.yaml).
- Trivial and small low-risk changes can be approved without a human.
- Changes touching sensitive paths ({sens}), large changes and anything Graft rates `sensitive` need the maintainer.
- Address review comments in new commits; don't force-push over reviewed commits.

## Definition of done
- [ ] All acceptance criteria met and checked in the PR description
- [ ] CI green (lint, types, tests, security scans)
- [ ] Docs and examples updated if behaviour changed
- [ ] No new undeclared permissions, secrets or LLM calls
- [ ] On the issue, record **actuals**: agent attempts, wall-clock time, whether a human took over. These feed the [estimation](https://github.com/horae-dev/horae/blob/main/docs/ESTIMATION.md) recalibration.
"""


def graft_yaml(name):
    core = KIND[name] == "core"
    if core:
        sens = ["src/horae/permissions/**", "src/horae/plugins/host/**", "src/horae/sandbox/**",
                "src/horae/marketplace/**", "src/horae/api/act/**", "src/horae/llm/backends/**",
                "src/horae/secrets/**", "schemas/**", ".github/workflows/**", "Dockerfile", "deploy/**"]
        small = 150
    else:
        sens = ["plugin.yaml", "**/auth/**", ".github/workflows/**", "Dockerfile"]
        small = 300
    sp = "\n".join(f'  - "{s}"' for s in sens)
    return f"""# yaml-language-server: $schema=https://raw.githubusercontent.com/mmornati/graft/main/schema/graft.v1.schema.json
# Review policy for agent-written pull requests (https://github.com/mmornati/graft).
# Agents review and approve low-risk changes; the maintainer is pulled in only when needed.
version: 1

# Paths that always raise the risk to `sensitive`.
sensitive_paths:
{sp}

# When nothing else matches: one human.
default:
  require: {{ humans: 1 }}

rules:
  - name: docs-only
    match: {{ risk: trivial, only_paths: ["docs/**", "*.md"] }}
    approve: auto

  - name: trivial
    match: {{ risk: trivial }}
    approve: auto

  - name: small-logic
    match: {{ risk: logic, size: {{ max_lines: {small} }} }}
    approve: auto

  - name: sensitive
    match: {{ risk: sensitive }}
    require: {{ humans: 1 }}

  - name: big-change
    match: {{ size: {{ min_lines: 800 }} }}
    require: {{ humans: 1 }}

report:
  language: en
"""


def main():
    base = "repos"
    shutil.rmtree(base, ignore_errors=True)
    for name, desc in REPOS.items():
        d = os.path.join(base, name)
        os.makedirs(d)
        open(f"{d}/README.md", "w").write(readme(name, desc))
        open(f"{d}/AGENTS.md", "w").write(agents_md(name))
        open(f"{d}/CLAUDE.md", "w").write("@AGENTS.md\n")
        open(f"{d}/graft.yaml", "w").write(graft_yaml(name))
        open(f"{d}/LICENSE", "w").write(LICENSE)
        open(f"{d}/.gitignore", "w").write(GITIGNORE)
        if name == "horae":
            os.makedirs(f"{d}/docs")
            shutil.copy("../../docs/PLAN.md", f"{d}/docs/PLAN.md")
            shutil.copy("../../docs/ESTIMATION.md", f"{d}/docs/ESTIMATION.md")
            shutil.copy("../../docs/ROADMAP.md", f"{d}/docs/ROADMAP.md")
    print("generated", len(REPOS))


if __name__ == "__main__":
    main()
