# Design note: de-identification transform

- **Issue:** [#34](https://github.com/horae-dev/horae/issues/34) (M2, Human-led), epic
  [#3 Bounded LLM step](https://github.com/horae-dev/horae/issues/3); threat **T10** in
  [docs/security.md](../security.md#b3-core--llm-backend) (and its §10 addition).
- **Status:** Draft, for maintainer review. No code until the questions below are answered.
- **Sources:** [PLAN §2.1](../PLAN.md#21-design-tenets) tenet 6,
  [§3.4](../PLAN.md#34-llm-step-and-backends) ("De-identification hook"),
  [§9](../PLAN.md#9-quality-and-security-practice); [ADR 0005](../adr/0005-claude-cli-backend-and-its-terms.md);
  schemas decisions 21, 30, 33, 35; `src/horae/engine/executor.py`, `src/horae/secrets/`,
  `src/horae/plugins/host/redact.py`.

## Goals and non-goals

Goals (issue AC plus the #14 addition):

1. Declared fields never reach a **cloud** backend in clear (tested by inspecting the request).
2. The mapping is stable within a run and stored locally only.
3. Rendered output (the LLM output and the intents built from it) uses real values again.
4. **T10:** a request to a cloud backend that contains the value of any resolved secret is
   refused before it is sent; the step is `degraded`; the value is never logged.

Non-goals: general PII detection (NER, regexes for every kind of personal data); de-identifying
data sent to plugins; encrypting the store (T28, retention note 0014a); protecting against a
skill prompt that itself contains personal data (the skill author's choice).

## Current state (on `main`)

- The LLM step builds `LlmRequest(skill, model, input=_json_copy(run.data), …)` and records
  `llm[].request` (the backend's own shape, else `{skill, model, input, attempt, repair}`),
  `llm[].response`, `input_hash` of the attempt's input, and the step output
  (`horae.engine.executor._llm_step`). Nothing transforms the input on the way.
- The **cache key** is the SHA-256 of `{skill, plugin, model, input}` on the real input.
- Plugin data reaching the LLM is already redacted by the plugin host: every resolved secret,
  in its encodings, becomes `[REDACTED:<NAME>]` (`horae.plugins.host.redact.Redactor`, decision 35).
  So the T10 check is defence in depth for values arriving another way (skill prompt, `--var`,
  short secrets whose encodings are not matched).
- `SecretResolver.resolved()` lists every secret resolved so far (`horae.secrets`).
- The skill format and its **input view** are **#27** (no PR open at the time of writing): a
  skill declares which fields of the step input the model sees.
- Backends are #28–#30; `ollama` and (when local) `openai-compatible` are not cloud.

## Proposed design

**Order of the LLM input pipeline** (one place, `horae.llm.prepare`, run for every attempt):

```
step input -> input view (#27, drop undeclared fields) -> de-identify (this story)
  -> fence untrusted text (#35) -> render request -> secret check (T10) -> backend
backend output -> schema validation (#31) -> re-identify -> intents (validate, route)
```

**What is declared.** The skill's input view (#27) annotates fields with a class:
`person`, `amount`, `email`, `phone`, `place`, `free_text`. Example:

```yaml
input_view:
  events[].title: { class: free_text }
  events[].attendees[]: { class: person }
  budget.amount: { class: amount, buckets: [0, 50, 200, 1000] }
```

Plus an **instance vocabulary** of terms to replace anywhere in `free_text` fields (children's
names, the household's address): free text ("Dentist for Alice") is where names really are. The
vocabulary is personal data, so it lives in a file outside the instance repo (a `secrets.dir`
file, question 2), never in `horae.yaml`.

**Transforms per class.**

| Class | Sent to the backend | Reversible |
|---|---|---|
| `person`, `email`, `phone`, `place` | a token `<class>:<letter>` (`person:A`), PLAN's `kid:A` style | yes |
| vocabulary term in `free_text` | the term's token, matched whole-word, case-insensitive | yes |
| `amount` | its bucket label (`50-200`) | no |
| `free_text` without terms | unchanged | n/a |

**Stable within a run.** Tokens are assigned in order of first occurrence in the canonical JSON
of the viewed input, per class: a pure function of the input. The same input gives the same
pseudonymized request, so a replay recomputes it from the recorded inputs, and the request (and
its `input_hash`) is reproducible. Cross-run stability (Alice is always `person:A`) is not
promised (question 3).

**Stored locally only.** The mapping is kept in a new store table `deid_map(run_id, token,
value)`, pruned with the run (`Store.prune_runs`), never logged, never in `llm[].request`. The
run record already holds the real inputs (`inputs[]`), so this adds no new exposure; it makes
re-identification auditable. `llm[].request` and `llm[].response` record the **pseudonymized**
bytes, which are what the provider saw.

**Re-identification.** After schema validation, every string leaf of the output and of the raw
intents is scanned for known tokens of this run and they are replaced by their values. A token
the run never issued (`person:Z`) is left as is and noted (question 4). Bucketed amounts stay
buckets. Re-identification runs before intents are validated and routed, so a note path or a
task fingerprint is checked on real values; `task.update_status` fingerprints are never
de-identified (they are not personal data and must match collected input).

**Which backends.** De-identification and the secret check apply when the alias is **cloud**:
`anthropic`, `claude-cli`, and `openai-compatible` unless the alias says `local: true`; `ollama`
is local unless the alias says otherwise (question 1, a `ModelAlias` field: `schemas/` change).

**Secret check (T10).** On the **rendered request** (the exact payload the backend will send:
system prompt, messages, schema), match every value of `SecretResolver.resolved()` with the
`Redactor` patterns (all encodings for secrets of 8 bytes and more, bounded raw value for shorter
ones). A match refuses the attempt before any network I/O: step `degraded`, error kind
`secret_in_request`, a violation naming the secret (never the value). This needs the backend
protocol (#28) to expose a render step separate from the send (question 5). The `Redactor`
moves to `horae.secrets.redact` so the LLM layer does not import the plugin host (shared with #53).

## Alternatives

- **A pipeline `transform` step (a plugin).** PLAN calls it "a transform", but a plugin would
  see and hold the mapping, and the core could not guarantee the order relative to the input view
  and the backend. A core stage keyed on the skill's declaration is enforceable; rejected.
- **Pseudonyms by keyed HMAC** (stable across runs). Better LLM consistency and cache behaviour,
  but needs a key to manage and leaks equality across runs; kept as a later option.
- **Regex/NER detection of PII.** Unbounded false positives and negatives; declared fields and a
  vocabulary are testable.

## Test plan

- A fake cloud backend captures the rendered request: no declared field value nor vocabulary
  term appears in clear (also checked in their JSON-escaped forms); the `ollama` path is unchanged.
- Stability: the same value gets the same token within a run; two runs on the same input produce
  byte-identical requests; replay reproduces them.
- Mapping: present in `deid_map` only, absent from the run record JSON, the logs and
  `llm[].request`; pruned with the run.
- Re-identification: output strings and intent fields contain real values; unknown tokens left
  and noted; amounts stay bucketed; routing and fingerprint checks see real values.
- T10: a secret (long and short) planted in the skill prompt or a `--var` refuses the request
  before the backend is called (the fake records no call), status `degraded`, kind
  `secret_in_request`, value absent from record and logs.

## Risks

- The LLM rewrites a token (`Person A`), so re-identification misses it: the output shows the
  pseudonym, never a wrong real name. Prompts must tell the model to copy tokens verbatim.
- Vocabulary matching in free text over-matches (a child named "May") or under-matches
  (nicknames, typos): documented limit.
- Quoted or derived personal data in fields the skill did not declare still reaches the cloud;
  the input view (#27) is the primary control.

## Questions for the maintainer

1. **Cloud or local.** Classify by backend with an explicit `local: true|false` on the alias
   (default: `ollama` local, the rest cloud)? *Recommendation: yes; never infer from `base_url`.*
2. **Where declarations live.** Field classes in the skill's input view (#27) plus an instance
   vocabulary file in `secrets.dir`? *Recommendation: yes; the vocabulary is personal data and stays
   out of the instance repo.*
3. **Stability scope.** Per run, deterministic from the input (no key), or per instance via HMAC?
   *Recommendation: per run in v1.*
4. **Unknown or mangled tokens in the output.** Leave and note, or treat as a schema error and use
   the repair retry? *Recommendation: leave and note; a retry cannot fix what the model invented.*
5. **Render-then-send in the backend protocol.** Ask #28 for `render(request) -> payload` and
   `send(payload)`, so the core checks the exact bytes? *Recommendation: yes, before #28 lands.*
6. **Violation kind.** Add `secret_in_request` to `violations[].kind` (run-record v1, a
   compatible addition), or reuse `guard`? *Recommendation: add `secret_in_request`.*
7. **Default.** De-identification on by default for cloud aliases whenever the skill declares
   classes, with an opt-out per pipeline? *Recommendation: on, no opt-out in v1.*
