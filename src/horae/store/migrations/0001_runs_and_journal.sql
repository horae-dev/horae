-- SPDX-License-Identifier: Apache-2.0
-- Runs, their step records, the journal of applied changes and its content blobs (M1).
-- Timestamps are UTC text, `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so they sort lexically.

CREATE TABLE runs (
    id           TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    pipeline     TEXT NOT NULL CHECK (length(pipeline) BETWEEN 1 AND 64),
    -- `running` is store-only: the row exists (so the journal can reference it) but the run
    -- record does not yet. A run whose process died is `failed` with `abandoned_at` and `error`
    -- set and no record. The other statuses are the run record's.
    status       TEXT NOT NULL
                 CHECK (status IN ('running', 'ok', 'noop', 'degraded', 'failed', 'undone')),
    dry_run      INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out   INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd     REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    -- The process that owns a run started with `begin_run`, and its last sign of life.
    pid          INTEGER CHECK (pid > 0),
    host         TEXT CHECK (length(host) BETWEEN 1 AND 255),
    heartbeat_at TEXT,
    -- Set when the run was abandoned (its process crashed or was killed).
    abandoned_at TEXT,
    error        TEXT CHECK (length(error) BETWEEN 1 AND 4096),
    -- The RunRecord as JSON (schemas/run-record/v1.json); NULL while running or if abandoned.
    record       TEXT,
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK ((record IS NULL) = (status = 'running' OR abandoned_at IS NOT NULL)),
    CHECK (abandoned_at IS NULL OR (status = 'failed' AND error IS NOT NULL))
) STRICT;

CREATE INDEX runs_pipeline_started ON runs (pipeline, started_at);
CREATE INDEX runs_started ON runs (started_at);
CREATE INDEX runs_status ON runs (status);
-- The per-pipeline lock: at most one `running` run per pipeline.
CREATE UNIQUE INDEX one_running_per_pipeline ON runs (pipeline) WHERE status = 'running';

-- Steps as they finish, while the run is running. Once the run has a record, the record is the
-- source of truth: these rows are rewritten from it and the store reads the record.
CREATE TABLE steps (
    run_id      TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL CHECK (idx >= 0),
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    -- The StepRecord as JSON.
    record      TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
) STRICT;

-- Content-addressed bytes referenced by the journal, stored once however often they recur.
CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    data   BLOB NOT NULL,
    size   INTEGER NOT NULL CHECK (size = length(data))
) STRICT;

CREATE TABLE journal (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL CHECK (seq >= 0),
    -- Index into the run record's `intents`.
    intent        INTEGER NOT NULL CHECK (intent >= 0),
    intent_type   TEXT NOT NULL CHECK (intent_type IN (
                      'note.create', 'note.append_section', 'note.replace_owned_block',
                      'task.create', 'task.update_status', 'notify.push', 'http.post'
                  )),
    target        TEXT NOT NULL,
    -- NULL `before`: the target did not exist. NULL `after`: nothing to compare against
    -- (e.g. a deletion, or a notification).
    before_sha256 TEXT REFERENCES blobs (sha256),
    after_sha256  TEXT REFERENCES blobs (sha256),
    undoable      INTEGER NOT NULL CHECK (undoable IN (0, 1)),
    -- Journal first, then apply. 0 means "maybe applied": the process may have died between
    -- applying the change and marking it, so compare the target with the before/after hashes.
    applied       INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    undone_at     TEXT,
    created_at    TEXT NOT NULL,
    CHECK (undoable = 0 OR intent_type NOT IN ('notify.push', 'http.post')),
    CHECK (undoable = 0 OR before_sha256 IS NOT NULL OR after_sha256 IS NOT NULL),
    CHECK (undone_at IS NULL OR undoable = 1),
    UNIQUE (run_id, seq)
) STRICT;

CREATE INDEX journal_before ON journal (before_sha256) WHERE before_sha256 IS NOT NULL;
CREATE INDEX journal_after ON journal (after_sha256) WHERE after_sha256 IS NOT NULL;

PRAGMA user_version = 1;
