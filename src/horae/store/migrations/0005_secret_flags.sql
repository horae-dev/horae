-- SPDX-License-Identifier: Apache-2.0
-- What the store's secret gate found while a run was running (#53, schemas decision 43 C):
-- one row per finding (names of secrets and where, never a value), written in the same
-- transaction as the step row, journal entry, task, notification or ledger row that held it,
-- and turned into `secret_in_output` violations when the run's record is saved (then
-- deleted). Kept in the database, not in memory, so a record saved by another `Store` (or
-- another process: `horae undo` re-saving a run) still gets them. Deleted with the run.

CREATE TABLE secret_flags (
    id      INTEGER PRIMARY KEY,
    run_id  TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    detail  TEXT NOT NULL CHECK (length(detail) BETWEEN 1 AND 4096),
    UNIQUE (run_id, detail)
);

PRAGMA user_version = 5;
