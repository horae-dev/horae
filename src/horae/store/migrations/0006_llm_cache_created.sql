-- SPDX-License-Identifier: Apache-2.0
-- The LLM cache (#32, schemas decision 45): `Store.cache_trim` keeps the most recently
-- created entries (first in, first out: a hit does not refresh an entry) and
-- `Store.cache_purge(created_before=...)` deletes by age; both order or filter on
-- `created_at`, which no index covered (`llm_cache_skill` leads with `skill`).

CREATE INDEX llm_cache_created ON llm_cache (created_at);

PRAGMA user_version = 6;
