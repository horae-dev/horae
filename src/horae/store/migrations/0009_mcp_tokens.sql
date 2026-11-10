-- SPDX-License-Identifier: Apache-2.0
-- Bearer tokens of the MCP server's HTTP transport (#56, schemas decision 75).
--
-- One row per token ever created; a revoked token keeps its row (`revoked_at`), so `horae mcp
-- token list` can show it and its name can be reused. Only the token's digest is stored
-- (`token_sha256`: SHA-256 of a domain label and the token, `horae.mcp_auth.token_digest`),
-- never the token. `scopes` is a space-separated, sorted list of `horae.mcp_auth.SCOPES`.
-- Times are the store's UTC text. A name is unique among the tokens not revoked.

CREATE TABLE mcp_tokens (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    token_sha256 BLOB NOT NULL UNIQUE CHECK (length(token_sha256) = 32),
    scopes       TEXT NOT NULL CHECK (length(scopes) BETWEEN 1 AND 256),
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    last_used_at TEXT,
    revoked_at   TEXT
) STRICT;

CREATE UNIQUE INDEX mcp_tokens_active_name ON mcp_tokens (name) WHERE revoked_at IS NULL;

PRAGMA user_version = 9;
