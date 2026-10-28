# Design note: secret redaction in logs and run records

- **Issue:** [#53](https://github.com/horae-dev/horae/issues/53) (Human-led). Filed under epic
  [#6 Hard isolation](https://github.com/horae-dev/horae/issues/6) (M5); the threat model moved it
  to **M2** (docs/security.md §10, maintainer decision on PR #84) and the issue now carries the M2
  milestone. Threat **T09** in [docs/security.md](../security.md#b2-plugin-process--core).
- **Status:** Draft, for maintainer review. No code until the questions below are answered.
- **Sources:** [PLAN §2.1](../PLAN.md#21-design-tenets) tenet 6,
  [§9](../PLAN.md#9-quality-and-security-practice) ("Logs redact declared secrets"),
  [ADR 0006](../adr/0006-sqlite-as-the-store.md); schemas decisions 25, 27–29, 31, 35–37;
  `src/horae/secrets/`, `src/horae/plugins/host/`, `src/horae/store/`, `src/horae/runner/`,
  `src/horae/cli*.py`.

## Goal and non-goals

AC: **seeded secrets never appear in logs or the store (tested).** "Seeded" = every secret the
instance resolves, in the forms the redactor knows (raw, base64 incl. misaligned, hex, percent,
JSON escaping), long and short.

Non-goals: encryption at rest (T28, note 0014a); redacting personal data (#34); hostile plugins
that encode a secret in a way the redactor does not know (M5, #50–#52); bytes of the user's own
vault notes in the journal (question 2).

## Current state (on `main`): what is already redacted

| Channel | Control | Where |
|---|---|---|
| `Secret` values in `repr`/`str`/`format`/serialization, pickling refused | `horae.secrets.Secret` | `tests/test_secrets.py` |
| resolution and its errors (name only, no value, no chained context) | `SecretResolver` | `tests/test_secrets.py` |
| plugin stderr (per call, leftover), MCP log notifications | `StderrCapture` + `Redactor` over `SecretResolver.resolved()`, all secrets resolved at host open (decision 35 B) | `tests/test_plugin_host.py` |
| plugin error messages (whole, then body) | `PluginHost._redact_message` | `test_an_error_message_is_redacted_whole_before_its_body` |
| plugin outputs and intents (string and number leaves; key → `output_invalid`) | `Redactor.redact_data`, `secret_in_output` violation | `test_a_secret_in_the_output_or_intents_is_redacted_and_flagged` |
| sink operation results and errors (#25) | same host path, `McpSinks` violations | `tests/test_mcp_sink.py` |
| MCP SDK log records (message redacted, args/traceback dropped) | `horae.plugins.host.logs` record factory, while a host is open | `test_the_sdk_log_records_are_redacted` |
| `horae doctor` internal errors, crashed runs, CLI internal errors | exception **type** only (decisions 27, 31, 37) | `tests/test_doctor.py`, `tests/test_runner.py` |
| runner logs | run id and exception type only | `horae.runner._runner` |

So redaction is **at the producers**: the plugin host scrubs what plugins hand over, and core code
avoids printing values.

## What remains

1. **No last line of defence at the store.** `Store.save_run`, `record_step`, `journal_append`,
   the `llm_cache`, `tasks` and `notifications` tables write whatever they are given. A producer
   that forgets to redact (a future backend) writes the value to disk.
2. **The LLM layer** (M2, #28–#30): `LlmError.message` / `stderr` must be redacted by the backend
   (ports docstring), `llm[].request` / `response` are recorded verbatim, and the `claude-cli`
   stderr can echo the OAuth token or an API key (#29). Nothing implements this yet.
3. **Core logs.** Only the SDK's loggers are scrubbed, and only while a `PluginHost` is open.
   `horae.*` loggers, and anything logged by an LLM HTTP client (`httpx` debug logs carry headers),
   are not.
4. **Trusted inputs that a user may misuse.** A `--var` value, a pipeline parameter or a skill
   prompt holding a secret value lands in `context.vars`, `steps[].params`, `llm[].request`.
5. **CLI output** prints from the store and from errors; it is safe only if 1–4 are.
6. **The redactor lives in the plugin host** (`horae.plugins.host.redact`), so core modules would
   have to import the host to use it.

## Proposed design

**A. One shared redactor.** Move `Redactor`, `token` and `SecretInKey` to `horae.secrets.redact`
(re-exported from the old path). A process-wide `horae.secrets.registry` holds the redactor of
every secret resolved by any `SecretResolver` (host, LLM backends, doctor), rebuilt when the set
grows; resolvers register on resolve.

**B. Logging.** Generalise `horae.plugins.host.logs`: the record factory scrubs records of the
`horae`, `mcp`, `client`, `server`, `httpx`, `httpcore` and `anthropic` loggers with the registry's
redactor (message formatted and redacted, args dropped, exc_info reduced to its type), installed
once by the CLI entry point and by `Runner`, independent of any open host. `httpx`/`httpcore` are
also pinned to `WARNING` so headers are never formatted.

**C. Store gate (fail safe, not fail closed).** Every text or JSON value written by `Store`
(record JSON, step rows, cache entries, task titles, notification keys, journal `after` bytes
built by the core) passes `redact_data` / `redact_bytes` with the registry's redactor just before
the `INSERT`. A hit is replaced by `[REDACTED:<NAME>]`, recorded as a `secret_in_output` violation
on the run when there is one, and logged by name. Producers keep redacting; the gate makes a missed
spot a violation instead of a leak. Journal `before` bytes (the note as it was in the vault) are
**not** rewritten, or undo would restore different bytes (question 2).

**D. LLM backends.** A shared helper in `horae.llm` redacts `LlmError.message`, captured stderr,
`LlmResponse.request` and `output` with the registry's redactor before they leave the backend;
#34's request check refuses a secret *before* sending, this one cleans what comes back.

**E. Replays and digests.** Redaction happens before the record is stored, so a replay compares
redacted against redacted. `pipeline_digest` and the cache key stay computed on the real values
(they are hashes, not values).

## Alternatives

- **Fail closed at the store** (refuse to save a record with a secret). Loses the run record,
  the audit trail of exactly the incident; rejected for runs, kept as an option (question 3).
- **Producer-only redaction** (status quo). Every new channel must remember; the AC asks for a
  guarantee on the store as a whole.
- **Encrypt the store.** Different threat (T28); does not stop CLI or log output.

## Test plan

One end-to-end test, `tests/test_secret_redaction.py`:

- an instance with a long (≥ 8 bytes) and a short secret, and a malicious fixture plugin that
  echoes both, in every encoding, to stderr, a log notification, an error message, its output,
  its intents and a sink result; a fake LLM backend that echoes them in its error, stderr,
  request and response; a `--var` holding one;
- run, dry run, `horae runs --json`, `horae diff`, `horae undo`, `horae replay`;
- then read **the database file and its `-wal`/`-shm`** as bytes, every captured log record
  (`caplog`, all loggers) and every CLI stdout/stderr, and assert no encoding of either secret
  appears; each hit in the store is a `secret_in_output` violation naming the secret.
- unit tests: the registry grows across resolvers; the log factory scrubs `horae.*` and `httpx`
  records with no host open; the store gate redacts each table; journal `before` bytes are kept.

## Risks

- Over-redaction of short secrets in legitimate data (bounded raw matching, decision 35 D).
- Performance: redacting every write; records are bounded (`MAX_*`), the regex is built once.
- A secret not resolved in this process (another plugin's, never started) is not known to the
  registry; decision 35 B resolves all declared secrets at host open, the same is done at runner
  start.

## Questions for the maintainer

1. **Scope of "the store".** Every table (runs, steps, journal `after`, cache, tasks,
   notifications), not only run records? *Recommendation: every table.*
2. **Journal `before` bytes.** Exempt from the gate (the user's own note, needed byte-exact for
   undo), with a documented exception in docs/security.md? *Recommendation: exempt.*
3. **Fail mode at the store.** Redact and flag (`secret_in_output`), or refuse the write?
   *Recommendation: redact and flag; refuse only cache entries.*
4. **Trusted inputs.** Redact `--var`, parameters and skill prompts too, or refuse a run whose
   trusted inputs contain a resolved secret? *Recommendation: refuse the run at plan time
   (`params_invalid`, name only): a secret in a trusted parameter is a configuration error.*
5. **Logger list.** Scrub `horae`, the MCP SDK, `httpx`, `httpcore` and `anthropic`, plus pin the
   HTTP loggers at `WARNING`? *Recommendation: yes.*
6. **Epic.** Re-parent #53 from #6 (M5) to #3 (M2) to match its milestone? *Recommendation: yes.*
