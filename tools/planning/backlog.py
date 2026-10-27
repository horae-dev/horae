"""Horae backlog: repositories, epics, stories, human tasks and risks.

Estimates are human ideal days as (optimistic, most likely, pessimistic).
Categories drive the AI-agent model in estimate.py:
  normal   regular feature work
  docs     docs, CI, boilerplate
  contract core correctness / contracts other code depends on
  sec      security-critical (permissions, sandbox, auth, secrets): human-led
  integ    cross-repo / multi-service integration
  ext      unofficial or auth-heavy external APIs, needs live manual testing
  spec     ambiguous / design-heavy (prompts, UX of the note)
  human    human-only, no agent
"""

MILESTONES = {
    "M0": ("Foundations", "Repos, CI, ADRs, schemas, threat model, agent workflow"),
    "M1": ("Walking skeleton", "Pipeline engine, store, journal, dry-run, undo, plugin host v1, CLI"),
    "M2": ("LLM step", "Skills, backends, schema validation, cache, budgets, de-identification"),
    "M3": ("Dogfood", "First plugins, API, scheduler, image, Coolify template: a real daily note and reminders"),
    "M4": ("Marketplace", "Git marketplace, pinned installs, permissions approval and lockfile"),
    "M5": ("Hard isolation", "Plugin containers, egress allow-list, malicious-plugin suite, MCP server"),
    "M6": ("Existing MCP services", "brief() contract and plugin-mcp-brief"),
    "M7": ("Mail, school, home", "IMAP/Proton, Gmail, PRONOTE/ÉcoleDirecte, Home Assistant"),
    "M8": ("Public v0.1", "Docs site, release automation, signed artifacts, v0.1"),
}

REPOS = {
    "horae": "Core runtime, CLI, HTTP API, MCP server, SDK and docs for Horae, a predictable self-hosted personal agent.",
    "marketplace": "Git-based plugin index for Horae (marketplace.json) with CI that verifies every pinned plugin.",
    "template-plugin": "Template repository for writing a Horae plugin: manifest, MCP stdio server, recorded-input tests and CI.",
    "plugin-obsidian": "Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.",
    "plugin-livesync": "Horae plugin: keep an Obsidian vault as plain files on the server with Self-hosted LiveSync (E2EE CouchDB).",
    "plugin-ntfy": "Horae plugin: push notifications through ntfy, with digests, reminders, quiet hours and action buttons.",
    "plugin-github": "Horae plugin: GitHub review requests, assigned issues, mentions and participating notifications as tasks.",
    "plugin-calendar": "Horae plugin: calendars from ICS URLs, CalDAV and the Google Calendar API, with preparation tasks.",
    "plugin-mcp-brief": "Horae plugin: call a brief() tool on any MCP service and turn its section into tasks and alerts.",
    "plugin-imap": "Horae plugin: generic IMAP source, including Proton Mail through Proton Mail Bridge.",
    "plugin-gmail": "Horae plugin: rules-first Gmail triage that turns requests into tasks.",
    "plugin-school-fr": "Horae plugin: read-only PRONOTE and ÉcoleDirecte homework, notes and deadlines as tasks.",
    "plugin-homeassistant": "Horae plugin: Home Assistant notifications and household reminders from sensors and calendars.",
    "pack-daily-brief": "Horae pipeline pack: the morning note pipeline and its rank-and-word skill.",
    "pack-reminders": "Horae pipeline pack: reminders, daily digest and overdue nudges from the vault, without an LLM.",
}

