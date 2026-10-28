# ADR 0001: Pipelines, not agent loops

- **Status:** Accepted
- **Date:** 2026-10-27 (proposed)
- **Accepted:** 2026-10-28

## Context

Horae's purpose is an assistant you can "trust the way I trust a cron job"
([PLAN.md §1](../PLAN.md#1-why-horae-the-gap)). In PLAN's comparison
([§1](../PLAN.md#1-why-horae-the-gap)), the agents it is an alternative to (Hermes, OpenClaw) have
the LLM decide the actions as a planner with tools in a loop, every run is a conversation, and
testing is hard.

Horae's users need the opposite properties, listed as tenets in
[§2.1](../PLAN.md#21-design-tenets) and as CI-tested guarantees in
[§2.2](../PLAN.md#22-measurable-guarantees-tested-in-ci): no LLM call when inputs did not change,
a dry run that writes nothing, and schema-invalid output never applied. Tenet 6 adds exact
replays.

## Decision

Horae runs declared pipelines: the core owns the control flow, and an LLM is only a bounded,
tool-less function inside one step.

1. **Control flow is declared, never decided by an LLM.** A pipeline is a YAML document with a
   trigger (optional in the v1 contract, [schemas decision
   13](../../schemas/README.md#decisions-provisional-maintainer-to-confirm); PLAN says "a trigger,
   then steps") and an ordered list of steps
   ([§3.1](../PLAN.md#31-concepts), [§3.2](../PLAN.md#32-a-pipeline-example-the-morning-note)).
   Step kinds are `collect`, `transform`, `llm`, `act` and `notify`, run in order. Each non-`llm`
   step lists explicit plugin calls; the core calls them, there is no tool selection by a model.
2. **One bounded LLM step.** The `llm` step is a typed function: a skill (prompt and output JSON
   Schema), a model alias, a budget (`usd` and `tokens_out`), a timeout, and **no tools**.
3. **Unchanged input costs nothing.** `on_unchanged_input` defaults to `skip`, which makes no LLM
   call when the input hash is the same as the last run (the $0 path).
4. **LLM output is data.** It is validated against the skill's schema, with one repair retry; output
   that still fails is never applied and the run is marked `degraded`.

The contract encodes this in [`schemas/pipeline/v1.json`](../../schemas/pipeline/v1.json)
(generated from [`src/horae/contracts/pipeline.py`](../../src/horae/contracts/pipeline.py)):

- a step is an object with exactly one key, the step kind;
- a pipeline has **at most one** `llm` step (`contains` + `maxContains: 1`);
- the `llm` step has no `tools` key (unknown keys are rejected) and requires both budget fields;
- `on_unchanged_input` accepts only `skip`;
- `timeout_s` is optional, with a core default.

The run record ([`schemas/run-record/v1.json`](../../schemas/run-record/v1.json)) has one
`steps[]` entry per declared step, which makes the declared flow auditable after the fact.

## Consequences

### Positive

- Runs are predictable and testable: the same pipeline and inputs give the same sequence of calls,
  and replay uses recorded inputs and cached LLM responses
  ([§2.1](../PLAN.md#21-design-tenets) tenet 6).
- Cost is bounded per step and is $0 on unchanged input.
- Prompt injection in untrusted content cannot add or reorder actions, because the model has no
  tools and does not choose steps (see [ADR 0002](0002-intents-applied-by-the-core.md)).

### Negative

- Horae is not a general chat or autonomous agent; that is an explicit non-goal
  ([§2.3](../PLAN.md#23-non-goals-v0x)). Tasks that need open-ended exploration do not fit.
- A new behaviour needs a new or edited pipeline, not a new prompt.
- One LLM step per pipeline limits multi-stage reasoning to what one skill can do.

### Open questions

- PLAN.md says one LLM step "by default"; the v1 contract enforces at most one. A later opt-out
  would be an explicit key ([schemas decision
  14](../../schemas/README.md#decisions-provisional-maintainer-to-confirm)).
- The per-day budget and the usage ledger ([§3.4](../PLAN.md#34-llm-step-and-backends)) are not in
  the pipeline contract; they belong to instance configuration, which has no schema yet.
- The canonical form of the pipeline for `pipeline_digest` (e.g. RFC 8785) is for the engine to fix.

## Alternatives considered

- **LLM planner with tools (Hermes, OpenClaw).** In PLAN's comparison
  ([§1](../PLAN.md#1-why-horae-the-gap)): flexible, but the LLM decides the actions, every run is a
  conversation and testing is hard; it contradicts the core promise.
- **A general workflow engine (n8n, Windmill, Kestra).** Per PLAN's comparison
  ([§1](../PLAN.md#1-why-horae-the-gap)): community nodes for plugins and partial undo, and no
  project combines a fixed pipeline, bounded LLM steps, enforced permissions and a git marketplace.
- **A graph library (LangGraph, PydanticAI).** Control flow is your code (a graph); Horae wants the
  flow declared in data, so the core can validate, dry-run and replay it.

## References

- [PLAN.md §1](../PLAN.md#1-why-horae-the-gap), [§2.1](../PLAN.md#21-design-tenets),
  [§2.2](../PLAN.md#22-measurable-guarantees-tested-in-ci),
  [§3.1](../PLAN.md#31-concepts), [§3.2](../PLAN.md#32-a-pipeline-example-the-morning-note)
- [`schemas/pipeline/v1.json`](../../schemas/pipeline/v1.json),
  [`schemas/examples/pipeline/valid/daily-brief.yaml`](../../schemas/examples/pipeline/valid/daily-brief.yaml)
- [`schemas/README.md`](../../schemas/README.md)
