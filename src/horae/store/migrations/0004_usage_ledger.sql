-- SPDX-License-Identifier: Apache-2.0
-- The usage ledger (#33, schemas decision 41): one row per LLM call, for the daily budgets
-- and `horae usage`.
--
-- A row is written *before* the call as a `reserved` row holding the most the call can cost
-- (in the same `BEGIN IMMEDIATE` transaction that read the day's spend, so two runs cannot
-- both reserve the last of a daily ceiling), then `settled` with what the call really cost
-- right after it. A process that dies in between leaves its `reserved` row: it keeps
-- counting at its reserved amount (an upper bound), so a crash never frees budget.
-- `estimated` marks a settled row whose cost is that upper bound because the provider
-- reported none (a timeout, a lost connection), or whose run was abandoned (the reservation
-- of a dead process). `over_reservation` marks a settled row that cost more than it reserved
-- (a provider reporting more than the bound; logged).
--
-- `day` is the local day (instance time zone) the call was reserved in, stored then: a later
-- change of `timezone` does not move past rows. `at` is its UTC time.
-- The ledger outlives its runs: `prune_runs` sets `run_id` to NULL (the totals of a day never
-- change), and the ledger has its own retention (`prune_usage`).

CREATE TABLE llm_usage (
    id                 INTEGER PRIMARY KEY,
    run_id             TEXT REFERENCES runs (id) ON DELETE SET NULL,
    pipeline           TEXT NOT NULL CHECK (length(pipeline) BETWEEN 1 AND 64),
    step               INTEGER NOT NULL CHECK (step >= 0),
    attempt            INTEGER NOT NULL CHECK (attempt >= 1),
    alias              TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 128),
    backend            TEXT CHECK (backend IS NULL OR length(backend) BETWEEN 1 AND 64),
    model              TEXT CHECK (model IS NULL OR length(model) BETWEEN 1 AND 128),
    state              TEXT NOT NULL CHECK (state IN ('reserved', 'settled')),
    tokens_in          INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out         INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cache_read_tokens  INTEGER CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
    cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    usd                REAL NOT NULL DEFAULT 0 CHECK (usd >= 0),
    priced             INTEGER CHECK (priced IS NULL OR priced IN (0, 1)),
    estimated          INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
    over_reservation   INTEGER NOT NULL DEFAULT 0 CHECK (over_reservation IN (0, 1)),
    at                 TEXT NOT NULL,
    day                TEXT NOT NULL
                       CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    settled_at         TEXT,
    CHECK ((state = 'settled') = (settled_at IS NOT NULL)),
    CHECK (estimated = 0 OR state = 'settled'),
    CHECK (over_reservation = 0 OR state = 'settled')
) STRICT;

CREATE INDEX llm_usage_day ON llm_usage (day, pipeline);
CREATE INDEX llm_usage_run ON llm_usage (run_id);

PRAGMA user_version = 4;