# (key, repo, milestone, title, summary)
EPICS = [
    ("E-core-found", "horae", "M0", "Foundations", "Repository, CI, ADRs, JSON Schemas, threat model and the agent workflow that every other epic relies on."),
    ("E-core-engine", "horae", "M1", "Pipeline engine and plugin host", "Load an instance, run pipelines, record runs, apply intents with dry-run, journal, undo and replay; talk to plugins over MCP stdio."),
    ("E-core-llm", "horae", "M2", "Bounded LLM step", "Skills with output schemas, model backends (anthropic, claude-cli, openai-compatible, ollama), cache, budgets, de-identification and untrusted-content guards."),
    ("E-core-runtime", "horae", "M3", "Runtime and delivery", "HTTP API, signed action links, scheduler, container image, Coolify template and the plugin SDK."),
    ("E-core-market", "horae", "M4", "Marketplace client", "Install, upgrade, remove and audit plugins from git with pinned commits, per-plugin venvs and approved permissions."),
    ("E-core-isolation", "horae", "M5", "Hard isolation", "Per-plugin containers, egress allow-list proxy, declared mounts, secret redaction and a malicious-plugin test suite."),
    ("E-core-mcp", "horae", "M5", "Horae MCP server", "Read-only MCP tools for other agents and an approval-gated pipeline.run."),
    ("E-core-release", "horae", "M8", "Docs and v0.1 release", "Docs site, release automation, signed artifacts and the v0.1 release."),
    ("E-human-ops", "horae", "M0", "Human operations", "Tasks only a human can do: accounts, keys, GitHub App installs, server resources and device tests."),
    ("E-market", "marketplace", "M4", "Marketplace index", "marketplace.json, its schema and CI that verifies every entry."),
    ("E-template", "template-plugin", "M3", "Plugin template", "A template repository that produces a working, tested plugin in minutes."),
    ("E-obsidian", "plugin-obsidian", "M3", "Obsidian vault plugin", "Read tasks from the vault and apply note and task intents to files the plugin owns."),
    ("E-livesync", "plugin-livesync", "M3", "LiveSync bridge", "Mirror an E2EE LiveSync CouchDB to plain files on the server, with snapshots."),
    ("E-ntfy", "plugin-ntfy", "M3", "ntfy notifier", "Digests, reminders, quiet hours, deep links and action buttons."),
    ("E-github", "plugin-github", "M3", "GitHub source", "Review requests, assignments, mentions and notifications as tasks."),
    ("E-calendar", "plugin-calendar", "M3", "Calendar source", "ICS, CalDAV and Google Calendar events, and preparation tasks."),
    ("E-brief", "pack-daily-brief", "M3", "Daily brief pack", "The morning note pipeline and its LLM skill."),
    ("E-remind", "pack-reminders", "M3", "Reminders pack", "Reminders and digests computed from the vault, no LLM."),
    ("E-mcpbrief", "plugin-mcp-brief", "M6", "MCP brief bridge", "A brief() contract and a source that calls it on any MCP service."),
    ("E-imap", "plugin-imap", "M7", "IMAP and Proton Mail", "Generic IMAP source and the Proton Mail Bridge recipe."),
    ("E-gmail", "plugin-gmail", "M7", "Gmail triage", "Rules-first Gmail triage into tasks."),
    ("E-school", "plugin-school-fr", "M7", "French school platforms", "Read-only PRONOTE and ÉcoleDirecte sources."),
    ("E-ha", "plugin-homeassistant", "M7", "Home Assistant", "HA notifications and household reminders."),
]

# (epic, title, summary, [acceptance criteria], (O, M, P), category, milestone or None=epic's)
S = []
def story(epic, title, summary, ac, est, cat, ms=None):
    S.append(dict(epic=epic, title=title, summary=summary, ac=ac, est=est, cat=cat, ms=ms))

# ---------------- horae core ----------------
story("E-core-found", "Scaffold the uv project and quality tooling",
      "Python 3.12+ project managed with uv: src layout, ruff, mypy (strict), pytest, pre-commit with gitleaks, Apache-2.0 headers where needed.",
      ["`uv run horae --help` prints the CLI help", "`uv run ruff check`, `uv run mypy`, `uv run pytest` pass on an empty project", "pre-commit runs ruff and gitleaks", "Renovate configured for Python deps and GitHub Actions"],
      (0.5, 1, 1.5), "docs")
story("E-core-found", "CI pipeline with lint, types, tests and security scans",
      "GitHub Actions: lint, mypy, pytest with coverage gate, gitleaks, semgrep or CodeQL, on PRs and main.",
      ["CI runs on every PR and push to main", "Coverage gate at 85% for `src/horae` (configurable)", "Security scan results visible on PRs", "Workflow uses pinned action SHAs"],
      (0.5, 1, 1.5), "docs")
story("E-core-found", "Write ADRs 0001-0006",
      "Architecture decision records: pipelines not loops, intents applied by core, MCP as plugin protocol, git marketplace with SHA pinning, claude-cli backend and its terms, SQLite store.",
      ["Six ADRs in `docs/adr/` using one template (context, decision, consequences)", "Each ADR links the relevant PLAN.md section", "Reviewed and accepted by the maintainer"],
      (1, 1.5, 2.5), "spec")
story("E-core-found", "JSON Schemas for pipeline, plugin manifest, intents, lockfile and run record",
      "Machine-readable contracts under `schemas/`, with pydantic models generated or kept in sync, and example files.",
      ["Schemas for pipeline, plugin.yaml, each intent, horae.lock and run record", "The examples from PLAN.md §3.2 and §3.3 validate", "A test fails if pydantic models and schemas drift", "Schemas are versioned (`$id` with version)"],
      (1, 2, 3), "contract")
story("E-core-found", "Threat model (docs/security.md)",
      "Threat model covering untrusted content, plugin compromise, secret leaks into prompts/logs, stolen /act tokens and a compromised marketplace; maps each threat to a control and a test.",
      ["Assets, trust boundaries and attacker models documented", "Each threat maps to a planned control and a test story", "Reviewed by the maintainer"],
      (1, 1.5, 3), "sec")
story("E-core-found", "Agent workflow: AGENTS.md, Graft trailers and definition of done",
      "Make the repo agent-ready: AGENTS.md (and CLAUDE.md pointing to it), Graft provenance trailers via a prepare-commit-msg hook, PR checklist and definition of done.",
      ["AGENTS.md explains setup, commands, conventions and the definition of done", "Commits made by agents carry `Graft-Agent`, `Graft-Model`, `Graft-Task` trailers", "graft.yaml policy present and valid (`pnpm graft policy check` or schema)"],
      (0.5, 0.5, 1), "docs")

