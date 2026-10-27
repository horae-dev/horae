# Roadmap

_Dates assume the **likely** estimate with a part-time maintainer (15 h/week) supervising 4 agents. See [ESTIMATION.md](ESTIMATION.md). They will be re-forecast after M1._

| Milestone | Goal | Start | Target | Weeks | Supervision h | Agent h | Human team (3 devs) weeks |
|---|---|---|---|---|---|---|---|
| **M0 Foundations** | Repos, CI, ADRs, schemas, threat model, agent workflow | 2026-11-02 | 2026-12-02 | 4.4 | 66.0 | 17.9 | 1.3 |
| **M1 Walking skeleton** | Pipeline engine, store, journal, dry-run, undo, plugin host v1, CLI | 2026-12-03 | 2027-02-03 | 9.0 | 135.3 | 45.7 | 3.3 |
| **M2 LLM step** | Skills, backends, schema validation, cache, budgets, de-identification | 2027-02-04 | 2027-04-12 | 9.8 | 146.7 | 44.0 | 2.6 |
| **M3 Dogfood** | First plugins, API, scheduler, image, Coolify template: a real daily note and reminders | 2027-04-13 | 2027-08-19 | 18.4 | 276.4 | 95.3 | 6.6 |
| **M4 Marketplace** | Git marketplace, pinned installs, permissions approval and lockfile | 2027-08-20 | 2027-10-10 | 7.4 | 110.6 | 31.4 | 1.9 |
| **M5 Hard isolation** | Plugin containers, egress allow-list, malicious-plugin suite, MCP server | 2027-10-11 | 2028-01-15 | 13.9 | 208.1 | 34.8 | 2.5 |
| **M6 Existing MCP services** | brief() contract and plugin-mcp-brief | 2028-01-16 | 2028-02-01 | 2.4 | 35.6 | 12.0 | 0.7 |
| **M7 Mail, school, home** | IMAP/Proton, Gmail, PRONOTE/ÉcoleDirecte, Home Assistant | 2028-02-02 | 2028-03-25 | 7.6 | 113.9 | 34.2 | 2.6 |
| **M8 Public v0.1** | Docs site, release automation, signed artifacts, v0.1 | 2028-03-26 | 2028-04-13 | 2.8 | 41.7 | 9.3 | 1.0 |

**Critical path:** M0 → M1 (engine, plugin host) → M2 (LLM step) → M3 (dogfood). M4-M8 build on a working dogfood instance. M6 and M7 plugins can start in parallel once M3 is done if supervisor time allows.

## Burning points

The hot topics that can stall the project. Each one is tracked as a **Risk** issue in `horae` and visible in the project's "Burning points" view.

| Risk | Severity | When | Mitigation |
|---|---|---|---|
| **Scope creep: product and personal tool at once** — Building a public product and a personal assistant at the same time can stall both. | high | M0-M8 | M3 dogfooding comes before marketplace and isolation; non-goals in PLAN §2.3; every feature must be used by the reference instance. |
| **Supervisor bandwidth is the real bottleneck** — Agents produce PRs faster than one part-time human can specify and review them (industry data: review time up 2-5x with agents). | high | M0-M8 | Graft policy auto-approves trivial and small logic changes; specs written as acceptance criteria up front; at most 3-4 agents in parallel; recalibrate estimates after 15 stories. |
| **Security-critical code written by agents** — Permissions, sandboxing, signed links and secrets are where AI code fails most (studies: ~45% insecure choices) and mistakes are costly. | high | M1, M4, M5 | These stories are human-led (agent drafts, human designs and reviews), sensitive paths in graft.yaml, malicious-plugin suite in CI. |
| **Claude terms for the claude-cli backend** — Anthropic allows personal headless use of claude -p but not third-party products offering claude.ai login. | medium | M2 | Horae never handles OAuth; API backend is the documented default; re-check terms before v0.1. |
| **LiveSync CLI encryption parity and sync conflicts** — CLI encryption docs are thin; conflicts between server writes and phone edits are possible. | medium | M3 | Plugin owns separate files/blocks only; parity test on real devices; nightly git snapshot; Obsidian Sync headless as fallback. |
| **iOS behaviour of ntfy action buttons and deep links unverified** — ntfy docs do not say whether http actions and obsidian:// links work on iOS. | medium | M3 | Device test task in M3; fallback to view actions that open the note. |
| **Unofficial school APIs break or lock accounts** — PRONOTE/ÉcoleDirecte libraries are reverse-engineered; repeated logins can lock accounts. | medium | M7 | Read-only, rate-limited, degraded status, separate plugin; core never depends on it. |
| **Coolify cross-resource networking** — Reaching other services by container name across Coolify resources may not be stable across redeploys. | low | M3, M6 | Spike in M3; fallback to an internal route with a bearer token and IP allow-list. |
| **MCP specification churn** — MCP SDK and spec changes can break the plugin host. | low | M1, M5 | Pin the SDK; keep the plugin host behind an internal interface. |
| **Trademark: French marks in class 42** — Several French national marks contain HORAE, two in class 42. | low | M8 | Open source, no commercial use or filing; professional clearance before any commercial step (PLAN §10.1). |

## How the work flows (AI-first)

1. **Ready:** a story has acceptance criteria, an estimate, an executor (Agent, Agent + human check, Human-led, Human-only) and its dependencies done.
2. **Agent works:** one agent per story, on its own branch, following the repository's `AGENTS.md`. Commits carry Graft trailers (`Graft-Agent`, `Graft-Model`, `Graft-Task`).
3. **Review:** Graft writes an intent card, checks it against the diff and applies `graft.yaml`: trivial and small low-risk changes can be approved without a human; security paths, core contracts and large changes require the maintainer.
4. **Done:** merged with CI green, acceptance criteria checked, docs updated, estimate actuals recorded on the issue.
