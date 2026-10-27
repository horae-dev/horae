# AGENTS.md — how to work in `horae`

This repository is built mostly by **AI coding agents** (Claude Code and others) supervised by a human maintainer. These rules apply to agents and humans alike. `CLAUDE.md` imports this file.

## Before you start
1. Work on **one issue at a time**. Only pick issues with the `agent-ready` label and executor `Agent` or `Agent + human check`.
   - `Human-led`: open a **draft PR with a short design note first** and wait for the maintainer before writing the implementation.
   - `Human-only`: do not pick it.
2. Read the issue's **acceptance criteria**, the linked epic, and the relevant section of the [plan](https://github.com/horae-dev/horae/blob/main/docs/PLAN.md).
3. If anything is ambiguous, **ask in the issue** instead of guessing. Unclear specs are the top cause of wasted agent work.

## Working rules
- Branch: `story/<issue-number>-<short-slug>`. One PR per issue; keep it under ~400 changed lines when possible.
- Python 3.12+, managed with **uv**. Style: ruff, mypy strict, pytest. Commands: `uv sync`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`, `uv run pre-commit run --all-files`.
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
- Changes touching sensitive paths (`src/horae/permissions/`, `src/horae/plugins/host/`, `src/horae/sandbox/`, `src/horae/marketplace/`, `src/horae/api/act/`, `src/horae/llm/backends/`, `src/horae/secrets/`, `schemas/`, workflows, Dockerfile and `deploy/`), large changes and anything Graft rates `sensitive` need the maintainer.
- Address review comments in new commits; don't force-push over reviewed commits.

## Definition of done
- [ ] All acceptance criteria met and checked in the PR description
- [ ] CI green (lint, types, tests, security scans)
- [ ] Docs and examples updated if behaviour changed
- [ ] No new undeclared permissions, secrets or LLM calls
- [ ] On the issue, record **actuals**: agent attempts, wall-clock time, whether a human took over. These feed the [estimation](https://github.com/horae-dev/horae/blob/main/docs/ESTIMATION.md) recalibration.