story("E-core-engine", "Instance and configuration loader",
      "Load `horae.yaml`, the `pipelines/` folder and `horae.lock`; resolve model aliases and secret references (env or /run/secrets files) without ever logging values.",
      ["Valid instance loads into typed models; invalid ones give precise errors with file and line", "Secrets resolved from env or files, never printed (tested)", "`horae doctor` reports config problems"],
      (1, 1.5, 2.5), "normal")
story("E-core-engine", "Pipeline model and step execution",
      "Pipeline object with ordered steps `collect`, `transform`, `llm`, `act`, `notify`; templating of parameters (`{{date}}`), step outputs passed as typed data.",
      ["The daily-brief example from PLAN.md §3.2 parses and executes with fixture plugins", "Step failures mark the run `failed` or `degraded` according to step policy", "No step can change control flow at run time"],
      (1.5, 2.5, 4), "contract")
story("E-core-engine", "SQLite store with migrations",
      "Tables for runs, step records, journal, LLM cache, task index and notification state; migrations with a schema version.",
      ["Fresh install creates the schema; upgrades migrate in place", "WAL mode and busy timeout set", "Store API covered by tests, including concurrent runs"],
      (1, 2, 3), "normal")
story("E-core-engine", "Run lifecycle, locking and statuses",
      "Run record with inputs snapshot, outputs, intents, applied changes, cost and status `ok | noop | degraded | failed`; per-pipeline lock and timeouts.",
      ["Two concurrent runs of one pipeline: the second exits with a clear lock message", "Timeouts stop steps and mark the run", "`horae runs` lists runs with status, duration and cost"],
      (1, 2, 3), "contract")
story("E-core-engine", "Intent model and validation",
      "Closed set of intents (`note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`, `notify.push`, `http.post`) with schemas; reject intents a plugin did not declare.",
      ["Each intent validated against its schema", "An undeclared intent from a plugin fails the step (tested)", "`http.post` only to hosts declared by the plugin"],
      (1, 2, 3), "contract")
story("E-core-engine", "Dry-run diff",
      "`horae run --dry-run` computes intents and shows a unified diff for file changes and a list of notifications and HTTP calls, without applying anything.",
      ["Dry-run writes nothing anywhere (filesystem, network, notifications), enforced by a test", "Diff output is readable in a terminal and as JSON (`--json`)"],
      (1, 1.5, 2.5), "normal")
story("E-core-engine", "Journal and undo",
      "Record before/after state for every applied change; `horae undo <run-id>` restores files and marks the run undone.",
      ["Undo restores every file touched by a run, byte for byte", "Undo refuses when files changed since (unless `--force`)", "Notifications are listed as not undoable"],
      (1, 2, 3), "contract")
story("E-core-engine", "Replay from recorded inputs",
      "`horae replay <run-id>` re-executes a run from its inputs snapshot and cached LLM responses and compares outputs.",
      ["Replay of a recorded run yields identical intents", "Differences are reported as a diff", "Replay never calls external services"],
      (1, 1.5, 3), "normal")
story("E-core-engine", "Plugin host v1 (MCP stdio, declared secrets only)",
      "Start each plugin as a subprocess speaking MCP over stdio; clean environment with only its declared secrets; explicit tool calls from pipeline steps; timeouts and crash handling.",
      ["A plugin sees only its declared env vars (tested with a probe plugin)", "Tool calls time out and report errors without crashing the run", "Plugin stderr captured into the run record with secrets redacted"],
      (2, 3, 5), "sec")
story("E-core-engine", "Built-in fixture plugins and end-to-end test",
      "`fixture-source` and `file-sink` plugins for tests and examples; an end-to-end test covering run, dry-run, undo and replay.",
      ["E2E test runs in CI in under 60 seconds", "Fixture plugins documented as examples"],
      (0.5, 1, 1.5), "normal")
story("E-core-engine", "CLI commands",
      "`horae init | run [--dry-run] | runs | diff | undo | replay | doctor` with consistent output and exit codes.",
      ["`horae init` creates a working example instance", "Exit codes documented and tested", "`--json` output for scripting on every command"],
      (1, 2, 3), "normal")

story("E-core-llm", "Skill format and loader",
      "A skill is a versioned folder with a prompt, an input view (which fields the model sees) and an output JSON Schema.",
      ["Skill version is part of the cache key", "Input view excludes undeclared fields (tested)", "Invalid skills fail at load time"],
      (1, 1.5, 2.5), "spec")
story("E-core-llm", "Backend interface and anthropic backend",
      "Backend protocol (request, response, usage, cost) and the Anthropic API backend with model aliases.",
      ["Anthropic backend passes recorded-response tests", "Usage and cost recorded per call", "API key read from a declared secret only"],
      (1, 1.5, 2.5), "normal")
