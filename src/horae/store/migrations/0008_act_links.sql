-- SPDX-License-Identifier: Apache-2.0
-- Signed single-use action links (#38, schemas decision 53).
--
-- 1. Each `POST /act/<token>` is a run of the reserved pipeline `_act` (a pipeline name never
--    starts with `_`): a run row, with the journal entry of the task change it applies, that
--    never gets a run record (it has no pipeline definition or steps), like a live eval's
--    `eval:` row (migration 7). The runs table is rebuilt (create, copy, drop, rename; foreign
--    keys are off while a migration runs) so that the second CHECK also holds for an `_act`
--    row. Every other column, CHECK and index is as in migration 7.
-- 2. `act_consumed`: the nonce of every token that was used, inserted in the transaction that
--    journals its change, before the sink applies it (a primary-key conflict means "already
--    used"). `outcome` is `pending` until the sink answered (a crash leaves it so: at most
--    once), then `applied` or `failed`; `stale` when the task changed since the link was
--    minted (nothing applied). Rows are deleted an hour after the token expired.

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
    -- The RunRecord as JSON; NULL while running, if abandoned, and for a live eval's or an
    -- `/act` row.
    record       TEXT,
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK (
        (record IS NULL)
        = (
            status = 'running'
            OR abandoned_at IS NOT NULL
            OR substr(pipeline, 1, 5) = 'eval:'
            OR pipeline = '_act'
        )
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

CREATE TABLE act_consumed (
    nonce       BLOB PRIMARY KEY CHECK (length(nonce) = 16),
    kid         BLOB NOT NULL CHECK (length(kid) = 4),
    -- The token's expiry (Unix seconds).
    expires_at  INTEGER NOT NULL,
    consumed_at TEXT NOT NULL,
    run_id      TEXT REFERENCES runs (id) ON DELETE SET NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('pending', 'applied', 'stale', 'failed'))
) STRICT;

CREATE INDEX act_consumed_expires ON act_consumed (expires_at);

PRAGMA user_version = 8;
