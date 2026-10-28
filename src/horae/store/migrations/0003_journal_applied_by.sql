-- SPDX-License-Identifier: Apache-2.0
-- The journal names the sink that applied each change (#22), so `horae undo` restores a
-- change through the same sink even for a run abandoned mid-apply (which has no run record).
-- NULL for entries written before this migration, or by callers that do not say.

ALTER TABLE journal ADD COLUMN applied_by TEXT
    CHECK (applied_by IS NULL OR length(applied_by) BETWEEN 1 AND 128);

PRAGMA user_version = 3;
