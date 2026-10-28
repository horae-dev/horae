-- SPDX-License-Identifier: Apache-2.0
-- LLM cache (M2), task index and notification state (M3).

CREATE TABLE llm_cache (
    -- Hash of the request (skill version, model, normalised input), as sha256 hex.
    key           TEXT PRIMARY KEY CHECK (length(key) = 64),
    skill         TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    model         TEXT NOT NULL,
    -- The response as JSON.
    response      TEXT NOT NULL,
    tokens_in     INTEGER NOT NULL CHECK (tokens_in >= 0),
    tokens_out    INTEGER NOT NULL CHECK (tokens_out >= 0),
    cost_usd      REAL NOT NULL CHECK (cost_usd >= 0),
    created_at    TEXT NOT NULL,
    expires_at    TEXT
) STRICT;

CREATE INDEX llm_cache_expires ON llm_cache (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX llm_cache_skill ON llm_cache (skill, created_at);

CREATE TABLE tasks (
    fingerprint   TEXT PRIMARY KEY CHECK (length(fingerprint) BETWEEN 1 AND 128),
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    status        TEXT NOT NULL CHECK (status IN ('todo', 'done', 'cancelled', 'snoozed')),
    due           TEXT,
    time          TEXT,
    -- When a snoozed task comes back, as given: a date (with `snooze_tz`, the IANA zone that
    -- date is in) or a timestamp; and the resolved instant, in UTC.
    snoozed_until TEXT,
    snooze_tz     TEXT,
    wake_at       TEXT,
    -- Where the task lives (e.g. a note path) and which source proposed it.
    target        TEXT,
    source        TEXT,
    run_id        TEXT REFERENCES runs (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK ((status = 'snoozed') = (snoozed_until IS NOT NULL)),
    CHECK ((snoozed_until IS NULL) = (wake_at IS NULL)),
    CHECK (snooze_tz IS NULL OR snoozed_until IS NOT NULL),
    CHECK (time IS NULL OR due IS NOT NULL)
) STRICT;

CREATE INDEX tasks_status_due ON tasks (status, due);
CREATE INDEX tasks_wake ON tasks (wake_at) WHERE status = 'snoozed';

CREATE TABLE notifications (
    -- A caller-chosen dedupe key, e.g. `remind:<fingerprint>:<due>`.
    key           TEXT PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 256),
    run_id        TEXT REFERENCES runs (id) ON DELETE SET NULL,
    first_sent_at TEXT NOT NULL,
    last_sent_at  TEXT NOT NULL,
    count         INTEGER NOT NULL CHECK (count >= 1)
) STRICT;

PRAGMA user_version = 2;
