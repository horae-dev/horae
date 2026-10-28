-- SPDX-License-Identifier: Apache-2.0
-- Runs, their step records and the journal of applied changes (M1).
-- Timestamps are UTC text, `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so they sort lexically.

CREATE TABLE runs (
    id          TEXT PRIMARY KEY,
    pipeline    TEXT NOT NULL,
    -- `running` is store-only: the row exists (so the journal can reference it) but the
    -- run record does not yet. The other statuses are the run record's.
    status      TEXT NOT NULL
                CHECK (status IN ('running', 'ok', 'noop', 'degraded', 'failed', 'undone')),
    dry_run     INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out  INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd    REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    -- The RunRecord as JSON (schemas/run-record/v1.json); NULL while running.
    record      TEXT,
    CHECK ((status = 'running') = (record IS NULL)),
    CHECK ((record IS NULL) = (finished_at IS NULL))
) STRICT;

CREATE INDEX runs_pipeline_started ON runs (pipeline, started_at);
CREATE INDEX runs_started ON runs (started_at);
CREATE INDEX runs_status ON runs (status);

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

CREATE TABLE journal (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL CHECK (seq >= 0),
    -- Index into the run record's `intents`.
    intent        INTEGER NOT NULL CHECK (intent >= 0),
    intent_type   TEXT NOT NULL,
    target        TEXT NOT NULL,
    -- NULL `before`: the target did not exist. NULL `after`: nothing to compare against
    -- (e.g. a deletion, or a notification).
    before        BLOB,
    before_sha256 TEXT,
    after         BLOB,
    after_sha256  TEXT,
    undoable      INTEGER NOT NULL CHECK (undoable IN (0, 1)),
    -- Journal first, then apply: 0 until the engine confirms the change was applied.
    applied       INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    created_at    TEXT NOT NULL,
    CHECK ((before IS NULL) = (before_sha256 IS NULL)),
    CHECK ((after IS NULL) = (after_sha256 IS NULL)),
    UNIQUE (run_id, seq)
) STRICT;

PRAGMA user_version = 1;
