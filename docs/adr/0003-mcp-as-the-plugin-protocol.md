# ADR 0003: MCP as the plugin protocol

- **Status:** Proposed
- **Date:** 2026-10-27

## Context

Plugins provide sources, sinks, notifiers, skills, transforms and pipeline packs
([§3.1](../PLAN.md#31-concepts)). They must be able to run as separate processes (so that each gets
only its declared secrets, and later its own container), be written in any language, and reuse the
existing personal services that already speak MCP ([§3.3](../PLAN.md#33-plugin-manifest-and-permissions),
[§6](../PLAN.md#6-repositories)).

## Decision

1. **The plugin protocol is MCP**, using the official `mcp` SDK
   ([§3.6](../PLAN.md#36-stack)):
   - **`mcp-stdio`** for local plugins: the core starts the plugin as a subprocess from an argument
     list (no shell);
   - **`mcp-http`** (streamable HTTP) for remote services.
2. **Any MCP server can become a plugin** by adding a `plugin.yaml` manifest.
3. **The core calls tools explicitly from pipeline steps.** There is no LLM tool selection
   ([ADR 0001](0001-pipelines-not-agent-loops.md)).
4. **Permissions are declared in the manifest** (`network`, `secrets`, `paths`, `intents`) and
   enforced in two tiers:
   - **M1–M4, "declared":** one subprocess per plugin, a clean environment with only its declared
     secrets, outputs checked against declared intents;
   - **M5, "isolated":** one container per plugin on an `internal: true` network, egress only
     through an allow-listing proxy built from `permissions.network`, only declared paths mounted,
     read-only filesystem, non-root.

The contract is [`schemas/plugin-manifest/v1.json`](../../schemas/plugin-manifest/v1.json),
generated from [`src/horae/contracts/plugin.py`](../../src/horae/contracts/plugin.py):

- `runtime` is `{ protocol: mcp-stdio, command: [...] }` or `{ protocol: mcp-http, url: https://… }`,
  and is optional for plugins that only ship skills or pipelines;
- for `mcp-http`, the URL host must be in `permissions.network` (checked by the model only);
- permissions default to empty: no access.

## Consequences

### Positive

- Language-agnostic plugins, and existing MCP services plug in through a manifest or
  `plugin-mcp-brief` without being rewritten ([§5](../PLAN.md#5-plugin-catalogue)).
- Process boundaries make secret scoping possible from M1 and container isolation possible in M5
  without changing the protocol.
- The same protocol serves Horae's own read-only MCP server ([§3.5](../PLAN.md#35-interfaces)).

### Negative

- Until M5, enforcement is "declared": a subprocess can still reach hosts or paths it did not
  declare. That is acceptable for first-party plugins only.
- MCP spec and SDK churn can break the plugin host. Mitigation: pin the SDK and keep the host behind
  an internal interface ([§10](../PLAN.md#10-risks-honest)).
- A subprocess per plugin costs memory and start-up time on small servers.

### Open questions

- The v1 contract accepts only `https` for `mcp-http`, including internal Docker or Coolify service
  names (decision 6 in the [schemas README](../../schemas/README.md#decisions-provisional-maintainer-to-confirm)).
  Existing services joined over a Docker network ([§7](../PLAN.md#7-reference-deployment-coolify))
  therefore need TLS; whether that is practical is unverified. Container-name stability across
  Coolify redeploys is a ⚠️ spike in §7.
- What `permissions.paths` are relative to (container, instance root, vault mount) is open (decision 9).
- How a tool namespace (`coach.brief`) resolves to a plugin is for the engine to define (decision 11).
- Default runtime after M5 (containers for third-party plugins only, or all) is open
  ([§11](../PLAN.md#11-open-decisions) item 2).

## Alternatives considered

- **Python packages loaded in-process** (the pip-package model of [§1](../PLAN.md#1-why-horae-the-gap)).
  Simplest to call, but Python-only and no process boundary for secrets or isolation.
- **A custom JSON-RPC or HTTP protocol.** Full control, but no ecosystem; existing services would
  need adapters.

## References

- [PLAN.md §3.3](../PLAN.md#33-plugin-manifest-and-permissions), [§3.6](../PLAN.md#36-stack),
  [§10](../PLAN.md#10-risks-honest), [M1](../PLAN.md#m1--walking-skeleton-1-week),
  [M5](../PLAN.md#m5--hard-isolation-12-weeks)
- [`schemas/plugin-manifest/v1.json`](../../schemas/plugin-manifest/v1.json),
  [`schemas/examples/plugin-manifest/valid/github.yaml`](../../schemas/examples/plugin-manifest/valid/github.yaml),
  [`schemas/examples/plugin-manifest/valid/remote-coach.yaml`](../../schemas/examples/plugin-manifest/valid/remote-coach.yaml)