story("E-core-llm", "claude-cli backend with isolation",
      "Backend that runs the user's own `claude -p --output-format json` with an env allowlist, built-in tools disabled and no user settings; documented as personal use under the user's plan terms.",
      ["The CLI runs with tools disabled and without user/project settings (tested with a fake binary)", "Only allowlisted env vars reach the process", "Docs explain the terms and recommend the API backend for published setups"],
      (1, 2, 3), "sec")
story("E-core-llm", "openai-compatible and ollama backends",
      "Backends for OpenRouter/Mistral/vLLM-style endpoints and local Ollama.",
      ["Both pass the shared backend test suite with recorded responses", "Base URL and key configurable per alias"],
      (1, 1.5, 2), "normal")
story("E-core-llm", "Schema-validated output with one repair retry",
      "Validate model output against the skill schema; one repair attempt with the validation errors; otherwise mark the run degraded and apply nothing from this step.",
      ["Invalid output is never applied (tested)", "Repair prompt includes only validation errors, not new data", "Degraded status visible in `horae runs`"],
      (0.5, 1, 2), "contract")
story("E-core-llm", "Input-hash cache and the $0 path",
      "Cache key = skill version + model + normalized input; skip the LLM call when input is unchanged.",
      ["Unchanged input makes no LLM call (tested with a counting fake backend)", "Cache entries expire by policy", "`--no-cache` flag for debugging"],
      (1, 1.5, 2.5), "contract")
story("E-core-llm", "Budgets and usage ledger",
      "Per-step and per-day token/cost ceilings; usage ledger queryable from the CLI.",
      ["A step over budget is stopped and marked degraded", "Daily ceiling across runs enforced", "`horae usage` shows cost per pipeline and day"],
      (1, 1.5, 2), "normal")
story("E-core-llm", "De-identification transform",
      "Pseudonymize declared fields (e.g. names to `kid:A`, amounts to buckets) before any cloud backend; re-identify when rendering.",
      ["Declared fields never reach a cloud backend in clear (tested by inspecting the request)", "Mapping is stable within a run and stored locally only", "Rendered output uses real values again"],
      (1, 2, 3), "sec")
story("E-core-llm", "Untrusted-content tagging and action guard",
      "Tag all externally sourced text as untrusted; the LLM can fill schema fields but cannot add actions, recipients or URLs absent from the input set.",
      ["Prompt-injection fixtures cannot add an intent or a URL (tested)", "Untrusted text is fenced as data in prompts", "Violations mark the run degraded and are logged"],
      (1.5, 2, 4), "sec")
story("E-core-llm", "Skill evaluation harness",
      "Golden tests per skill with recorded responses in CI and an on-demand live mode that reports schema validity, cost and diffs.",
      ["`horae eval <skill>` runs recorded cases offline", "`--live` runs against a backend and writes a report", "Used by pack-daily-brief"],
      (1, 1.5, 2.5), "normal")

story("E-core-runtime", "HTTP API: /healthz and /runs",
      "Small FastAPI app exposing health and read-only run information.",
      ["`/healthz` reports store and scheduler state", "`/runs` paginated, read-only", "No secrets in responses (tested)"],
      (0.5, 1, 1.5), "normal")
story("E-core-runtime", "Signed single-use action links (/act)",
      "HMAC-signed tokens bound to one intent (e.g. task done, snooze), single use, expiring after 24 h, with replay protection.",
      ["A token works once; reuse and expired tokens are rejected (tested)", "Tokens carry no secrets and no free-form URLs", "Rate limiting on the endpoint"],
      (1, 1.5, 2.5), "sec")
story("E-core-runtime", "Scheduler and external triggers",
      "Built-in cron for plain Docker, or external trigger mode (`horae run` from Coolify Scheduled Tasks, cron, systemd); optional push-monitor ping per run.",
      ["Cron expressions per pipeline in horae.yaml", "`HORAE_SCHEDULER=external` disables the internal cron", "Push monitor URL pinged with up/down status"],
      (1, 1.5, 2.5), "normal")
story("E-core-runtime", "Container image and GHCR publishing",
      "Multi-stage Dockerfile (non-root, read-only root fs friendly), published to ghcr.io/horae-dev/horae with SBOM.",
      ["Image runs as non-root and passes a smoke test in CI", "Tags `edge` on main and semver on releases", "SBOM attached to the image"],
      (0.5, 1, 1.5), "docs")
story("E-core-runtime", "Coolify template and plain compose",
      "`deploy/coolify/docker-compose.yaml` in Coolify template format (magic variables, required `${VAR:?}`), plus `deploy/compose/` for plain Docker, with install docs.",
      ["Template deploys on a fresh Coolify and passes its healthcheck", "Required variables block deployment until set", "Install docs cover Scheduled Tasks and push monitors"],
      (1, 1.5, 2.5), "integ")
story("E-core-runtime", "Plugin SDK (Python)",
      "`horae-sdk`: plugin base classes, manifest helpers, intent builders and a recorded-input test harness.",
      ["A minimal plugin is under 50 lines with the SDK", "Test harness replays recorded inputs", "SDK published from the core repo"],
      (1.5, 2.5, 4), "contract")

