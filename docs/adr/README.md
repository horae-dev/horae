# Architecture decision records

Each ADR records one architectural decision: its context, the decision, its consequences and the
alternatives considered. All ADRs use the same [template](0000-template.md).

| ADR | Title | Status | PLAN.md |
|---|---|---|---|
| [0001](0001-pipelines-not-agent-loops.md) | Pipelines, not agent loops | Proposed | [§2.1](../PLAN.md#21-design-tenets), [§3.2](../PLAN.md#32-a-pipeline-example-the-morning-note) |
| [0002](0002-intents-applied-by-the-core.md) | Plugins return intents; the core applies them | Proposed | [§2.1](../PLAN.md#21-design-tenets), [§3.3](../PLAN.md#33-plugin-manifest-and-permissions) |
| [0003](0003-mcp-as-the-plugin-protocol.md) | MCP as the plugin protocol | Proposed | [§3.3](../PLAN.md#33-plugin-manifest-and-permissions) |
| [0004](0004-git-marketplace-with-sha-pinning.md) | Git marketplace with commit-SHA pinning | Proposed | [§4](../PLAN.md#4-marketplace-git-only) |
| [0005](0005-claude-cli-backend-and-its-terms.md) | The `claude-cli` backend and its terms | Proposed | [§3.4](../PLAN.md#34-llm-step-and-backends) |
| [0006](0006-sqlite-as-the-store.md) | SQLite as the store | Proposed | [§3.1](../PLAN.md#31-concepts), [§2.1](../PLAN.md#21-design-tenets) |

## Process

- Copy `0000-template.md` to `NNNN-short-title.md` with the next free number, and add a row here.
- A new ADR starts as **Proposed**. The maintainer sets it to **Accepted** when merging the review.
- An accepted ADR is not rewritten. To change a decision, write a new ADR that supersedes it and
  set the old one to **Superseded by ADR NNNN**, linking the new ADR.
- **Deprecated** means the decision no longer applies and nothing replaces it (for example the
  feature was removed); the ADR is kept for history.
- The contracts in [`src/horae/contracts/`](../../src/horae/contracts/) (exported to
  [`schemas/`](../../schemas/README.md)) are the normative form of many of these decisions. When an
  ADR and a contract disagree, that is a bug in one of them: fix it in the
  same PR or open an issue.
