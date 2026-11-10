-- SPDX-License-Identifier: Apache-2.0
-- Approvals of `pipeline.run` (#55, schemas decision 77).
--
-- One row per request an MCP client made: the dry run it started, the binding digest the
-- user's approval is checked against, who asked (a token's id, or NULL for stdio, with a
-- display name) and where it stands. `approval_events` is its append-only journal: one row per
-- transition, written in the same transaction as the status change. Times are the store's UTC
-- text. A pruned run leaves its approvals (their run ids become NULL).

CREATE TABLE approvals (
    id             TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 64),
    pipeline       TEXT NOT NULL CHECK (length(pipeline) BETWEEN 1 AND 64),
    token_id       INTEGER REFERENCES mcp_tokens (id),
    requested_by   TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 64),
    status         TEXT NOT NULL CHECK (status IN (
                       'planning', 'pending', 'approving', 'applied', 'failed', 'refused',
                       'denied', 'revoked', 'expired', 'void'
                   )),
    created_at     TEXT NOT NULL,
    expires_at     TEXT,
    decided_at     TEXT,
    dry_run_id     TEXT REFERENCES runs (id) ON DELETE SET NULL,
    applied_run_id TEXT REFERENCES runs (id) ON DELETE SET NULL,
    binding_sha256 TEXT CHECK (binding_sha256 IS NULL OR length(binding_sha256) = 64),
    reason         TEXT CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    CHECK (status <> 'pending' OR (expires_at IS NOT NULL AND binding_sha256 IS NOT NULL))
) STRICT;

CREATE INDEX approvals_requester ON approvals (token_id, created_at);
CREATE INDEX approvals_status ON approvals (status);

CREATE TABLE approval_events (
    id          INTEGER PRIMARY KEY,
    approval_id TEXT NOT NULL REFERENCES approvals (id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    event       TEXT NOT NULL CHECK (event IN (
                    'planning', 'pending', 'approving', 'applied', 'failed', 'refused',
                    'denied', 'revoked', 'expired', 'void'
                )),
    run_id      TEXT CHECK (run_id IS NULL OR length(run_id) BETWEEN 1 AND 128),
    detail      TEXT CHECK (detail IS NULL OR length(detail) BETWEEN 1 AND 500)
) STRICT;

CREATE INDEX approval_events_approval ON approval_events (approval_id, id);

PRAGMA user_version = 10;