story("E-core-market", "Marketplace resolution",
      "Resolve plugin names across configured marketplace indexes (any git URL), with a local cache.",
      ["Multiple indexes supported, order defines priority", "Clear error on name conflicts", "Works offline from cache"],
      (1, 1.5, 2), "normal")
story("E-core-market", "Install from git with pinned commit",
      "`horae plugin add name@version`: git clone, checkout the SHA pinned in the index, refuse when tag and SHA disagree.",
      ["Tampered tag or SHA mismatch is refused (tested)", "Installed plugins live under the instance plugin dir", "Install is atomic (no half-installed plugin)"],
      (1, 1.5, 2.5), "sec")
story("E-core-market", "Per-plugin virtual environments",
      "Install each plugin's dependencies in its own uv venv so plugins never share dependencies.",
      ["Two plugins with conflicting deps both work", "Venvs rebuilt on upgrade", "No network access needed at run time"],
      (1, 1.5, 2.5), "normal")
story("E-core-market", "Permission approval and lockfile",
      "Show requested permissions at install, require approval, write `horae.lock` with name, version, SHA and approved permissions.",
      ["Install without approval is refused unless `--yes` with matching lock", "Lockfile is deterministic and diff-friendly", "Run refuses plugins whose permissions differ from the lock"],
      (1, 1.5, 2), "sec")
story("E-core-market", "Upgrade with permission diff, remove and audit",
      "`upgrade` shows a permission diff and needs re-approval for new hosts, secrets or intents; `remove`; `audit` lists plugins, permissions and last activity.",
      ["New permissions require approval (tested)", "`audit` output in table and JSON", "Remove cleans venv and lock entry"],
      (1, 2, 3), "sec")
story("E-core-market", "Direct install from a git URL",
      "`horae plugin add https://...@<sha>` without any marketplace.",
      ["Requires a full SHA", "Same permission flow as marketplace installs"],
      (0.5, 0.5, 1), "normal")

story("E-core-isolation", "Per-plugin container runtime",
      "Run each plugin in its own container (MCP over stdio), on an `internal: true` network, as non-root with read-only root fs.",
      ["Plugins run in containers when `runtime: container`", "Startup and call latency documented", "Works on a 4-core / 16 GB home server"],
      (2, 3, 5), "sec")
story("E-core-isolation", "Egress allow-list proxy",
      "HTTP(S) proxy generated from `permissions.network`; the only egress path for plugin containers.",
      ["Undeclared host is blocked (tested)", "Declared host works for HTTPS (CONNECT)", "Blocked attempts are recorded in the run"],
      (2, 3, 5), "sec")
story("E-core-isolation", "Declared mounts and resource limits",
      "Mount only declared paths (read-only unless declared writable); CPU/memory limits per plugin.",
      ["Undeclared path access fails (tested)", "Limits configurable per plugin"],
      (1, 1.5, 2.5), "sec")
story("E-core-isolation", "Malicious-plugin test suite",
      "One fixture plugin per guarantee: undeclared host, secret, path or intent; prompt injection trying to change actions.",
      ["Every malicious fixture fails in CI", "Suite documented in docs/security.md"],
      (2, 3, 4), "sec")
story("E-core-isolation", "Secret redaction in logs and run records",
      "Redact declared secret values from logs, stderr captures and run records.",
      ["Seeded secrets never appear in logs or the store (tested)"],
      (0.5, 1, 1.5), "sec")

story("E-core-mcp", "Read-only MCP tools",
      "MCP server exposing `tasks.today`, `brief.latest`, `runs.recent`.",
      ["Tools return schema-valid JSON", "Server works with Claude Code as a client (manual check documented)"],
      (1, 1.5, 2), "normal")
story("E-core-mcp", "Approval-gated pipeline.run",
      "`pipeline.run` produces a dry-run and requires an explicit approval before applying.",
      ["Nothing is applied without approval (tested)", "Approval expires"],
      (1, 1.5, 2.5), "sec")
story("E-core-mcp", "MCP server authentication",
      "Token-based auth for the MCP server, tokens stored hashed.",
      ["Requests without a valid token are rejected", "Tokens revocable from the CLI"],
      (0.5, 1, 1.5), "sec")

story("E-core-release", "Docs site",
      "MkDocs site: concepts, quick start (Docker, Coolify), writing a plugin, security model, CLI reference.",
      ["Published to GitHub Pages from main", "Quick start tested on a clean machine"],
      (1.5, 2.5, 4), "docs")
story("E-core-release", "Release automation and signed artifacts",
      "Semver releases with changelog, PyPI trusted publishing, cosign-signed images.",
      ["Tag push publishes PyPI package and signed image", "Changelog generated from PRs"],
      (1, 1.5, 2), "docs")
story("E-core-release", "Propose the Coolify one-click template upstream",
      "Prepare and submit the service template to Coolify following their contribution rules.",
      ["Template PR opened upstream", "Docs link to the template"],
      (0.5, 1, 2), "integ")
story("E-core-release", "v0.1 release",
      "Release checklist, stability promise for 0.x plugin API, announcement.",
      ["Someone other than the maintainer installs Horae from the docs alone", "v0.1.0 tagged and published"],
      (0.5, 1, 1.5), "human")

# Human operations (human-only)
story("E-human-ops", "Install the Graft GitHub App on horae-dev",
      "Install Graft on the organization, add repositories, then raise the mode ceiling and enable enforce mode per repository once the pilot is stable.",
      ["Graft comments and checks appear on PRs in all repos", "Mode decision recorded (report or enforce) per repo"],
      (0.25, 0.5, 1), "human", "M0")
story("E-human-ops", "Organization security settings",
      "Require 2FA, enable dependency graph, Dependabot alerts, secret scanning and push protection for new repositories; restrict repo deletion and visibility changes.",
      ["Settings applied and verified on one repo"],
      (0.25, 0.25, 0.5), "human", "M0")
story("E-human-ops", "Model provider keys and spend limits",
      "Create an Anthropic API key (or set up claude-cli for personal use) with a monthly spend limit, store it as a secret.",
      ["Key works in CI live-eval workflow (manual trigger)", "Spend limit set"],
      (0.25, 0.25, 0.5), "human", "M2")
story("E-human-ops", "Reference server: Coolify resources",
      "Create the vault-sync (Obsidian LiveSync one-click) and horae resources, env vars, Scheduled Tasks and push monitors on the reference server.",
      ["Daily brief and reminders run on schedule", "Missed run raises an alert"],
      (0.5, 1, 2), "human", "M3")
story("E-human-ops", "Device tests: Obsidian LiveSync and ntfy on iPhone",
      "Configure LiveSync on desktop and phone; verify ntfy deep links (`obsidian://`) and action buttons on iOS; report results in the ntfy and livesync issues.",
      ["Two-week dogfood log with conflicts and notification issues recorded"],
      (0.5, 1, 2), "human", "M3")
story("E-human-ops", "PyPI and GHCR publishing setup",
      "Reserve the PyPI name `horae`, configure trusted publishing, make GHCR packages public.",
      ["Test release publishes successfully"],
      (0.25, 0.5, 1), "human", "M8")

# ---------------- marketplace & template ----------------
story("E-market", "marketplace.json schema and initial entries",
      "Schema for the index (name, repo, versions -> commit SHA, tier) and entries for first-party plugins.",
      ["Schema validated in CI", "All first-party plugins listed with pinned SHAs"],
      (0.5, 0.5, 1), "docs")
story("E-market", "Verification CI for every entry",
      "For each entry: clone, verify the pinned SHA and tag, validate plugin.yaml, run the plugin tests against the latest core.",
      ["A tampered SHA fails CI", "Nightly run against core `main` reports breakages as issues"],
      (1, 2, 3), "integ")
story("E-market", "Contribution guide and review policy for third-party plugins",
      "How to submit a plugin, what reviewers check (permissions, tests, licence), and tiers (first-party, community).",
      ["Guide published", "PR template for new entries"],
      (0.5, 1, 1.5), "docs")
story("E-template", "Template repository",
      "Repository template with plugin.yaml, an MCP stdio server built on horae-sdk, recorded-input tests, CI, graft.yaml and AGENTS.md.",
      ["'Use this template' produces a repo whose CI passes unchanged", "Template documented in the core docs"],
      (1, 1.5, 2), "normal")
story("E-template", "Plugin authoring checklist",
      "Checklist covering permissions, secrets, recorded fixtures, degraded handling and docs.",
      ["Checklist linked from the template README and PR template"],
      (0.25, 0.5, 0.5), "docs")

# ---------------- plugins ----------------
story("E-obsidian", "Parse Obsidian Tasks syntax from the vault",
      "Read tasks with status, 📅 due, ⏰ reminder, ✅ done date, tags and hidden ids from Markdown files.",
      ["Parser handles the Obsidian Tasks emoji format with fixtures", "Unknown syntax is preserved, never rewritten"],
      (1, 1.5, 2.5), "normal")
story("E-obsidian", "Owned files and blocks writer",
      "Apply `note.create`, `note.append_section`, `note.replace_owned_block` only to files or blocks the plugin owns; atomic writes.",
      ["Writes outside owned files/blocks are refused (tested)", "Atomic temp-file-and-rename writes"],
      (1, 1.5, 2.5), "contract")
story("E-obsidian", "open_tasks source and task status sink",
      "Expose open tasks as a source and apply `task.update_status` (e.g. from an /act link).",
      ["Ticking a task through the sink edits only that line", "Task ids stable across runs"],
      (0.5, 1, 1.5), "normal")
story("E-obsidian", "Daily note rendering templates",
      "Templates for the assistant note: sections by source, tasks by due date, alerts.",
      ["Rendering is deterministic for the same input", "Template overridable per instance"],
      (0.5, 1, 1.5), "spec")
story("E-livesync", "livesync-cli daemon sidecar",
      "Run the official Self-hosted LiveSync CLI in daemon mode to mirror CouchDB into a plain-file vault folder; generate its config from horae.yaml.",
      ["Two-way sync between a test CouchDB and the folder works", "Health exposed to Horae"],
      (1, 2, 3), "ext")
story("E-livesync", "E2EE parity check with the Obsidian plugin",
      "Verify that encryption and path obfuscation settings of the CLI match the Obsidian plugin, on real devices.",
      ["Notes written on the server open on desktop and phone with E2EE on", "Findings documented"],
      (1, 1.5, 3), "ext")
story("E-livesync", "Nightly git snapshot of the vault",
      "Commit the vault folder nightly into a local git repo for readable history and recovery.",
      ["Snapshot job documented and tested", "Restore procedure documented"],
      (0.5, 1, 1.5), "normal")
story("E-livesync", "Setup and recovery docs",
      "CouchDB setup (Coolify one-click), plugin settings, conflict handling and recovery.",
      ["Docs cover setup from zero to synced phone"],
      (0.5, 1, 1.5), "docs")
story("E-ntfy", "notify.push notifier",
      "Send notifications with topics, priorities, tokens, click URLs and quiet hours.",
      ["Quiet hours respected except for critical", "Tokens are declared secrets"],
      (1, 1.5, 2), "normal")
story("E-ntfy", "Reminder state and deduplication",
      "Track what was sent so a reminder is sent once per rule and day.",
      ["No duplicate pushes across runs (tested)"],
      (0.5, 1, 1.5), "normal")
story("E-ntfy", "Deep links and action buttons",
      "`obsidian://` click links and Done/Snooze buttons calling signed /act links.",
      ["Buttons call /act and the vault updates", "Fallback to view actions documented if iOS http actions don't work"],
      (1, 1.5, 2.5), "integ")
story("E-github", "Review requests, assignments and mentions",
      "Sources for PRs awaiting review, assigned issues/PRs and mentions.",
      ["Each item becomes a task with a link and due policy", "Recorded fixtures for tests"],
      (1, 1.5, 2), "normal")
story("E-github", "Participating notifications",
      "Source using the notifications API (classic PAT, participating only).",
      ["Read state not modified", "Rate limits respected"],
      (0.5, 1, 1.5), "normal")
story("E-github", "Docs and permissions",
      "Document required token scopes and the declared permissions.",
      ["README lists scopes and permissions"],
      (0.25, 0.5, 1), "docs")
story("E-calendar", "ICS URL source",
      "Read ICS URLs (e.g. Proton share links, Google secret address) with recurrence expansion.",
      ["Recurring events expanded correctly over the window", "Fixtures for tests"],
      (1, 1.5, 2), "normal")
story("E-calendar", "CalDAV source",
      "CalDAV source (iCloud with app-specific password and others).",
      ["Works against a CalDAV test server in CI", "Manual check against iCloud documented"],
      (1, 1.5, 2.5), "ext")
story("E-calendar", "Google Calendar API source",
      "OAuth-based Google Calendar source with token refresh.",
      ["Token refresh handled", "Setup docs for the OAuth app in production mode"],
      (1, 1.5, 2.5), "ext")
story("E-calendar", "Preparation tasks transform",
      "Rules that turn events needing preparation into tasks due the day before.",
      ["Rules configurable; examples tested"],
      (0.5, 1, 1.5), "normal")
story("E-brief", "Daily brief pipeline and rank-and-word skill",
      "Pipeline definition and the skill (prompt, input view, output schema) that ranks and words tasks for the morning note.",
      ["Pipeline runs end to end with fixture plugins", "Skill output schema-valid on all golden cases"],
      (1, 1.5, 2.5), "spec")
story("E-brief", "Golden tests and evals",
      "Recorded cases covering typical and edge mornings (nothing new, many items, conflicting sources).",
      ["`horae eval` passes offline in CI", "Live eval report attached to releases"],
      (1, 1, 2), "normal")
story("E-remind", "Reminder pipeline",
      "Every 5 minutes, without LLM: morning digest, ⏰ reminders, one overdue nudge per day, quiet hours.",
      ["Ticked tasks never remind (tested)", "No LLM call ever (tested)"],
      (1, 1.5, 2.5), "normal")
story("E-remind", "Reminder tests",
      "Time-travel tests across days and quiet hours.",
      ["Tests cover DST changes and quiet hours"],
      (0.5, 1, 1.5), "normal")
story("E-mcpbrief", "brief() contract",
      "JSON Schema and docs for services that want to feed Horae (headline, alerts, tasks, links, status).",
      ["Schema published and versioned", "Example service in the docs"],
      (0.5, 1, 1.5), "spec")
story("E-mcpbrief", "MCP brief client source",
      "Call brief() over streamable HTTP with auth and timeouts; degraded status on failure.",
      ["Timeouts and errors give degraded, never failed runs", "Works with two real services"],
      (1, 1.5, 2.5), "integ")
story("E-mcpbrief", "Reference integrations",
      "Wire two existing MCP services as references and document the integration.",
      ["Two services produce sections in the morning note"],
      (1, 2, 3), "integ")
