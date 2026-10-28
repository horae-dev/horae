# Retention and backups (interim operator guidance)

- **Status:** interim. Horae has no retention setting or backup command yet; this page says how to
  do both by hand until the workflow of [design note 0014a](../design/0014a-retention-workflow.md)
  is decided.
- **Why:** threat T28 in [docs/security.md](../security.md#b7-horae--host): the store and its
  backups hold LLM inputs, note contents and task data for as long as nobody deletes them.

## What the store keeps

The store is one SQLite database, `<state_dir>/horae.db` (default `state/horae.db` in the instance
directory, [ADR 0006](../adr/0006-sqlite-as-the-store.md)). Horae creates the state directory
`0700` and the database, `-wal` and `-shm` files `0600`. It holds:

| Table | Personal data | Removed by |
|---|---|---|
| `runs`, `steps` | run records: collected inputs, LLM requests and responses, intents, stderr excerpts | `Store.prune_runs` |
| `journal`, `blobs` | every applied change, with the note's bytes before and after | `Store.prune_runs` (with its runs; a blob is kept while any journal entry refers to it) |
| `llm_cache` | LLM responses keyed on the input hash | `Store.cache_purge`, `Store.cache_purge_expired` |
| `tasks`, `notifications` | task titles and statuses, notification dedupe keys | not pruned (they lose their run link when the run is pruned) |

**Pruning a run removes its undo.** `horae undo` needs the journal; once a run is pruned it can no
longer be undone. Pick a retention longer than the time you would still want to undo a run.

## Pruning old runs

There is no CLI command yet. Run this with the same Horae version as the instance, from the
instance directory (inside the container: `docker exec` into the `horae` service). It uses the
store's own API, in one transaction, and is safe while Horae runs (SQLite WAL with a busy timeout);
avoid running it during a `horae undo`.

```sh
uv run python - <<'EOF'
from datetime import UTC, datetime, timedelta

from horae.store import Store

cutoff = datetime.now(UTC) - timedelta(days=90)  # your retention
with Store.open("state/horae.db") as store:
    print(store.prune_runs(cutoff), "runs pruned")
    print(store.cache_purge(created_before=cutoff), "cached LLM responses purged")
EOF
```

`prune_runs` never deletes a `running` run. Deleted rows can survive in free pages of the database
file: to reclaim them, stop Horae and run `VACUUM`, or rely on your backups' own retention to age
them out:

```sh
python3 -c "import sqlite3; sqlite3.connect('state/horae.db').execute('VACUUM')"
```

A Coolify Scheduled Task (weekly) is a good place for this, next to the pipeline triggers of
[PLAN §7](../PLAN.md#7-reference-deployment-coolify).

## Backing up the store

Never copy `horae.db` with `cp` or a file-level backup while Horae runs: with WAL, the database is
spread over `horae.db`, `horae.db-wal` and `horae.db-shm`, and a copy taken mid-write can be
inconsistent. Use one of:

1. **SQLite's online backup API** (Horae may keep running):

   ```sh
   umask 077
   uv run python - <<'EOF'
   import sqlite3

   src = sqlite3.connect("state/horae.db")
   dst = sqlite3.connect("backup/horae-backup.db")
   with dst:
       src.backup(dst)
   assert dst.execute("PRAGMA integrity_check").fetchone() == ("ok",)
   dst.close()
   src.close()
   EOF
   ```

   `umask 077` keeps the copy `0600`, like the original (the backup file is otherwise created with
   the default umask). The `sqlite3` CLI's `.backup` command does the same where it is installed.
2. **With the container stopped**: copy the whole state directory, `-wal` included.

Back up the vault at the same time: undo restores note bytes from the journal, and a store and a
vault from different days disagree.

## Offsite copies

[PLAN §7](../PLAN.md#7-reference-deployment-coolify) requires an offsite copy of the state
directory and the vault. Both hold personal data (A4, A5 in
[docs/security.md](../security.md#3-assets)), so:

- **encrypt before the data leaves the server**, with a key that is not stored next to the backup
  (for example restic or borg repositories, or `age` on the backup file);
- keep the offsite retention no longer than you need; pruned runs live on in older backups until
  those expire;
- do not include `/run/secrets` or the Coolify environment in the same archive as the data;
  back secrets up separately (a password manager);
- test a restore: `PRAGMA integrity_check` on the restored database, then `horae runs` against it.

## Checklist

- [ ] A weekly prune with a retention you chose, longer than your undo horizon
- [ ] A daily online backup of `horae.db` (or a stopped-container copy), made with `umask 077`
- [ ] The vault backed up at the same time
- [ ] Offsite copies encrypted client-side, key kept elsewhere, retention set
- [ ] A restore tested once
