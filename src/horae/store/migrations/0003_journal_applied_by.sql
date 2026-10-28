-- SPDX-License-Identifier: Apache-2.0
-- What `horae undo` needs to find, check and revert a change (#22, schemas decision 34):
--
-- - `applied_by`: the sink's tool, so undo restores a change through the same sink even for a
--   run abandoned mid-apply (which has no run record);
-- - `sink_identity`: where that sink writes (e.g. a local vault's real path and device and
--   inode), so undo refuses to "restore" a note in another vault;
-- - `actual_after_sha256`: the bytes a sink really wrote when they differ from the planned
--   `after` (`apply_mismatch`), which undo accepts as "as the run left it";
-- - `undo_overwrote_sha256`: the bytes a forced undo overwrote, kept so they can be recovered.
--
-- All are NULL for entries written before this migration, or by callers that do not say.

ALTER TABLE journal ADD COLUMN applied_by TEXT
    CHECK (applied_by IS NULL OR length(applied_by) BETWEEN 1 AND 128);
ALTER TABLE journal ADD COLUMN sink_identity TEXT
    CHECK (sink_identity IS NULL OR length(sink_identity) BETWEEN 1 AND 512);
ALTER TABLE journal ADD COLUMN actual_after_sha256 TEXT REFERENCES blobs (sha256);
ALTER TABLE journal ADD COLUMN undo_overwrote_sha256 TEXT REFERENCES blobs (sha256);

-- Later changes of the same note (undo is last-in, first-out per note).
CREATE INDEX journal_target ON journal (target, id) WHERE undoable = 1;
CREATE INDEX journal_actual_after ON journal (actual_after_sha256)
    WHERE actual_after_sha256 IS NOT NULL;
CREATE INDEX journal_undo_overwrote ON journal (undo_overwrote_sha256)
    WHERE undo_overwrote_sha256 IS NOT NULL;

PRAGMA user_version = 3;
