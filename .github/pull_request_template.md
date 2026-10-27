Closes #<issue>

## Summary

<!-- What changed and why, in a few lines. -->

## Acceptance criteria

<!-- Copy them from the issue; each maps to at least one test. -->
- [ ]
- [ ]

## Definition of done (see AGENTS.md)

- [ ] All acceptance criteria met and checked above
- [ ] CI green: the **CI OK** check (lint, types, tests, security scans)
- [ ] `uv run pre-commit run --all-files` passes locally
- [ ] Commits carry `Graft-Agent`, `Graft-Model` and `Graft-Task` trailers
- [ ] Docs and examples updated if behaviour changed
- [ ] No new undeclared permissions, secrets or LLM calls
- [ ] Actuals recorded on the issue (attempts, wall-clock, human takeover)

## Provenance

- Agent: <!-- e.g. claude-code, or "human" -->
- Model: <!-- e.g. claude-sonnet-5-5 -->

## Declared additions

<!-- Write "none" or list each with a justification. -->
- Network hosts:
- Secrets:
- Paths:
- Intents:
- LLM calls:
