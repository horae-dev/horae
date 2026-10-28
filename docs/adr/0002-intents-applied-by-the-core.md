# ADR 0002: Plugins return intents; the core applies them

- **Status:** Accepted
- **Date:** 2026-10-27 (proposed)
- **Accepted:** 2026-10-28

## Context

Horae reads untrusted content (mail, notes, web pages) and writes to things the user cares about
(the vault, notifications, remote services). If plugins or the LLM could write directly, a buggy or
compromised plugin, or a prompt injection, could change anything, and there would be no reliable
dry run, journal or undo.

PLAN.md makes three tenets of this ([§2.1](../PLAN.md#21-design-tenets) tenets 3–5) and three
of its guarantees ([§2.2](../PLAN.md#22-measurable-guarantees-tested-in-ci)): `--dry-run` writes
nothing, schema-invalid LLM output is never applied, and `horae undo <run-id>` restores every file
the run touched.

## Decision

Plugins and the LLM never write; they return intents, and only the core, through a sink, applies
them.

1. **Plugins never write directly.** Sources, transforms and the LLM step return **intents**: typed
   requests for a change. Only a sink applies an intent, and only after the core has validated it,
   checked it against the proposing plugin's declared `permissions.intents`, shown a dry-run diff
   and journalled it ([§3.3](../PLAN.md#33-plugin-manifest-and-permissions)).
2. **Intents are a closed set with schemas:** `note.create`, `note.append_section`,
   `note.replace_owned_block`, `task.create`, `task.update_status`, `notify.push`, `http.post`
   (`http.post` only to declared hosts).
3. **Untrusted content stays data.** It can fill schema fields, but it can never choose an action,
   a recipient or a URL that was not already in the input set
   ([§2.1](../PLAN.md#21-design-tenets) tenet 5).
4. **Every applied change is journalled per run**, which is what makes undo possible
   ([§3.1](../PLAN.md#31-concepts)).

The contracts:

- [`schemas/intent/v1.json`](../../schemas/intent/v1.json): one intent, `oneOf` tagged by `type`;
  one file per intent in [`schemas/intents/`](../../schemas/intents/) (e.g.
  [`intents/http.post/v1.json`](../../schemas/intents/http.post/v1.json)), generated from
  [`src/horae/contracts/intents.py`](../../src/horae/contracts/intents.py).
- `permissions.intents` in
  [`schemas/plugin-manifest/v1.json`](../../schemas/plugin-manifest/v1.json);
  empty means the plugin may neither propose nor apply writes.
- The run record ([`schemas/run-record/v1.json`](../../schemas/run-record/v1.json)) keeps
  `intents[]` (each with `step` and `proposed_by`) and `changes[]` (applied changes, each an index
  into `intents`). A dry run has no `changes`.

The contracts constrain shape (safe note paths, `https`-only URLs, `https` or `obsidian`
notification links, one-line titles, size limits); the runtime still checks meaning, e.g. the
`http.post` host against `permissions.network`
([schemas README](../../schemas/README.md#strings-that-reach-the-outside-world)).

## Consequences

### Positive

- One choke point for all writes: validation, permission checks, dry run, journal and undo are
  implemented once in the core, not in every plugin.
- `--dry-run` is trustworthy because there is no other write path.
- Through the core, a compromised plugin can at most propose the intents it declared (checked from
  M1, approved at install from M4). Until M5 it can still reach undeclared hosts or paths directly
  ([ADR 0003](0003-mcp-as-the-plugin-protocol.md)).

### Negative

- A new kind of write needs a new intent type and a schema change in the core; plugins cannot
  extend the set.
- Sinks must be written so their effects can be diffed and undone, which rules out some
  integrations (and costly targets are read-only by policy, [§2.3](../PLAN.md#23-non-goals-v0x)).
- `notify.push` and `http.post` reach the outside world and cannot be undone by the journal;
  `undo` restores files only.

### Open questions

- §2.1 names intents `create_task`, `append_note`, `notify`; §3.3 lists the closed set above. The
  v1 contract uses the §3.3 names ([schemas decision
  1](../../schemas/README.md#decisions)).
- `permissions.intents` covers both "may propose" and "may apply" (sinks); whether these should be
  two lists is open (decision 12).
- Untrusted-content tagging is not part of the v1 run record (decision 21); how the core tracks
  provenance so that untrusted data cannot pick a URL or recipient is left to the M2 guards
  ([#35, Untrusted-content tagging and action
  guard](https://github.com/horae-dev/horae/issues/35)).

## Alternatives considered

- **Plugins write directly, with permissions.** Simpler plugins, but no global dry run or journal,
  and enforcement would rely on every plugin being correct.
- **LLM tool calls perform writes.** Rejected by [ADR 0001](0001-pipelines-not-agent-loops.md).
- **An open, plugin-defined intent vocabulary.** Extensible, but the core could not validate,
  diff or journal intents it does not understand.

## References

- [PLAN.md §2.1](../PLAN.md#21-design-tenets),
  [§2.2](../PLAN.md#22-measurable-guarantees-tested-in-ci),
  [§3.1](../PLAN.md#31-concepts), [§3.3](../PLAN.md#33-plugin-manifest-and-permissions)
- [`schemas/intent/v1.json`](../../schemas/intent/v1.json),
  [`schemas/intents/`](../../schemas/intents/),
  [`schemas/run-record/v1.json`](../../schemas/run-record/v1.json)
- [`schemas/README.md`](../../schemas/README.md#decisions)
  (decisions 1, 2, 4–7, 12, 21)

## Amendments

The accepted text above is unchanged; these dated additions record what later work settled.

### Amendment (2026-10-28, #17)

On the open question of untrusted-content tagging: until the M2 guards, the engine's routing
([schemas decision 30](../../schemas/README.md#decisions)) binds every target to a trusted
pipeline parameter: a note `path` or `http.post` `url` to the entry's, a notification `link` to
the entry's `link_prefix`, and a task status change to a fingerprint the run collected. Titles
and bodies stay untrusted text that appliers treat as data.

### Amendment (2026-10-28, #35)

#35 adds no per-value tags: the action guard ([schemas decision
46](../../schemas/README.md#decisions)) checks the LLM's text instead, so every URL in it is an
exact string of the viewed input, no embed or script link is ever proposed, and a skill's
per-type limits hold; a violation degrades the whole step. Plugin proposals are left to #52.
