# Design note: retention workflow (to revisit)

- **Issue:** follows [#14](https://github.com/horae-dev/horae/issues/14); threat T28 in
  [docs/security.md](../security.md#b7-horae--host).
- **Status:** Open, deferred. The maintainer chose operator documentation first (PR #84):
  [operations/retention-and-backups.md](../operations/retention-and-backups.md). This note records
  what a proper workflow would decide, so it can be picked up later as a story.
- **Revisit:** before M8 (public v0.1, the docs promise of [PLAN §9](../PLAN.md#9-quality-and-security-practice)),
  or earlier if the reference instance's store or backups grow uncomfortable.

## Context

[PLAN §9](../PLAN.md#9-quality-and-security-practice) and
[ADR 0006](../adr/0006-sqlite-as-the-store.md) promise that run records keep LLM inputs **locally
only, with a configurable retention**. Today the store has the primitives (`Store.prune_runs`,
`Store.cache_purge`, `Store.cache_purge_expired`, tested in `tests/test_store.py`), but no setting
and no command call them, so retention depends on the operator.

## Questions for the workflow

1. **Setting.** A `horae.yaml` key (e.g. `retention.runs_days`, `retention.cache_days`) with a
   default, or no default (keep forever unless set)? A default is the privacy-friendly choice but
   silently ends undo for old runs.
2. **Trigger.** Prune at the end of each run, from the built-in scheduler, or only through a
   `horae prune` command called by an external scheduler (Coolify Scheduled Tasks)?
3. **Undo horizon.** Should pruning skip runs whose journal is still the latest change of a note
   (so the newest undo always works), or is "older than N days" enough?
4. **What else ages out.** Task rows (`tasks`) and notification dedupe keys (`notifications`) are
   never pruned; dropping a dedupe key can re-send a notification. Which of them get a retention?
5. **Free pages.** Deleted rows survive in free pages: `PRAGMA secure_delete`, periodic `VACUUM`
   (needs exclusive access), or `auto_vacuum`?
6. **Backups.** Should Horae ship `horae backup <path>` (online backup API, `0600`, integrity
   check) so operators do not script it, and leave encryption and offsite copies to their tools?
7. **Audit.** Should a prune be recorded (count, cutoff) so `horae runs` can say that older history
   was removed?

## Options

- **A. Documentation only** (current): no code, everything in the operator guide.
- **B. `horae prune` and `horae backup` commands**, no setting: explicit, scheduled by the
  operator.
- **C. Retention setting plus automatic pruning** by the scheduler, with B's commands for manual
  use.

## Next step

When revisited: pick an option, answer the questions above, and open a story (likely M3 or M8)
with acceptance criteria that test the retention end to end (a seeded old run is gone, its blobs
are collected, a recent run can still be undone).
