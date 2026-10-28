# ADR 0006: SQLite as the store

- **Status:** Accepted
- **Date:** 2026-10-27 (proposed)
- **Accepted:** 2026-10-28

## Context

Horae must remember runs (inputs, step outputs, intents, applied changes, cost and status), keep a
journal for undo, cache LLM responses for the $0 path and exact replays, and track tasks and
notification state ([§3.1](../PLAN.md#31-concepts)). Its tenets ask for reproducibility and
**boring operations**: "One container, SQLite, a CLI, `/healthz`, and push monitors for missed
runs" ([§2.1](../PLAN.md#21-design-tenets) tenets 6–7). The reference deployment is a small home
server running Coolify, with `horae` as a Docker Compose resource that uses the Horae image plus
`plugin-livesync` ([§7](../PLAN.md#7-reference-deployment-coolify)).

## Decision

Horae stores all run state in one SQLite database in the instance state directory; there is no
database server.

1. **The store is SQLite**, in the instance's state directory ([§3.1](../PLAN.md#31-concepts)).
   It holds:
   - **runs**: one run record per execution;
   - **the journal**: per-run record of applied changes, enough to undo them;
   - **the LLM cache**, keyed on (skill version, model, normalized input);
   - **the task index** and **notification state**.
2. **No database server.** This follows the "One container, SQLite" tenet
   ([§2.1](../PLAN.md#21-design-tenets) tenet 7); plugins run as subprocesses until M5.
3. **Backups** of the state directory (with the vault) are taken with SQLite's online backup API or
   with the container stopped, with an offsite copy
   ([§7](../PLAN.md#7-reference-deployment-coolify)).
4. **Privacy:** run records keep LLM inputs locally only, with a configurable retention
   ([§9](../PLAN.md#9-quality-and-security-practice)).

The stored documents have contracts: the run record
([`schemas/run-record/v1.json`](../../schemas/run-record/v1.json), generated from
[`src/horae/contracts/run.py`](../../src/horae/contracts/run.py)) carries a `schema_version`, so
rows written by one version can be read and migrated by the next. It records the inputs snapshot,
per-step records, LLM calls (request, response, `cached`), proposed intents, applied changes,
violations, cost and status (`ok`, `noop`, `degraded`, `failed`, `undone`), plus digests of the
pipeline and, optionally, the lockfile and config. The models validate both JSON text and
already-parsed data such as SQLite rows ([schemas
README](../../schemas/README.md#rules-worth-knowing)).
The table layout and the journal's undo format are not contracts yet; they belong to the M1 store
story ([M1](../PLAN.md#m1--walking-skeleton-1-week)).

## Consequences

### Positive

- Zero operations: a file, no server, no credentials, no network; works on plain Docker and on
  Coolify.
- Transactions keep the run record, journal and cache consistent. File changes in the vault are
  outside the transaction, so the engine still has to journal before it applies.
- A backup is a consistent snapshot of one database file (plus the vault).
- Python ships `sqlite3`, so no extra dependency is needed.

### Negative

- A single writer: concurrent runs (`daily-brief` and `remind` every 5 minutes, plus `/act`
  requests) serialize on writes and must handle busy timeouts.
- The database file must live on local storage, not a network filesystem, and be backed up
  consistently (online backup API or a stopped container), not by copying a live file.
- No multi-host scaling, which is acceptable because multi-user and SaaS hosting are non-goals
  ([§2.3](../PLAN.md#23-non-goals-v0x)).
- Recorded inputs and LLM requests contain personal data at rest; retention and the threat model
  (secret leaks into logs, [§9](../PLAN.md#9-quality-and-security-practice)) must cover the store.

### Open questions

- Schema migrations, retention defaults and the journal format for undo are for M1.
- Whether `undo` of a run sets the stored record's status to `undone` (a status the v1 contract
  adds beyond PLAN's `ok`, `noop`, `degraded`, `failed`) or writes a new record is for M1.
- The store, journal and run statuses are tracked as
  [#18, SQLite store with migrations](https://github.com/horae-dev/horae/issues/18),
  [#22, Journal and undo](https://github.com/horae-dev/horae/issues/22) and
  [#19, Run lifecycle, locking and statuses](https://github.com/horae-dev/horae/issues/19).

## Alternatives considered

- **PostgreSQL.** Concurrent writers and richer queries, but a second service to run, secure and
  back up, against the "one container" tenet.
- **Plain files (JSON per run).** Simple and diffable, but no transactions, so journal-then-apply
  and the cache would need their own locking.
- **An embedded key-value store.** Fast, but no SQL for `horae runs`, `audit` or the task index, and
  an extra dependency.

## References

- Issues: [#18](https://github.com/horae-dev/horae/issues/18),
  [#22](https://github.com/horae-dev/horae/issues/22),
  [#19](https://github.com/horae-dev/horae/issues/19)
- [PLAN.md §2.1](../PLAN.md#21-design-tenets), [§3.1](../PLAN.md#31-concepts),
  [§3.6](../PLAN.md#36-stack), [§7](../PLAN.md#7-reference-deployment-coolify),
  [§9](../PLAN.md#9-quality-and-security-practice), [M1](../PLAN.md#m1--walking-skeleton-1-week)
- [`schemas/run-record/v1.json`](../../schemas/run-record/v1.json),
  [`schemas/examples/run-record/valid/ok.json`](../../schemas/examples/run-record/valid/ok.json)
