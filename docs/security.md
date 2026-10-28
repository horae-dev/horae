# Security model and threat model

- **Issue:** [#14](https://github.com/horae-dev/horae/issues/14) (M0). Supersedes the design note
  [0014](design/0014-threat-model-note.md); the maintainer's answers to its questions are on
  [PR #84](https://github.com/horae-dev/horae/pull/84).
- **Sources:** [PLAN.md](PLAN.md) §2, §3.3–3.5, §4, §7, §9, §10;
  [ADRs 0001–0006](adr/README.md); [schemas decisions](../schemas/README.md#decisions); the code in
  `src/horae/` and its tests.
- **Reporting a vulnerability:** see [Reporting a vulnerability](#reporting-a-vulnerability). Do not
  open a public issue.

This document says what Horae protects, from whom, and how: every threat names its control (code,
ADR or schemas decision), the test that pins it (an existing test file, or the issue that must add
one), the enforcement tier, and whether it is implemented, planned or an accepted risk. A control
without a test is not counted as implemented.

## 1. Scope and security promise

Horae is a **single-user**, self-hosted runtime ([PLAN §2.3](PLAN.md): no multi-tenancy, no SaaS).
The promise of [PLAN §2.2](PLAN.md) is what this model defends:

- control flow is declared, never chosen by an LLM; the LLM step has no tools
  ([ADR 0001](adr/0001-pipelines-not-agent-loops.md));
- plugins and the LLM never write; they propose intents from a closed set, which the core
  validates, diffs, journals and applies through a sink
  ([ADR 0002](adr/0002-intents-applied-by-the-core.md));
- a plugin gets only what its manifest declares, approved at install and pinned in `horae.lock`
  ([ADR 0003](adr/0003-mcp-as-the-plugin-protocol.md),
  [ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md));
- untrusted content stays data: it can fill fields, never choose an action, a recipient or a URL
  that was not already in the input set (PLAN §2.1, tenet 5).

## 2. Method

**STRIDE per trust boundary** (confirmed on PR #84), with assets and attacker models as front
matter.

- Horae's controls are boundary-shaped (plugin host, LLM backend, `/act`, marketplace client,
  container runtime), so each boundary is walked once with STRIDE (Spoofing, Tampering,
  Repudiation, Information disclosure, Denial of service, Elevation of privilege).
- Every row has a **tier**: **D** = *declared* (M1–M4: subprocess plugins, clean environment,
  intents checked against the manifest) or **I** = *isolated* (M5+: one container per plugin,
  egress proxy, declared mounts, read-only, non-root). The residual risk before M5 is written
  down, not implied.
- Privacy threats (LLM inputs sent to a cloud backend, run-record retention) are Information
  disclosure rows; a separate LINDDUN pass was not worth it.
- **Status** is one of: **implemented** (code and a test on `main`), **planned #N** (the story
  that delivers it, with the acceptance criterion to add listed in
  [§10](#10-story-updates-requested) when it is missing), **accepted risk** (see
  [§7](#7-accepted-risks)).

## 3. Assets

| ID | Asset | Where it lives |
|---|---|---|
| A1 | Plugin secrets (`GITHUB_TOKEN`, CalDAV app passwords, IMAP credentials) | `/run/secrets` files (default) or explicit `{env: VAR}` references ([decision 25](../schemas/README.md#decisions)) |
| A2 | LLM credentials: API keys, `CLAUDE_CODE_OAUTH_TOKEN` or the user's logged-in Claude Code | secrets, Claude Code config ([ADR 0005](adr/0005-claude-cli-backend-and-its-terms.md)) |
| A3 | `/act` HMAC key | environment (PLAN §3.5) |
| A4 | Vault content (notes, tasks): personal and family data | vault mount, LiveSync CouchDB |
| A5 | Store: run records with LLM inputs, journal with note bytes before and after, LLM cache, task index, notification state | `<state_dir>/horae.db` ([ADR 0006](adr/0006-sqlite-as-the-store.md)) |
| A6 | Instance definition: `horae.yaml`, `pipelines/`, `horae.lock` (approved permissions) | instance repo, state |
| A7 | Integrity of what runs: Horae image, plugin code at a pinned commit, plugin venvs | GHCR, git, plugin directory |
| A8 | Outbound channels: ntfy topics, `http.post` targets, the MCP server | network |
| A9 | The host: home server, Docker, Coolify, other resources on shared networks | PLAN §7 |
| A10 | Budget: LLM spend per step and per day | provider account |

## 4. Trust boundaries

```mermaid
flowchart LR
  ext["External content<br/>(mail, notes, web, calendars)"] -->|B1 untrusted data| src
  subgraph host["Home server / Coolify (B7)"]
    subgraph core["Horae core (trusted)"]
      eng["Engine + intent validation + routing"]
      store[("SQLite store")]
      api["HTTP API /act, /runs"]
      mcp["MCP server (M5)"]
    end
    src["Plugin processes<br/>(subprocess M1-M4, container M5+)"]
    vault[("Vault")]
  end
  src <-->|B2 MCP stdio, declared permissions| eng
  eng -->|B3 prompt out, JSON in| llm["LLM backend<br/>(anthropic, claude-cli, ...)"]
  eng -->|sinks apply intents| vault
  phone["Phone (ntfy buttons)"] -->|B4 /act token| api
  client["MCP clients"] -->|B5 token auth| mcp
  mkt["Marketplace + plugin git repos"] -->|B6 install, SHA pin| src
  gh["Horae repo, CI, GHCR, PyPI"] -->|B8 supply chain| core
```

| Boundary | Between | Choke point |
|---|---|---|
| B1 | External content → plugins, LLM, notes | `horae.intents` validation, engine routing (`horae.engine.executor`), sinks |
| B2 | Plugin process ↔ core | plugin host (#24), `horae.intents.validate` |
| B3 | Core ↔ LLM backend | backend adapters (#28–#30), schema validation (#31) |
| B4 | Phone ↔ `/act` | `/act` endpoint (#38), SSO middleware (#41) |
| B5 | MCP clients ↔ Horae's MCP server | MCP server auth (#56) |
| B6 | Marketplace and git → installed plugin | `horae plugin add` (#43–#48), `horae.lock` |
| B7 | Horae ↔ host (Docker, Coolify, neighbours, backups) | compose files (#41), container runtime (#49) |
| B8 | Horae's own supply chain → image | CI (#11), image build (#40), release (#58) |

## 5. Attacker models

| ID | Attacker | Capability | Goal |
|---|---|---|---|
| M1 | Content author | Writes text Horae reads (email, shared calendar, web page, a note) | Prompt injection: add an action, recipient or URL; break out of a field; exfiltrate via a link |
| M2 | Malicious or compromised plugin | Arbitrary code inside a plugin process, within its declared manifest | Steal secrets, reach undeclared hosts or paths, propose undeclared intents |
| M3 | Marketplace or git-host compromise | Can change `marketplace.json`, a tag, a repo | Get malicious code installed or upgraded |
| M4 | Network or notification-path observer | Sees ntfy payloads, push relays, logs, link previews | Replay or forge `/act` tokens; read data in transit |
| M5 | Upstream dependency compromise | Malicious release of a Python package, action or base image | Code execution in CI or in the Horae image |
| M6 | Neighbour on the host | Another Coolify resource or container on a shared network | Reach Horae's API, store or plugins |
| M7 | Curious LLM provider or provider breach | Sees prompts and responses | Disclosure of personal data (A4) |

**Not defended against:** a malicious operator or root on the host; multi-tenant attacks (single
user, PLAN §2.3); physical access to the server.

## 6. Threats per boundary

Columns: **Threat → Control** (where it lives) **→ Test** (existing test file, or the issue that
must add it) **→ Tier → Status**. IDs `T01`–`T29` keep the numbering of design note 0014; `T30`
and later are new.

### B1: untrusted content → plugins, LLM, notes

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T01 | T, E | Injected text makes the LLM or a plugin add an intent, a recipient or a URL | Closed intent set ([ADR 0002](adr/0002-intents-applied-by-the-core.md)); at most one tool-less `llm` step ([ADR 0001](adr/0001-pipelines-not-agent-loops.md), [decision 14](../schemas/README.md#decisions)); routing binds every target to a trusted `act`/`notify` entry, untrusted data never chooses between entries (`intent_ambiguous`), `task.update_status` only for a fingerprint present in the collected input ([decision 30](../schemas/README.md#decisions), `horae.engine.executor`); LLM output schema-validated with one retry, else `degraded` (#31); untrusted tagging and action guard (#35) | `tests/test_engine.py` (`test_explicit_target_must_match_the_binding`, `test_task_status_needs_a_collected_fingerprint`, `test_two_notifiers_for_the_same_type_are_ambiguous`), `tests/test_contracts.py`; #31, #35, #52 (injection fixture) | D | implemented (routing, closed set); planned #31, #35, #52 |
| T02 | T | Untrusted text breaks out of its field in the note: owned-block markers, line breaks in headings, Obsidian Tasks signifiers (`📅 ⏰ ✅ 🔁`), terminal control characters in `horae diff` | Note paths reject `.` segments ([decision 4](../schemas/README.md#decisions)); `horae.apply.notes.check_text` refuses markers in content and markers or line breaks in headings; `horae.apply.render.safe` escapes Cc, Cf, Zl, Zp in diff output; notification links `https://` or `obsidian://` only ([decision 7](../schemas/README.md#decisions)) and only under a trusted `link_prefix` ([decision 30](../schemas/README.md#decisions)); Tasks signifier escaping in sinks (schemas README, "Contracts constrain shape") | `tests/test_apply.py` (`test_untrusted_content_cannot_hold_a_marker`, `test_untrusted_headings_cannot_hold_a_marker_or_a_line_break`, `test_text_output_escapes_untrusted_control_characters`), `tests/test_engine.py` (`test_notification_links_need_a_trusted_prefix`); signifiers: #42 | D | implemented (markers, paths, links, terminal); planned #42 (signifier escaping) |
| T30 | T, E | A note intent writes outside the vault or into a note Horae does not own (symlink, `..`, hidden file, someone else's note) | Owned notes only: `*.assistant.md`, decided on the path resolved without following symlinks ([decision 32](../schemas/README.md#decisions) D2); `LocalSink` / `LocalFileSink` open every segment `O_NOFOLLOW` relative to the parent descriptor, compare-and-swap on the planned SHA-256 (`src/horae/apply/local.py`) | `tests/test_apply.py` (`test_only_owned_notes_are_created_or_appended_to`, `test_local_sink_never_follows_symlinks`), `tests/test_local_file_sink.py` | D | implemented |

### B2: plugin process ↔ core

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T03 | I | A plugin reads the secrets of another plugin or of the core | Secrets resolved per name from `/run/secrets/<NAME>` only, env only through an explicit `{env: VAR}` reference ([decision 25](../schemas/README.md#decisions), `horae.secrets.SecretResolver`); secret names reject loader variables and `HORAE_` ([decision 8](../schemas/README.md#decisions)); clean child environment with only declared secrets and the full deny list (`NODE_OPTIONS`, `*_PROXY`, `GIT_*`…) owned by the host ([decision 28](../schemas/README.md#decisions), #24) | `tests/test_secrets.py` (`test_default_lookup_is_the_secrets_directory_only`, `test_invalid_names_are_refused_before_any_lookup`); probe plugin: #24 | D | implemented (resolution); planned #24 (clean environment) |
| T04 | I | Before M5 a plugin, running as the same uid, reads `/run/secrets`, the store, other venvs or `horae.yaml` from the filesystem | **Refuse non-first-party plugins until M5** (maintainer decision, PR #84): only marketplace entries with `tier: first-party` (PLAN §4) install while the runtime is `subprocess`; declared mounts from M5 (#51) | #46, #48 (refusal); #51, #52 (mounts) | D→I | planned #46, #48; planned #51 |
| T05 | I | A plugin exfiltrates to an undeclared host | Egress only through the allow-listing proxy built from `permissions.network` (M5); before M5, T04's refusal limits this to first-party code | #50, #52 | I | planned #50; accepted risk in D tier (first-party only) |
| T06 | E | A plugin proposes an intent it did not declare, or `http.post` to an undeclared host | `horae.intents.validate`: every intent checked against `permissions.intents`, `http.post` host checked with the same parsed host (`horae.contracts.url_host`) as the manifest; one invalid intent rejects the batch; permissions checked before routing | `tests/test_intent_validation.py` (`test_undeclared_intent_fails_the_step`, `test_http_post_to_undeclared_host_fails`, `test_host_undeclared_names_the_parsed_host`), `tests/test_engine.py` (`test_permissions_are_checked_before_routing`); #52 | D | implemented |
| T07 | I | `http.post` to a declared name that resolves to a private or link-local address (SSRF, DNS rebinding); a redirect to an undeclared host | URLs are `https` only, no IP literals or `localhost` ([decision 6](../schemas/README.md#decisions)); resolution through the egress proxy blocking private ranges and no redirects (schemas README, "Contracts constrain shape") | `tests/test_contracts.py` (URL rules); #50 | D/I | implemented (contract); planned #50 (private ranges, see [§10](#10-story-updates-requested)) |
| T08 | D | A plugin hangs, floods its output or crashes the run | Run timeout `runs.timeout_s` with a 5 s grace ([decision 31](../schemas/README.md#decisions)); every sink call bounded by `call_timeout_s` ([decision 34](../schemas/README.md#decisions)); stderr capped at 64 KiB per step (`horae.engine.executor`); intent payloads bounded (`MAX_INTENTS`, `MAX_BODY`, nesting 64); tool-call timeouts (#24); CPU and memory limits (#51) | `tests/test_runner.py` (`test_run_timeout_stops_the_step_and_marks_the_run`), `tests/test_undo_hardening.py` (`test_a_sink_call_past_its_timeout_stops_the_apply`), `tests/test_engine.py` (`test_stderr_is_shared_between_calls_head_and_tail`), `tests/test_intent_validation.py` (`test_huge_payloads_are_rejected_fast`); #24, #51 | D/I | implemented (run, sink, size bounds); planned #24, #51 |
| T09 | I | Declared secret values land in logs, stderr captures, error messages or run records | `horae.secrets.Secret` redacts `repr`, `str`, `format` and serialization, refuses pickling and copying into a subclass; resolution logs nothing; errors name the secret, never the value; `horae doctor` prints only the exception type on an internal error ([decision 27](../schemas/README.md#decisions)) and never echoes a key that may be a pasted secret ([decision 29](../schemas/README.md#decisions)); a crashed run records the exception type only ([decision 31](../schemas/README.md#decisions)); stderr redaction (#24) and redaction everywhere (#53, **moved to M1/M2**) | `tests/test_secrets.py` (`test_secret_never_shows_its_value`, `test_resolution_logs_nothing`, `test_bad_secret_files_fail_without_keeping_the_value`), `tests/test_doctor.py`; #24, #53 (seeded secrets) | D | implemented (`Secret` type, doctor, runner); planned #24, #53 |
| T31 | T | A source plugin has side effects during `horae run --dry-run` (it writes or posts itself) | A dry run calls no sink and applies nothing ([decision 32](../schemas/README.md#decisions) D5); what the plugin process itself does is outside the guard (see [§7](#7-accepted-risks)); read-only filesystem and egress proxy in M5 | `tests/test_dry_run.py` (`test_the_engine_calls_no_sink_during_a_dry_run`, `test_horae_diff_writes_nothing`); #49, #50 | D→I | implemented (core); accepted risk in D tier |

### B3: core ↔ LLM backend

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T10 | I | A secret or personal data inside source output is sent to a cloud LLM | De-identification of declared fields before any cloud backend (#34); refuse a request that contains the value of any resolved secret (`SecretResolver.resolved()`) | #34 | D | planned #34 (secret check: see [§10](#10-story-updates-requested)) |
| T11 | I, E | `claude-cli` exposes user configuration, tools or the OAuth token | Isolated empty config, env allowlist, built-in tools disabled, no user settings ([ADR 0005](adr/0005-claude-cli-backend-and-its-terms.md)); Horae never handles OAuth; re-verified per Claude Code release | #29 (fake binary) | D | planned #29 |
| T12 | D | Runaway spend (loop, injection-inflated input) | One LLM step, `on_unchanged_input: skip` ([decision 15](../schemas/README.md#decisions)); budgets per step and per day with a ledger (#33); input-hash cache, the $0 path (#32); provider spend limits (#63) | `tests/test_contracts.py` (budget fields required); #32, #33 | D | planned #32, #33, #63 |

### B4: phone ↔ `/act`

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T13 | S | A stolen or replayed `/act` token | HMAC-signed, single use, 24 h expiry, bound to one intent, no secrets or free-form URLs, rate limited (#38) | #38 | D | planned #38 |
| T14 | T | A link preview or prefetch consumes a single-use token; CSRF on `/act` | `GET /act/<token>` only shows a confirmation and changes nothing; `POST` applies | #38 | D | planned #38 (see [§10](#10-story-updates-requested)) |
| T15 | I | `/act` is reachable without SSO (PLAN §7); a misconfiguration exempts more than `/act/` | Only `/act/` bypasses SSO; `/runs` and `/healthz` behind SSO; no secrets in responses (#37) | #37; SSO-exemption test: #41 | D | planned #37, #41 |
| T16 | S | The HMAC key leaks or is never rotated | A dedicated key (a secret, not a shared one); rotation invalidates every outstanding token | #38 | D | planned #38 (see [§10](#10-story-updates-requested)) |

### B5: MCP clients ↔ Horae's MCP server

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T17 | S, E | An unauthenticated MCP client runs a pipeline or reads tasks | Token auth, hashed and revocable (#56); read-only tools (#54); `pipeline.run` only as a dry run plus an expiring approval (#55) | #54, #55, #56 | I | planned #54, #55, #56 |

### B6: marketplace and git → installed plugin

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T18 | T | A moved tag or rewritten branch changes installed code | `horae.lock` `commit` is a full 40- or 64-hex id, never a tag or short SHA ([decision 20](../schemas/README.md#decisions), `horae.contracts.lockfile`); checkout of the pinned SHA, refusal on tag/SHA mismatch ([ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md)) | `tests/test_contracts.py` (lockfile examples); #44, #48 | D | implemented (contract); planned #44, #48 |
| T19 | T, E | A compromised marketplace points a name at a malicious commit | Permission approval at install is the main defence ([ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md)); non-first-party refused before M5 (T04); signed tags are "later" (PLAN §4) | #46 | D | planned #46; accepted risk (no signed tags yet) |
| T20 | E | An upgrade silently widens permissions; a run uses permissions that were not approved | Permission diff and re-approval (#47); a run refuses plugins whose permissions differ from the lock (#46); run records carry `lock_digest` and replay refuses a changed lock ([decision 33](../schemas/README.md#decisions)) | `tests/test_runner.py` (`test_the_record_keeps_lock_config_digests_models_and_definition`), `tests/test_replay.py`; #46, #47 | D | implemented (digests); planned #46, #47 |
| T21 | T | A malicious URL or protocol in a clone (`ext::`, `file://`, option injection) | Lockfile `repo` is `https://` only ([decision 6](../schemas/README.md#decisions), [decision 20](../schemas/README.md#decisions)); `git clone -- <repo>` with `protocol.allow` restricted to `https` ([ADR 0004](adr/0004-git-marketplace-with-sha-pinning.md)) | `tests/test_contracts.py`; #44 | D | implemented (contract); planned #44 (see [§10](#10-story-updates-requested)) |
| T22 | T | Plugin dependencies are resolved unpinned at install time | Per-plugin venv (#45) built from the plugin's lock file with hashes | #45 | D | planned #45 (see [§10](#10-story-updates-requested)) |
| T23 | R | No trace of which plugin did what | Run records carry each plugin's version and commit, `lock_digest`, `intents[].proposed_by` and `applied_by` ([decision 21](../schemas/README.md#decisions), [decision 30](../schemas/README.md#decisions)); the journal records `applied_by` and `sink_identity` ([decision 34](../schemas/README.md#decisions)); `horae plugin audit` (#47) | `tests/test_engine.py` (`test_run_record_rejects_impossible_routes`), `tests/test_undo_hardening.py` (`test_the_journal_records_where_each_change_was_written`); #47 | D | implemented (records, journal); planned #47 |

### B7: Horae ↔ host

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T26 | E | The container runtime needs the Docker socket: a Horae compromise becomes host root | None beyond keeping the core small and reviewed; documented in the deployment docs | #49 (documentation) | I | **accepted risk** ([§7](#7-accepted-risks)) |
| T27 | I | A neighbour container on a shared Coolify network reaches Horae or its plugins | `internal: true` plugin network (M5, #49); `mcp-http` is `https` only ([decision 6](../schemas/README.md#decisions)); fallback bearer token and IP allow-list for cross-resource networking (#74) | #49; #74 | D/I | planned #49; risk #74 |
| T28 | I | Backups or long-lived run records leak LLM inputs, journal note bytes and vault data | State directory 0700, database and its `-wal`/`-shm` 0600 (`Store.open`, umask 0077); `Store.prune_runs` deletes old runs with their steps, journal and unreferenced blobs; `Store.cache_purge`; interim operator guidance in [operations/retention-and-backups.md](operations/retention-and-backups.md); a retention workflow to revisit in [design note 0014a](design/0014a-retention-workflow.md) | `tests/test_store.py` (`test_state_files_are_private`, `test_prune_runs_cascades_and_collects_orphan_blobs`) | D | implemented (file modes, pruning API); accepted risk until 0014a is decided |
| T29 | I | The vault CouchDB is exposed without SSO | LiveSync E2EE and path obfuscation on (PLAN §7) | #64, #65 (manual); risk #71 | D | planned #64, #65 |
| T32 | T | Two processes apply or undo at once and corrupt a note or the journal | Per-pipeline `flock` and the instance-wide apply lock (lock files 0600, directory 0700, `O_NOFOLLOW`) ([decision 31](../schemas/README.md#decisions), [decision 34](../schemas/README.md#decisions)); compare-and-swap writes | `tests/test_runner.py` (`test_lock_files_are_private_and_probing_creates_nothing`, `test_two_processes_one_pipeline_the_second_exits_with_a_lock_message`), `tests/test_undo_hardening.py` (`test_a_run_waits_for_the_apply_lock_then_fails`) | D | implemented |

### B8: Horae's own supply chain

| ID | STRIDE | Threat | Control | Test | Tier | Status |
|---|---|---|---|---|---|---|
| T24 | T | A compromised dependency, action or base image in Horae | `uv.lock` and Renovate; actions pinned to commit SHAs, checksum-verified gitleaks, CodeQL (`.github/workflows/ci.yml`, #11); SBOM (#40); **signed images from the first `edge` image** (maintainer decision, PR #84; was M8 in #58); PyPI trusted publishing (#58); org 2FA and push protection (#62) | CI itself (#11); #40, #58, #62 | — | implemented (#11); planned #40, #58, #62 |
| T25 | T | Insecure security code written by agents | Human-led stories and Graft sensitive paths (`graft.yaml`, [AGENTS.md](../AGENTS.md#review)), `docs/security.md` added to them (maintainer decision, PR #84); malicious-plugin suite (#52); risk #69 | `tests/test_graft_policy.py`; #52, #61 | — | implemented (policy); planned #52, #61 |

## 7. Accepted risks

Recorded as accepted by the maintainer (PR #84) or by an accepted decision. Each is revisited at the
milestone named.

1. **Docker socket = host root (T26), until further notice.** The M5 container runtime (#49) needs
   to start containers; with the Docker socket mounted, code execution in the Horae core is root on
   the host. A socket proxy or rootless Docker were considered and not required for now. Keep the
   core's attack surface small (B2 and B4 are the entry points) and keep Horae on a host that holds
   nothing more valuable than what Horae already reads.
2. **The dry-run write guard does not cover `ctypes`, C extensions or plugin subprocesses (T31).**
   `tests/test_dry_run.py` proves that the core writes nothing but its own store during a dry run:
   Python-level writes, metadata changes, sockets, name resolution, SQLite connections and process
   creation ([decision 32](../schemas/README.md#decisions) D5). Writes made below Python, and
   anything a plugin process does itself, are outside it. A dry run guarantees that no intent is
   applied, not that a plugin has no side effects; the M5 read-only filesystem and egress proxy
   narrow this.
3. **`/proc/<horae-pid>/environ` before M5.** A plugin subprocess running as Horae's uid can read
   Horae's environment. Mitigated by file-based secrets: a secret is read from
   `/run/secrets/<NAME>` unless an explicit `{env: VAR}` reference says otherwise
   ([decision 25](../schemas/README.md#decisions)), so a deployment that uses secret files keeps
   secret values out of the environment. The files themselves stay readable by the same uid until
   M5 mounts, which is why non-first-party plugins are refused before M5 (T04).
4. **First-party trust in the D tier (T04, T05).** Between M1 and M4 a plugin is a subprocess with
   the core's uid and network: the D tier constrains what it can make the core do, not what the
   process can do. Accepted for first-party plugins only (PLAN §3.3, ADR 0003); others are refused
   until M5.
5. **No signed marketplace tags (T19).** Approval of permissions at install and SHA pinning are the
   defence; signed tags are "later" (PLAN §4).
6. **Retention is manual (T28).** Until the workflow of [design note
   0014a](design/0014a-retention-workflow.md) is decided, pruning and backup encryption are operator
   tasks ([operations/retention-and-backups.md](operations/retention-and-backups.md)).

## 8. Out of scope (for now)

- **M6, existing MCP services** (`plugin-mcp-brief`, the finance and training coaches, joining
  their Coolify networks) and **M7, mail and school** (`plugin-imap`, `plugin-gmail`,
  `plugin-school-fr`, school credentials, Proton Mail Bridge). Their boundaries are added to this
  document when those milestones start (maintainer decision, PR #84).
- A malicious operator or root on the host, multi-tenancy and physical access (see §5).

## 9. Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting: the **Security** tab
of this repository, then **Report a vulnerability**. The policy (what to include, response time,
scope, supported versions) is the organisation's
[`SECURITY.md`](https://github.com/horae-dev/.github/blob/main/SECURITY.md), which GitHub applies to
every `horae-dev` repository.

**Keeping this document current:** a PR that adds or changes a control on a boundary updates its
row (control, test, status). A new boundary, plugin runtime or interface gets its rows before it is
merged. `docs/security.md` is a Graft sensitive path, so changes need the maintainer.

## 10. Story updates requested

The maintainer chose to extend existing stories rather than open new ones (PR #84). Acceptance
criteria to add, per issue:

- **#42 Plugin SDK (Python)**, T02:
  - [ ] Intent builders escape the Obsidian Tasks emoji signifiers (`📅`, `⏰`, `✅`, `🔁`, `➕`,
        `⏳`, `🛫`, priorities) and Markdown in task titles, and sinks writing Tasks syntax use
        them (tested with a title holding every signifier)
- **#46 Permission approval and lockfile**, T04:
  - [ ] While plugins run as subprocesses (before M5), installing a marketplace plugin whose tier is
        not `first-party` is refused (tested)
- **#48 Direct install from a git URL**, T04:
  - [ ] Before M5, a direct install (no marketplace tier) is refused (tested)
- **#34 De-identification transform**, T10:
  - [ ] A request to a cloud backend that contains the value of any resolved secret is refused
        before it is sent, and the run is marked `degraded` without logging the value (tested)
- **#38 Signed single-use action links (/act)**, T14, T16:
  - [ ] `GET /act/<token>` only shows a confirmation page and consumes nothing; `POST` applies
        (tested: a GET, as a link preview does, leaves the token usable)
  - [ ] The HMAC key is a dedicated secret; rotating it invalidates every outstanding token (tested)
- **#41 Coolify template and plain compose**, T15:
  - [ ] Only `/act/` is exempt from the SSO middleware; `/runs` and `/healthz` require SSO (tested
        against the rendered template)
- **#44 Install from git with pinned commit**, T21:
  - [ ] Clones run as `git clone -- <repo>` with `protocol.allow` limited to `https`; `ext::`,
        `file://` and option-like URLs are refused (tested)
- **#45 Per-plugin virtual environments**, T22:
  - [ ] A plugin's dependencies install only from its committed lock file with hashes
        (`uv sync --locked`); a plugin without one is refused (tested)
- **#49 Per-plugin container runtime**, T26:
  - [ ] The deployment docs state that mounting the Docker socket makes a Horae compromise
        equivalent to host root, an accepted risk (docs/security.md §7)
- **#29 claude-cli backend with isolation**, T11:
  - [ ] The isolation test is re-run against each new Claude Code release before the pinned
        version is bumped
- **#40 Container image and GHCR publishing**, T24:
  - [ ] `edge` and release images are signed with cosign and the signature is verified in CI
- **#53 Secret redaction in logs and run records**, T09: move from **M5 Hard isolation** to **M1
  Walking skeleton** (or M2 at the latest); its AC is unchanged.

Also suggested (from the table, not in the maintainer's answers):

- **#50 Egress allow-list proxy**, T07:
  - [ ] A declared host that resolves to a private, loopback or link-local address is blocked
        (tested)
