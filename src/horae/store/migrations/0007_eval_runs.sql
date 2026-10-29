-- SPDX-License-Identifier: Apache-2.0
-- Live skill evals are runs of their own (#36, schemas decision 48 J): `horae eval --live`
-- begins a run row of pipeline `eval:<skill>` (a pipeline name never holds `:`), reserves and
-- settles each case in the usage ledger under it, and finishes it `ok` or `failed` with its
-- cost, but never gives it a run record (it has no pipeline definition, steps or changes).
-- The runs table is rebuilt (the documented create, copy, drop, rename procedure; foreign
-- keys are off while a migration runs) so that such a finished row may have no record: the
-- second CHECK now also holds for an `eval:` row. Every other column, CHECK and index is
-- as in migration 1.

CREATE TABLE runs_new (
    id           TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    pipeline     TEXT NOT NULL CHECK (length(pipeline) BETWEEN 1 AND 64),
    status       TEXT NOT NULL
                 CHECK (status IN ('running', 'ok', 'noop', 'degraded', 'failed', 'undone')),
    dry_run      INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out   INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd     REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    pid          INTEGER CHECK (pid > 0),
    host         TEXT CHECK (length(host) BETWEEN 1 AND 255),
    heartbeat_at TEXT,
    abandoned_at TEXT,
    error        TEXT CHECK (length(error) BETWEEN 1 AND 4096),
    -- The RunRecord as JSON; NULL while running, if abandoned, and for a live eval's row.
    record       TEXT,
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK (
        (record IS NULL)
        = (status = 'running' OR abandoned_at IS NOT NULL OR substr(pipeline, 1, 5) = 'eval:')
    ),
    CHECK (abandoned_at IS NULL OR (status = 'failed' AND error IS NOT NULL))
) STRICT;

INSERT INTO runs_new (
    id, pipeline, status, dry_run, started_at, finished_at, tokens_in, tokens_out, cost_usd,
    pid, host, heartbeat_at, abandoned_at, error, record
)
SELECT
    id, pipeline, status, dry_run, started_at, finished_at, tokens_in, tokens_out, cost_usd,
    pid, host, heartbeat_at, abandoned_at, error, record
FROM runs;

DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;

CREATE INDEX runs_pipeline_started ON runs (pipeline, started_at);
CREATE INDEX runs_started ON runs (started_at);
CREATE INDEX runs_status ON runs (status);
CREATE UNIQUE INDEX one_running_per_pipeline ON runs (pipeline) WHERE status = 'running';

PRAGMA user_version = 7;