story("E-imap", "Generic IMAP source",
      "IMAP source with folder selection, since-state and rules-first extraction of requests and dates.",
      ["Works against a test IMAP server in CI", "Never marks mail as read"],
      (1, 1.5, 2.5), "normal")
story("E-imap", "Proton Mail Bridge recipe",
      "Container recipe and docs for running Proton Mail Bridge headless next to Horae.",
      ["Manual end-to-end check documented"],
      (0.5, 1, 2), "ext")
story("E-gmail", "Gmail OAuth and token lifecycle",
      "OAuth setup (production publishing to avoid 7-day tokens), refresh and minimal scopes.",
      ["Read-only scopes by default", "Token refresh tested"],
      (1, 1.5, 3), "ext")
story("E-gmail", "Rules-first triage into tasks",
      "User rules first, then the core LLM step only for mail that needs interpretation; requests become task intents.",
      ["No LLM call when rules decide", "Golden cases for common mail types"],
      (1.5, 2.5, 4), "normal")
story("E-school", "PRONOTE source",
      "pronotepy with QR-token login, credential persistence after every login, rate limits, read-only.",
      ["Never more than the configured polls per day", "Credential rotation persisted", "Degraded status on any login failure, no retry loop"],
      (1.5, 2.5, 4), "ext")
story("E-school", "ÉcoleDirecte source",
      "ecoledirecte library with security-question handling and trusted-device (cn/cv) persistence, read-only.",
      ["Trusted device persisted; no repeated quizzes", "Degraded status on failure, no retry loop"],
      (1.5, 2.5, 4), "ext")
story("E-school", "Normalization and de-identification",
      "Homework, notes and signatures become tasks; child names declared as de-identified fields.",
      ["Names never reach a cloud backend in clear (tested with core)"],
      (0.5, 1, 1.5), "normal")
story("E-school", "Lockout-safe scheduling",
      "Polling windows and backoff rules that avoid account lockouts.",
      ["Documented limits and tests for backoff"],
      (0.5, 1, 1.5), "normal")
story("E-ha", "Home Assistant notifier",
      "Send notifications through HA notify services.",
      ["Works with a recorded HA API fixture"],
      (0.5, 1, 1.5), "normal")
story("E-ha", "Household reminders source",
      "Turn HA sensors and calendars into tasks (e.g. bins tonight).",
      ["Configurable entity mapping with tests"],
      (1, 1.5, 2), "normal")

RISKS = [
    ("Scope creep: product and personal tool at once", "high", "M0-M8",
     "Building a public product and a personal assistant at the same time can stall both.",
     "M3 dogfooding comes before marketplace and isolation; non-goals in PLAN §2.3; every feature must be used by the reference instance."),
    ("Supervisor bandwidth is the real bottleneck", "high", "M0-M8",
     "Agents produce PRs faster than one part-time human can specify and review them (industry data: review time up 2-5x with agents).",
     "Graft policy auto-approves trivial and small logic changes; specs written as acceptance criteria up front; at most 3-4 agents in parallel; recalibrate estimates after 15 stories."),
    ("Security-critical code written by agents", "high", "M1, M4, M5",
     "Permissions, sandboxing, signed links and secrets are where AI code fails most (studies: ~45% insecure choices) and mistakes are costly.",
     "These stories are human-led (agent drafts, human designs and reviews), sensitive paths in graft.yaml, malicious-plugin suite in CI."),
    ("Claude terms for the claude-cli backend", "medium", "M2",
     "Anthropic allows personal headless use of claude -p but not third-party products offering claude.ai login.",
     "Horae never handles OAuth; API backend is the documented default; re-check terms before v0.1."),
    ("LiveSync CLI encryption parity and sync conflicts", "medium", "M3",
     "CLI encryption docs are thin; conflicts between server writes and phone edits are possible.",
     "Plugin owns separate files/blocks only; parity test on real devices; nightly git snapshot; Obsidian Sync headless as fallback."),
    ("iOS behaviour of ntfy action buttons and deep links unverified", "medium", "M3",
     "ntfy docs do not say whether http actions and obsidian:// links work on iOS.",
     "Device test task in M3; fallback to view actions that open the note."),
    ("Unofficial school APIs break or lock accounts", "medium", "M7",
     "PRONOTE/ÉcoleDirecte libraries are reverse-engineered; repeated logins can lock accounts.",
     "Read-only, rate-limited, degraded status, separate plugin; core never depends on it."),
    ("Coolify cross-resource networking", "low", "M3, M6",
     "Reaching other services by container name across Coolify resources may not be stable across redeploys.",
     "Spike in M3; fallback to an internal route with a bearer token and IP allow-list."),
    ("MCP specification churn", "low", "M1, M5",
     "MCP SDK and spec changes can break the plugin host.",
     "Pin the SDK; keep the plugin host behind an internal interface."),
    ("Trademark: French marks in class 42", "low", "M8",
     "Several French national marks contain HORAE, two in class 42.",
     "Open source, no commercial use or filing; professional clearance before any commercial step (PLAN §10.1)."),
]
