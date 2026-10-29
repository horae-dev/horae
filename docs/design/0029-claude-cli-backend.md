# Design note: `claude-cli` backend with isolation

- **Issue:** [#29](https://github.com/horae-dev/horae/issues/29) (M2, Human-led), epic
  [#3 Bounded LLM step](https://github.com/horae-dev/horae/issues/3); threat **T11** in
  [docs/security.md](../security.md#b3-core--llm-backend).
- **Status:** Draft, for maintainer review. No code until the questions below are answered.
- **Sources:** [ADR 0005](../adr/0005-claude-cli-backend-and-its-terms.md),
  [PLAN §3.4](../PLAN.md#34-llm-step-and-backends), [§10](../PLAN.md#10-risks-honest),
  schemas decisions 23–25, 28, 35; `src/horae/engine/ports.py`, `src/horae/plugins/host/`.
- **Not a legal assessment.** The terms position (personal headless use of `claude -p` on your
  own plan) is PLAN's **⚠️ unverified** reading, tracked in risk
  [#70](https://github.com/horae-dev/horae/issues/70). This note draws no legal conclusion.

## Goals and non-goals

Goals (issue AC plus the #14 addition):

1. Run the user's own `claude -p --output-format json` as an `LlmBackend` with **no tools, no
   MCP servers, no user/project/local settings, no hooks, no CLAUDE.md, no session persistence**.
2. Only allowlisted environment variables reach the process; no ambient credential other than the
   user's own Claude Code login or a `CLAUDE_CODE_OAUTH_TOKEN` made with `claude setup-token`.
3. Bounded: timeout, output size, budget; a misbehaving CLI degrades the step, never crashes the run.
4. Version pinned; the isolation test is re-run against each new Claude Code release before the
   pin is bumped (#14, T11).
5. Docs: "personal use under your plan's terms"; `anthropic` with an API key is the recommended
   backend for published setups.

Non-goals: Horae handling OAuth (ADR 0005 §3); shipping Claude Code in the Horae image; streaming
(`stream-json`); multi-turn sessions; any tool use.

## Current state (on `main`)

- `horae.engine.ports.LlmBackend` (`complete(LlmRequest) -> LlmResponse`, raises `LlmError`) and
  `LlmBackendName` already include `claude-cli`. The engine enforces one deadline for both attempts,
  the budget, the repair retry and records `llm[]` (`horae.engine.executor._llm_step`).
- `ModelAlias` (`src/horae/contracts/instance.py`): `claude-cli` needs neither `base_url` nor
  `secret` (decision 24); `secret` is optional and can name the setup-token secret.
- No `src/horae/llm/` package exists yet; `horae run` fails an `llm` step with `llm_unavailable`
  (decision 37). The backend protocol and Anthropic backend are **#28** (no PR open at the time of
  writing); this backend implements whatever #28 settles and is written after it.
- Reusable pieces from the plugin host (#24): exact child env and deny list
  (`horae.plugins.host.env`), process-group spawn and kill (`transport.py`), capped stderr
  capture with redaction (`capture.py`, `redact.py`).

## Proposed design

Module `src/horae/llm/backends/claude_cli.py` (a Graft sensitive path).

**Invocation.** `claude` is resolved once when the backend is built (absolute path, symlinks
resolved), then `claude --version` is checked against the pin. Each attempt spawns:

```
<abs claude> -p --output-format json --model <alias.model>
  --tools "" --strict-mcp-config --mcp-config <empty.json> --setting-sources ""
  --no-session-persistence --disable-slash-commands --permission-mode dontAsk
  --system-prompt <skill prompt file or text> [--max-budget-usd <remaining>]
```

Every flag above is listed by `claude --help` of Claude Code **2.1.284** (checked locally); their
**behaviour is still to verify** with the live isolation test, in particular: that `--tools ""`
leaves no built-in tool, that `--setting-sources ""` is accepted and excludes user settings and the
hooks they define, and whether `--system-prompt` replaces the default prompt entirely. `--bare`
would be the strongest mode, but its help text says OAuth and the keychain are never read under it,
so it seems to exclude subscription auth (**to verify**); `--restricted` and `--safe-mode` are
candidates to evaluate. The prompt (fenced input, #35) goes on **stdin**, never argv (visible in
`ps`, size limits).

**Environment.** Built like a plugin's (#24): exactly `PATH` (fixed), `HOME` and `TMPDIR` (empty
per-attempt directories, 0700), `LANG=C.UTF-8`, `TZ`, plus, in token mode, `CLAUDE_CODE_OAUTH_TOKEN`
from the alias's `secret` (resolved as a `Secret`, revealed only into the child mapping). Never
`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` or any `*_PROXY` (the deny list applies). Variables to
switch off auto-update and telemetry (`DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) are **to verify** for the pinned version. The cwd is an
empty per-attempt directory, so no `CLAUDE.md` or project settings are discovered.

**Auth modes** (question 1). *Token mode* (recommended default): `HOME` is empty, auth is the
setup-token only; works headless and in containers. *Login mode* (opt-in): the user's real login
is needed, which lives under the real `HOME` (or the macOS keychain). Pointing `CLAUDE_CONFIG_DIR`
at a dedicated, Horae-only config directory that the user logs into once is the candidate (**to
verify** that it relocates credentials and settings).

**Bounds.** One process group per attempt; `request.timeout_s` then `killpg` (TERM, 2 s, KILL);
stdout capped at 1 MiB (`output_too_large`), stderr at 64 KiB, redacted with every resolved secret
(the OAuth token included) before it reaches `LlmError` or the record.

**Output parsing.** Exactly one JSON object on stdout. Expected fields (**to verify** per pinned
version, pinned in a recorded fixture): `type == "result"`, `subtype`, `is_error`, `result` (text),
`total_cost_usd`, `usage` (`input_tokens`, `output_tokens`, cache fields), `num_turns`, `session_id`.
`is_error`, a non-zero exit, `num_turns != 1` or any sign of tool use is an `LlmError`; `result` must
parse as the skill's JSON (no fence stripping); schema validation and repair stay #31's. Unknown
extra fields are ignored; missing required ones are `output_invalid`.

**Usage.** `tokens_out` from `usage.output_tokens`; `usd` from `total_cost_usd` (notional under a
subscription, counted anyway; question 4). `--max-budget-usd` gets the remaining step budget as a
second fence.

**Version pin.** `SUPPORTED_CLAUDE_CODE = "2.1.x"` (exact range per question 2) in the module; a
version outside it is refused at build time (`backend_unsupported`, `horae doctor` reports it). A
`live` pytest marker (`HORAE_LIVE_CLAUDE=1`, never in CI) runs the isolation test against the real
binary; bumping the pin requires its output in the PR.

## Alternatives

- **Claude Agent SDK instead of the CLI.** More control, but an extra Node/Python dependency
  and the same terms question; the CLI is what PLAN and ADR 0005 name.
- **Inherit the user's environment and rely on flags only.** Simpler login, but any `ANTHROPIC_*`,
  proxy or Node variable would reach the process: rejected.
- **No version pin, best effort.** Isolation flags can change silently between releases (ADR 0005,
  "Negative"); rejected.

## Test plan (fake binary, CI)

A Python fake `claude` on a temporary `PATH` records argv, env, cwd, stdin and prints canned output.

- argv has every isolation flag; the prompt is on stdin, not argv; the model is the alias's.
- env keys are exactly the allowlist (token present only in token mode); a hostile parent env
  (`ANTHROPIC_BASE_URL`, `HTTPS_PROXY`, `NODE_OPTIONS`) does not leak.
- cwd and `HOME` are empty, private and removed afterwards; no `--mcp-config` but the empty one.
- timeout kills the group (fake sleeps, spawns a child); oversized stdout; malformed JSON;
  `is_error`; non-zero exit; `num_turns: 3`: each is a typed `LlmError`, the run is `degraded`.
- the token echoed on stderr is redacted in the record; version outside the pin is refused.
- recorded real-output fixture parses into `LlmResponse` with usage.
- live (manual): with real `claude`, a prompt asking to read a file or run a command gets no tool.

## Risks

- Flags or JSON shape change between releases (mitigated by the pin and the live re-check).
- Login mode may need the real `HOME`, weakening isolation; hence token mode as the default.
- Terms change (#70): the backend is one adapter and can be dropped (PLAN §10).

## Questions for the maintainer

1. **Auth modes.** Token mode (`CLAUDE_CODE_OAUTH_TOKEN` via `ModelAlias.secret`, empty `HOME`) as
   the only v1 mode, login mode later? *Recommendation: yes, token mode only in v1.*
2. **Pin granularity.** Exact version, or a minor range (`>=2.1.284,<2.2`)? Refuse or warn outside
   it? *Recommendation: minor range, refuse (override flag later if needed).*
3. **Flag set.** Accept the flag list above (each verified by the live test), and evaluate
   `--restricted` / `--bare` only if they keep subscription auth? *Recommendation: yes.*
4. **Budget under a subscription.** Count `total_cost_usd` against the step and daily budget even
   though it is notional? *Recommendation: yes, it is also the runaway guard (T12).*
5. **Structured output.** Use `--json-schema` with the skill's schema, or parse `result` and let
   #31 validate? *Recommendation: parse `result`; add `--json-schema` later if it proves reliable.*
6. **Binary location.** `PATH` lookup (fixed `PATH` plus `~/.local/bin`?) or a new optional
   `horae.yaml` key (`models.<alias>.command`, a `schemas/` change)? *Recommendation: the optional
   key, absolute path only, falling back to a `PATH` lookup.*
7. **Live re-check record.** Where is the per-release isolation result kept: the bump PR body, or
   a table in `docs/security.md` (T11)? *Recommendation: the PR body plus the T11 row's version.*
