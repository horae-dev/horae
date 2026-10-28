# Horae — Plan

_2026-10-24. Items still unverified are marked ⚠️._

---

## 0. TL;DR

- **Horae** (github.com/horae-dev) is a **self-hosted personal agent built for predictability**. It runs fixed pipelines with one bounded LLM step, uses plugins that declare their permissions, and installs those plugins from git.
- It is a deliberate alternative to Hermes or OpenClaw. **The LLM never decides which actions run; the pipeline does.**
- **Build order:**
  1. the **core runtime** (pipeline engine, LLM step, permissions, journal, CLI)
  2. **first-party plugins**
  3. a **git marketplace**
- **A reference instance is Horae's first user.** It runs on a home server under Coolify, and no core feature ships unless its morning note uses it.
- **Milestones:** walking skeleton → LLM step → **dogfood: a daily note and reminders, live on Coolify** → marketplace → hard isolation → existing MCP services → mail and school → public v0.1.
- **The main risk is scope.** Each milestone ends with something you use every day, and the plan says explicitly what Horae will not do (§2.3).

---

## 1. Why Horae: the gap

| | Hermes Agent / OpenClaw | n8n / Windmill / Kestra | LangGraph / PydanticAI | **Horae** |
|---|---|---|---|---|
| Who decides actions | The LLM, in a loop | The workflow | Your code (a graph) | **The pipeline definition** |
| LLM role | Planner with tools | A node | A node | **A typed function: schema in, schema out, no tools** |
| Cost when nothing changed | Every run is a conversation | Low | You decide | **$0 by design** (input hashing) |
| Plugin trust | Public skill hubs (OpenClaw's carried malware in 2026) | Community nodes | pip packages | **Declared, enforced permissions; installed from git and pinned to a commit** |
| Testability | Hard | Medium | Good | **Replay from recorded inputs, golden outputs, dry-run diff** |
| Undo | No | Partial | You build it | **A journal for every write, with `horae undo <run>`** |
| Personal use, self-hosted | Yes | Yes (n8n: Sustainable Use License) | It's a library | **Yes, a single container with a Coolify template** |
| Claude subscription | Blocked since April 2026 | No | No | **Backend calls the official `claude -p` CLI** (see §3.4 ⚠️) |

**The niche:** "I want an assistant that reads my sources every morning and tells me what to do, and I want to trust it the way I trust a cron job." No project found in the research combines a fixed pipeline, bounded LLM steps, enforced plugin permissions and a git marketplace.

---

## 2. Principles (the contract with users)

### 2.1 Design tenets
1. **Pipelines, not loops.** A pipeline is a declared sequence of steps. Control flow is never decided by an LLM.
2. **One bounded LLM step per pipeline** by default. It has:
   - a skill, which is the prompt
   - an output JSON Schema
   - a model alias
   - a token and cost ceiling
   - a timeout
   - **no tools**
3. **Plugins return intents; the core applies them.** A plugin never writes directly. It returns `create_task`, `append_note`, `notify`, … and the core validates them, shows a dry-run diff, journals and applies them.
4. **Everything is declared.** Each plugin declares the hosts, secrets, paths and actions it uses. Undeclared access is a bug, and from M5 it is blocked.
5. **Untrusted content stays data.** Anything read from outside (mail, notes, web pages) is tagged untrusted:
   - It can fill schema fields.
   - It can never choose an action, a recipient or a URL that wasn't already in the input set.
6. **Reproducible.** Every run records its inputs, plugin versions and the LLM request and response. Replays are exact (LLM responses come from the cache).
7. **Boring operations.** One container, SQLite, a CLI, `/healthz`, and push monitors for missed runs.

### 2.2 Measurable guarantees (tested in CI)
- A pipeline whose inputs didn't change **makes no LLM call**.
- `horae run --dry-run` **writes nothing**: no vault change, no notification, no remote write.
- An LLM output that fails its schema is **never applied**. After one retry the run is marked `degraded`.
- A plugin calling an undeclared host, secret or path **fails** (from M5).
- `horae undo <run-id>` restores every file the run touched.

### 2.3 Non-goals (v0.x)
- A general chat agent. Chat can come later as a **read-only client** over Horae's MCP server, or Hermes can be plugged in front.
- Autonomous browsing or computer use.
- Multi-user or SaaS hosting.
- Writing to services where a mistake is costly (bank, school ENT). Those plugins are **read-only by policy**.
- WhatsApp: no compliant way to read a personal account.

---

## 3. Core architecture (`horae-dev/horae`)

### 3.1 Concepts

| Concept | What it is |
|---|---|
| **Instance** | One `horae.yaml`, a `pipelines/` folder, a `horae.lock` file and a state directory |
| **Plugin** | A git repo with a `plugin.yaml` manifest. It provides **sources** (read), **sinks** (apply intents), **notifiers**, **skills** (prompts and schemas for LLM steps) and **pipeline packs** (ready-made pipelines). |
| **Pipeline** | YAML: a trigger, then steps. Step kinds: `collect`, `transform`, `llm`, `act`, `notify`. |
| **Run** | One execution: inputs snapshot, step outputs, intents, applied changes, cost and status (`ok`, `noop`, `degraded`, `failed`) |
| **Journal** | Per-run record of the changes applied, which makes undo possible |
| **Store** | SQLite: runs, journal, LLM cache, task index, notification state |

### 3.2 A pipeline (example: the morning note)

```yaml
# pipelines/daily-brief.yaml
name: daily-brief
trigger: { cron: "30 5 * * *" }          # or run by Coolify / external cron: `horae run daily-brief`
steps:
  - collect:
      - github.review_requests: {}
      - calendar.events: { days: 2 }
      - coach.brief: { endpoints: [finance, running] }   # MCP `brief` of existing services
      - obsidian.open_tasks: {}                          # read back what you already ticked
  - transform: [tasks.normalize, tasks.dedupe_fingerprint, calendar.prep_tasks]
  - llm:
      skill: daily-brief/rank-and-word      # from a plugin; prompt + output schema
      model: default                        # alias resolved in horae.yaml
      budget: { usd: 0.05, tokens_out: 2000 }
      on_unchanged_input: skip              # $0 path
  - act:
      - obsidian.write_note: { path: "Daily/{{date}}.assistant.md", mode: replace-owned }
  - notify:
      - ntfy.digest: { when: "07:30", only_if: due_today_or_overdue }
```

### 3.3 Plugin manifest and permissions

```yaml
# plugin.yaml
name: github
version: 0.1.0
horae: ">=0.1,<0.2"
provides:
  sources: [github.review_requests, github.assigned, github.mentions]
permissions:
  network: [api.github.com]
  secrets: [GITHUB_TOKEN]          # injected only into this plugin
  paths: {}                        # no filesystem access
  intents: []                      # a pure source: may not propose writes
runtime: { protocol: mcp-stdio, command: ["python", "-m", "horae_github"] }
```

- **Plugin protocol = MCP.** Local plugins use MCP over stdio. Remote services use streamable HTTP.
  - Any MCP server can become a plugin by adding a manifest.
  - Plugins can be written in any language.
  - **The core calls tools explicitly from pipeline steps.** There is no LLM tool selection.
- **Enforcement, in two tiers:**
  - **M1–M4, "declared":** each plugin runs as its own subprocess, gets **only its declared secrets** in a clean environment, and its outputs are checked against the declared intents. That is enough for first-party plugins.
  - **M5, "isolated":** each plugin runs in its own container on an `internal: true` Docker network. The only egress is an **allow-listing HTTP proxy** built from `permissions.network`. Only declared paths are mounted, the filesystem is read-only and runs as non-root. This is how to trust third-party plugins.
- **Intents** are a closed set with schemas: `note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`, `notify.push`, `http.post` (only to declared hosts). Sinks implement them, and the core validates, diffs and journals them.

### 3.4 LLM step and backends
- Backends:
  - **`claude-cli`**: runs the official `claude -p --output-format json` with an empty, isolated config. The isolation uses an env allowlist, built-in tools disabled and no user settings.
  - **`anthropic`** (API key)
  - **`openai-compatible`** (OpenRouter, Mistral, vLLM)
  - **`ollama`**
- Every call has: schema-validated output (one repair retry), a cache keyed on (skill version, model, normalized input), a budget per step and per day, and a usage ledger.
- **⚠️ Terms of service.** Anthropic allows personal headless use of `claude -p` on your own plan. It **does not allow third-party products to offer claude.ai login**. So:
  - Horae never handles OAuth itself. The `claude-cli` backend runs **the user's own installed and logged-in Claude Code**, or uses a `CLAUDE_CODE_OAUTH_TOKEN` the user created with `claude setup-token`.
  - It is documented as "personal use under your plan's terms".
  - **The default backend in the published docs is `anthropic` with an API key.** Max plans include monthly API credits that can fund it.
  - Re-check the policy before v0.1.
- **De-identification hook:** before any cloud backend, a transform can pseudonymize declared fields (child names become `kid:A`, amounts are bucketed). The rendered note uses real names again.

### 3.5 Interfaces
- **CLI** (`horae`):
  - `init`, `run [--dry-run]`, `replay <run>`, `undo <run>`, `runs`, `diff`
  - `plugin add | remove | upgrade | audit`, `doctor`
- **HTTP API** (small, FastAPI):
  - `/healthz`
  - `/runs`
  - `/act/<signed-token>` for notification buttons (single use, expires in 24 h; HMAC key from env)
- **MCP server:** read-only tools such as `tasks.today`, `brief.latest` and `runs.recent`, plus `pipeline.run`, which requires approval (an intent with dry-run). This lets Claude Code, Hermes or a phone chat app query Horae without becoming its brain.
- **Scheduler:**
  - a built-in cron (the default, for plain Docker)
  - or **external triggers** (Coolify Scheduled Tasks, systemd, cron) calling `horae run <pipeline>`
  - each run can ping a push monitor (Uptime Kuma or Healthchecks) to detect missed runs

### 3.6 Stack
- **Python 3.12+, uv, pydantic v2, typer, FastAPI, the official `mcp` SDK, SQLite.** They are common in the self-hosted Python ecosystem, so proven code for secrets, LLM backends and alerts can be reused.
- **Plugin SDK:** `horae-sdk` (Python) first. A TypeScript SDK comes later; any MCP server already works.
- **Distribution:**
  - PyPI `horae` (free as of 2026-10-18)
  - `ghcr.io/horae-dev/horae` (public)
  - a Coolify template
- **License:** **Apache-2.0** recommended for the core and SDK (explicit patent grant, friendly to companies). Plugins choose their own license.

---

## 4. Marketplace (git only)

- `horae-dev/marketplace`, file `marketplace.json`:
  ```json
  { "plugins": [ { "name": "github", "repo": "https://github.com/horae-dev/plugin-github",
                   "versions": { "0.1.0": "3f2c9e1…(commit sha)" }, "tier": "first-party" } ] }
  ```
- `horae plugin add github@0.1.0`:
  1. resolve the name in the configured marketplaces (any git URL, so you can run your own)
  2. **`git clone`** the repo and check out the pinned **commit SHA**, then refuse if the tag and SHA disagree
  3. show the permissions and ask for approval
  4. write `horae.lock` (name, version, SHA, approved permissions)
  5. install the plugin's dependencies in **its own venv** (`uv`)
- `horae plugin add https://github.com/you/plugin-x@<sha>` installs without any marketplace.
- **`upgrade` shows a permissions diff.** New hosts, secrets or intents need a new approval. Nothing updates itself.
- **`audit`** lists every installed plugin with its permissions and the last activity seen in runs.
- Later: signed tags (gitsign or sigstore) and a CI badge "tests pass on horae X.Y".

---

## 5. Plugin catalogue

| Plugin | Kind | Notes, with the research behind each one |
|---|---|---|
| `plugin-obsidian` | source + sink | Reads and writes a vault folder in Obsidian Tasks syntax (`- [ ] … 📅 2026-10-22 ⏰ 19:00`). Only writes files or blocks it owns (`*.assistant.md`). Atomic writes. |
| `plugin-livesync` | infra helper | Runs the official `livesync-cli daemon` so the vault exists as plain files on the server (E2EE CouchDB). ⚠️ Check that CLI encryption matches the plugin's. |
| `plugin-ntfy` | notifier | Digest, `⏰` reminders, overdue nudge, quiet hours, `obsidian://` deep links, and "Done" / "Snooze" buttons through `/act`. ⚠️ Test the iOS behaviour of `http` actions and `obsidian://` links. |
| `plugin-github` | source | Review requests, assigned issues, mentions, participating notifications (needs a classic PAT) |
| `plugin-calendar` | source | ICS URLs (Proton share link, Google secret address), CalDAV (iCloud with an app-specific password), Google API. Events that need preparation become tasks due the day before. |
| `plugin-mcp-brief` | source | Generic: calls a `brief()` tool on any MCP endpoint. This is how existing services (for example personal finance or training coaches) plug in without being rewritten. |
| `plugin-imap` | source | Generic IMAP, including the **Proton Mail Bridge** container (paid plan, headless) |
| `plugin-gmail` | source | Rules-first Gmail triage. A small model is used only when a human mail needs interpretation, and requests become tasks. |
| `plugin-school-fr` | source, **read-only** | PRONOTE (pronotepy QR-token login, persist credentials after every login) and ÉcoleDirecte (`ecoledirecte`, hacf-fr, persist `cn`/`cv`). At most twice a day, never retry in a loop. |
| `plugin-homeassistant` | notifier + source | HA notify, presence, and household reminders from HA sensors and calendars |
| `pack-daily-brief` | pipeline pack + skill | The daily note pipeline and the `rank-and-word` skill with its schema |
| `pack-reminders` | pipeline pack | The `remind` pipeline (every 5 min, reads the vault, no LLM) |

---

## 6. Repositories

| Repository | Visibility | Contents |
|---|---|---|
| `horae-dev/.github` | public | ✅ Done: profile, brand, community files |
| `horae-dev/horae` | public | Core, CLI, API, MCP server, SDK (`sdk/`), docs (`docs/`), Dockerfile, `deploy/coolify/` template, `deploy/compose/` |
| `horae-dev/marketplace` | public | `marketplace.json`, CI validating every entry (clone, check SHA, run the plugin's tests against the latest core) |
| `horae-dev/template-plugin` | public | Template repo: manifest, MCP stdio server skeleton, recorded-input tests, CI |
| `horae-dev/plugin-*` | public | One repo per plugin in §5 |
| an instance repo | your choice (no secrets) | `horae.yaml`, `pipelines/`, `horae.lock` and deployment notes for one installation |
| existing MCP services | unchanged | Each adds an MCP `brief` tool and is called through `plugin-mcp-brief` |

---

## 7. Reference deployment (Coolify)

- **Target:** a small home server (x86 mini-PC class, about 16 GB RAM) running **Coolify**, with its Traefik proxy, an SSO provider (e.g. authentik), a self-hosted **ntfy** with an iOS upstream configured, and Uptime Kuma.
- **Coolify resources:**
  1. **`vault-sync`:** the Coolify one-click "Obsidian LiveSync" service (CouchDB) on its own domain, **without** an SSO middleware (the plugin can't do SSO), with E2EE and path obfuscation turned on
  2. **`horae`:** Docker Compose using `ghcr.io/horae-dev/horae` + `plugin-livesync`
     - instance config from the instance repo
     - secrets as Coolify env vars (`${VAR:?}` for required ones) and `/run/secrets` files
     - **Coolify Scheduled Tasks** for `horae run daily-brief` (twice a day) and `horae run remind` (every 5 min), each with a push monitor
     - a domain behind SSO, except `/act/`
  3. **Existing MCP services**, as separate resources, joined through "Connect to predefined network". ⚠️ Spike: check that container names stay stable across redeploys.
- **Updates:** a public GHCR image, so any digest-based updater or Coolify's own redeploy works.
- **Backups:** the Horae state directory and the vault must be in the server's backup set, **with an offsite copy**.
- **LLM:** the `anthropic` backend with an API key, or `claude-cli` for personal use under your plan (§3.4 ⚠️). A small server can't run a useful local model; `ollama` is for machines that can.

---

## 8. Roadmap

Each milestone has a **"done when"** you can check. Time assumes evenings and weekends.

### M0 — Foundations (2–3 days)
- [ ] Create `horae-dev/horae`: Apache-2.0, uv project, ruff, mypy, pytest, CI, gitleaks, Renovate, **ADRs**:
  - 0001: pipelines, not loops
  - 0002: plugins return intents
  - 0003: MCP as the plugin protocol
  - 0004: git marketplace with SHA pinning
  - 0005: the `claude-cli` backend and its terms
  - 0006: SQLite store
- [ ] Specs as code: JSON Schemas for `pipeline`, `plugin.yaml`, the intents, the `horae.lock` file and the run record.
- [ ] `horae-dev/template-plugin` skeleton.
- [ ] **Done when:** `uv run horae --help` works in CI and the schemas validate the examples in §3.2 and §3.3.

### M1 — Walking skeleton (1 week)
- [ ] Pipeline loader and engine (`collect → transform → act`), the run record, SQLite store, journal, `--dry-run` diff, `undo`.
- [ ] Plugin host: start an MCP stdio subprocess with only its declared secrets, call tools, check intents against declarations.
- [ ] Built-in plugins for tests: `fixture-source` and `file-sink`.
- [x] **Done when:** a fixture pipeline writes a note, `--dry-run` shows the diff and writes nothing, `undo` restores it, and a replay gives the same output.

### M2 — The LLM step (4–5 days)
- [ ] Skills format: prompt, input view and output schema, versioned.
- [ ] Backends: `anthropic`, `claude-cli` (isolated), `openai-compatible`, `ollama`. Schema repair retry, cache, budgets, usage ledger.
- [ ] The $0 path (skip on unchanged input) and the de-identification transform.
- [ ] Evals: golden tests per skill, using recorded responses in CI and live runs only on demand.
- [ ] **Done when:** the guarantees in §2.2 about LLM calls pass in CI.

### M3 — Dogfood: a real morning note and reminders, live (1–2 weeks) ⭐
- [ ] Plugins: `obsidian`, `livesync`, `ntfy`, `github`, `calendar`, plus `pack-daily-brief` and `pack-reminders`.
- [ ] HTTP API (`/healthz`, `/act`) and the built-in cron, or external triggers.
- [ ] Dockerfile, GHCR image, **Coolify template** in `deploy/coolify/`.
- [ ] Reference instance:
  - deploy `vault-sync` and `horae` on Coolify
  - set up LiveSync on desktop and phone
  - add push monitors, and add Horae to the server's update and backup routines
- [ ] **Done when:** for **two weeks** the reference instance produces a correct note every morning and reminders on the phone, and ticking a task in Obsidian stops its reminders. LiveSync conflicts are understood. If LiveSync is painful, fall back to Obsidian Sync headless; only `plugin-livesync` changes.

### M4 — Marketplace (1 week)
- [ ] `horae-dev/marketplace` with CI. `horae plugin add | upgrade | remove | audit`: git clone, SHA pin, `horae.lock` file, per-plugin venv, permissions prompt and diff.
- [ ] Move the M3 plugins out of the core repo into `plugin-*` repos and install them via the marketplace in the reference instance.
- [ ] **Done when:** a clean instance installs everything from the marketplace and refuses a tampered SHA.

### M5 — Hard isolation (1–2 weeks)
- [ ] Container runtime per plugin: internal network, egress through an allow-listing proxy, declared mounts only, read-only filesystem, non-root.
- [ ] Security test suite: one malicious fixture plugin per guarantee (undeclared host, secret, path or intent; prompt injection trying to change actions).
- [ ] Horae's MCP server, read-only with approval-gated `pipeline.run`.
- [ ] **Done when:** every malicious fixture fails in CI.

### M6 — Existing MCP services (1–2 weeks, one at a time)
- [ ] `plugin-mcp-brief`, plus a documented `brief()` contract (JSON Schema) that any service can implement.
- [ ] Wire in two real services as references, for example a personal finance coach (aggregates only, never raw transactions) and a training coach.
- [ ] Services stop pushing day-to-day items themselves. Only `crit` alerts push directly.
- [ ] **Done when:** the morning note has a section per service, with no duplicate notifications.

### M7 — Mail and school (2 weeks, best effort)
- [ ] `plugin-imap` with the Proton Mail Bridge container, then `plugin-gmail` (rules-first triage).
- [ ] `plugin-school-fr`: PRONOTE first, then ÉcoleDirecte. Read-only, rate-limited, `degraded` on failure, de-identified before the LLM.
- [ ] Cross-source dedupe: the same homework seen in PRONOTE and in a teacher's mail becomes one task.
- [ ] **Done when:** a week of school items lands correctly in the note, with no account lockout.

### M8 — Public v0.1 (1 week)
- [ ] Docs site (concepts, quick start with Docker and Coolify, writing a plugin, security model), changelog, semver policy, the plugin API stability promise for 0.x.
- [ ] Propose the Coolify one-click template upstream ⚠️ (check their contribution rules). Announce the release.
- [ ] **Done when:** someone else installs Horae from the docs alone.

---

## 9. Quality and security practice
- **Tests per layer:**
  - unit tests (engine, schemas)
  - recorded-input pipeline tests (golden notes)
  - LLM evals (recorded responses in CI)
  - the malicious-plugin suite (M5)
- **Supply chain:** the core pins its dependencies (`uv.lock`), Renovate updates them, and GHCR images are signed (cosign) from M8. Plugins are pinned to a commit.
- **Threat model** (`docs/security.md`, written in M0):
  - untrusted content
  - plugin compromise
  - secret leaks into prompts and logs
  - a stolen `/act` token
  - a compromised marketplace
- **Privacy:** no telemetry. Logs redact declared secrets. Run records keep LLM inputs locally only, with a configurable retention.

---

## 10. Risks (honest)

| Risk | Mitigation |
|---|---|
| **Scope creep** (product plus personal tool) | M3 dogfooding comes before the marketplace and isolation work. The non-goals list is in §2.3. Every feature must be used by the reference instance. |
| Claude terms change for `claude -p` | `anthropic` stays the documented default. The backend is one adapter and is easy to drop. |
| LiveSync complexity or breaking changes | A git snapshot of the vault folder, and Obsidian Sync headless as the fallback |
| Unofficial school APIs break | Read-only, `degraded` status, a separate plugin. The core never depends on it. |
| MCP spec churn | Pin the SDK, keep the plugin host behind an internal interface |
| Small home servers | One container. Plugins as subprocesses until M5. No local model required. |
| Name or trademark conflict | Quick check done on 2026-10-24 (§10.1). Keep the name for an open-source, non-commercial project. Get a professional clearance before any commercial use or filing. |


### 10.1 Name check: "Horae" (quick search, 2026-10-24, not legal advice)

**Method:**
- WIPO Global Brand Database: word "horae", all offices, then filtered on Nice classes **9** (downloadable software) and **42** (SaaS and software development)
- web search for software products using the name
- registry checks on GitHub, PyPI and npm

**Trademarks containing "HORAE" in classes 9 or 42.** 9 records found; the ones still alive:

| Mark | Holder | Office | Classes | Status |
|---|---|---|---|---|
| HORAE | Lunaris Iris | INPI (FR) | 9, 28, 41 | Registered 2018, expires 2027 |
| HORAE OUROBOROS UNFAIRNESS | Lunaris Iris | INPI (FR) | 9, 28, 41 | Registered 2018 |
| Horae | Germain Bay | INPI (FR) | 9, 11, 20, 21, 31, 44 | Registered 2025 |
| Horae | HORAE (company) | INPI (FR) | 14, 38, 41, **42**, 45 | Registered 2024 |
| HORAE TECHNOLOGY | Horae Technology | INPI (FR) | 36, **42** | Registered 2022 |
| HORAE VOLANT | Florian Schramm | INPI (FR) | 9, 14, 18 | Registered 2016 |
| H HORAE THE ULTIMATE PLAYING PLEASURE | Starder Hsieh | CIPO (CA) | 4, 9, 15, 21 | Registered 2013 |

Other classes: wine (Italy, France), education and entertainment (France, class 41).

**What this means:**
- **No EU (EUIPO) or US (USPTO) mark** for "horae" appears in classes 9 or 42 in the WIPO data. The conflicts are **French national marks**, two of them in class 42.
- The exact goods and services lists weren't visible in the WIPO view. ⚠️ Check them on INPI (data.inpi.fr, records 4951818 and 4826696) to see whether either covers "software" in a way that overlaps a personal-agent tool.
- **Products:** a "Horae" team time-management and scheduling tool is listed on Appvizer (vendor unknown, no reviews), and a 2012 Plone package `horae.calendar` exists on PyPI. Neither is an agent runtime, but the first is in an adjacent space (scheduling).
- **Package names:** GitHub `horae-dev` is ours, PyPI `horae` is free, npm `horae` is taken.

**Decision:**
- Keep **Horae** for the open-source project: no sales, no hosted service, no trademark filing.
- Before any commercial offering, a hosted version, or registering the mark, get a professional clearance in FR, EU and US, focused on the two class-42 French marks.
- Fallback names if needed: **Orrery** (needs a different package name) or **Almanac**.

---

## 11. Open decisions
1. **License:** Apache-2.0 (recommended) or MIT?
2. **Plugin runtime default** after M5: container for third-party plugins only (recommended), or for all?
3. **Chat front-end** after v0.1: none, a Telegram bot over the MCP server, or Hermes plugged into Horae's MCP server?
4. **Vault sync:** keep LiveSync as the reference, or also document Obsidian Sync headless as a supported option?
