# ADR 0005: The `claude-cli` backend and its terms

- **Status:** Accepted
- **Date:** 2026-10-27 (proposed)
- **Accepted:** 2026-10-28

## Context

The LLM step ([ADR 0001](0001-pipelines-not-agent-loops.md)) needs a backend. Users with a Claude
subscription may want Horae to use it. PLAN.md records that, for Hermes and OpenClaw, the Claude
subscription has been "blocked since April 2026" ([§1](../PLAN.md#1-why-horae-the-gap)). PLAN's
understanding, marked **⚠️ unverified**, is that the official `claude -p` CLI remains available
for personal headless use on your own plan
([§3.4](../PLAN.md#34-llm-step-and-backends)). This ADR records how Horae approaches it; it is
**not a legal assessment**.

## Decision

Horae calls LLMs through backend adapters behind model aliases, including a `claude-cli` backend
that runs the user's own Claude Code, documented as personal use under the user's plan's terms,
with `anthropic` and an API key as the documented default.

1. **Backends are adapters behind model aliases.** A pipeline names a model alias
   (`model: default`), resolved in `horae.yaml`. Four backends are planned:
   - **`claude-cli`**: runs the official `claude -p --output-format json`;
   - **`anthropic`** (API key);
   - **`openai-compatible`** (OpenRouter, Mistral, vLLM);
   - **`ollama`**.
2. **`claude-cli` runs isolated:** an empty, isolated config, an environment allowlist, built-in
   tools disabled and no user settings. The LLM step has no tools on any backend.
3. **Horae never handles OAuth itself.** The `claude-cli` backend runs the user's own installed and
   logged-in Claude Code, or uses a `CLAUDE_CODE_OAUTH_TOKEN` the user created with
   `claude setup-token`.
4. **It is documented as "personal use under your plan's terms".**
5. **The default backend in the published docs is `anthropic` with an API key.** PLAN.md notes that
   Max plans include monthly API credits that can fund it (⚠️ unverified,
   [§3.4](../PLAN.md#34-llm-step-and-backends)).
6. **Re-check the policy before v0.1.**

Every backend gets the same guarantees from the core: schema-validated output with one repair
retry, a cache keyed on (skill version, model, normalized input), a budget per step and per day,
and a usage ledger. A de-identification transform can pseudonymize declared fields before any cloud
backend.

In the contracts, the backend is recorded per call in the run record
([`schemas/run-record/v1.json`](../../schemas/run-record/v1.json), `llm[].backend`, one of
`claude-cli`, `anthropic`, `openai-compatible`, `ollama`) together with the concrete `model` the
alias resolved to, `skill_version`, `cached`, `attempt` and `usage`. The pipeline contract
([`schemas/pipeline/v1.json`](../../schemas/pipeline/v1.json)) only ever names an alias.

## Consequences

### Positive

- If PLAN.md's ⚠️ reading of the terms holds, users with a subscription can use it for personal
  instances without a separate API bill.
- Pipelines do not depend on a provider; swapping a backend is a `horae.yaml` change.
- Because `claude-cli` is one adapter, it can be dropped if the terms change, without touching
  pipelines ([§10](../PLAN.md#10-risks-honest)).

### Negative

- The backend depends on terms Horae does not control. The understanding in PLAN.md (personal
  headless use of `claude -p` on your own plan is allowed; third-party products are not allowed to
  offer claude.ai login) is **unverified (⚠️)** and may change. Horae makes no claim that a given
  use is
  compliant; users are responsible for their plan's terms.
- Running a CLI as a subprocess is slower and less controllable than an API call, and its JSON
  output format and flags can change between Claude Code releases.
- The isolation (env allowlist, tools disabled, no user settings) must be verified against each
  Claude Code version; a regression could expose user configuration or tools.

### Open questions

- ⚠️ The terms-of-service position must be re-checked against Anthropic's current policy before
  v0.1 (tracked as a Burning point in [ROADMAP.md](../ROADMAP.md#burning-points) and as [risk
  #70, Claude terms for the claude-cli backend](https://github.com/horae-dev/horae/issues/70)).
- How `CLAUDE_CODE_OAUTH_TOKEN` is stored and injected (secrets handling) is for the M2 backend
  story ([#29, claude-cli backend with
  isolation](https://github.com/horae-dev/horae/issues/29)).
- `llm[].backend` is a closed list in run-record v1; adding a backend is a schema change.

## Alternatives considered

- **Horae handles claude.ai login (OAuth) itself.** Rejected: PLAN.md's understanding (⚠️
  unverified,
  [§3.4](../PLAN.md#34-llm-step-and-backends)) is that Anthropic does not allow third-party
  products to offer claude.ai login.
- **No subscription backend, API keys only.** Safest with respect to terms, but excludes a use
  PLAN.md wants to support for personal instances. API keys remain the documented default.
- **A local model only.** A small home server cannot run a useful local model
  ([§7](../PLAN.md#7-reference-deployment-coolify)); `ollama` stays available for machines that can.

## References

- [PLAN.md §1](../PLAN.md#1-why-horae-the-gap), [§3.4](../PLAN.md#34-llm-step-and-backends),
  [§7](../PLAN.md#7-reference-deployment-coolify), [§10](../PLAN.md#10-risks-honest),
  [M2](../PLAN.md#m2--the-llm-step-45-days)
- [ROADMAP.md: Burning points](../ROADMAP.md#burning-points)
- Issues: [#29, claude-cli backend with isolation](https://github.com/horae-dev/horae/issues/29);
  [#70, Claude terms for the claude-cli backend](https://github.com/horae-dev/horae/issues/70)
- [`schemas/run-record/v1.json`](../../schemas/run-record/v1.json),
  [`schemas/pipeline/v1.json`](../../schemas/pipeline/v1.json)
