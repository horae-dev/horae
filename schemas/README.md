# Horae JSON Schemas

Machine-readable contracts for the documents Horae reads and writes ([PLAN.md](../docs/PLAN.md) §3).
All schemas use JSON Schema **draft 2020-12**. `$id` values below are relative to
`https://raw.githubusercontent.com/horae-dev/horae/main/schemas/`.

| Schema | File | `$id` (relative) | Validates |
|---|---|---|---|
| Pipeline | [`pipeline/v1.json`](pipeline/v1.json) | `pipeline/v1.json` | `pipelines/*.yaml` (§3.2) |
| Plugin manifest | [`plugin-manifest/v1.json`](plugin-manifest/v1.json) | `plugin-manifest/v1.json` | `plugin.yaml` (§3.3) |
| Intent (any) | [`intent/v1.json`](intent/v1.json) | `intent/v1.json` | one intent, `oneOf` tagged by a `type` const |
| One intent | `intents/<type>/v1.json` | `intents/<type>/v1.json` | `note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`, `notify.push`, `http.post` |
| Lockfile | [`lockfile/v1.json`](lockfile/v1.json) | `lockfile/v1.json` | `horae.lock` (§4) |
| Run record | [`run-record/v1.json`](run-record/v1.json) | `run-record/v1.json` | one run (§3.1, §2.1.6) |
| Instance configuration | [`instance/v1.json`](instance/v1.json) | `instance/v1.json` | `horae.yaml` (§3.1, §3.4, §7) |

## Source of truth

The pydantic models in [`src/horae/contracts/`](../src/horae/contracts/) are the source of truth
(public types such as `Hostname`, `NotePath` or `SecretName` live in `horae.contracts.types` and
are re-exported from `horae.contracts`). The files here are **generated**; do not edit them by hand:

```
uv run python -m horae.contracts.export          # regenerate schemas/ (from any directory)
uv run python -m horae.contracts.export --check  # exit 1 if schemas/ is stale
```

`tests/test_contracts.py` fails if the committed files differ from what the models generate
(byte for byte, LF line endings: see `.gitattributes`), if a `*.json` file outside `examples/` is
not a generated schema, and checks that the schemas and the models agree on every example.

The output bytes depend on the pydantic version locked in `uv.lock`. A Renovate bump of
pydantic may change them; the drift test then fails in CI and the fix is to regenerate.

## Versioning

- Each schema has a versioned `$id`,
  `https://raw.githubusercontent.com/horae-dev/horae/main/schemas/<name>/v<N>.json`, and lives at
  `<name>/v<N>.json`, so the `$id` resolves to the file once it is on `main`.
- A breaking change adds `v<N+1>.json` next to the old file; compatible additions stay in `v<N>`.
  Most rules are deliberately tight: loosening one later is a compatible change.
- Files that Horae writes itself carry `schema_version` (`horae.lock`, run records). Files that
  people write (pipelines, `plugin.yaml`) do not, so the PLAN.md examples validate unchanged.
  `horae.yaml` accepts an **optional** `schema_version: 1` (the default), so that a future v2
  file can be told apart (decision 26).

## Rules worth knowing

- Unknown keys are rejected everywhere (`additionalProperties: false`), except the free-form
  parameters of a plugin call and recorded data (source outputs, LLM requests and responses).
- **No type coercion.** A string is never a number, a number never a string, and `true` is never
  an integer (not even `schema_version: 1`). Dates are ISO 8601 strings: `YYYY-MM-DD`, or RFC 3339
  date-times with seconds and an explicit offset (`2026-10-27T05:30:00Z`, upper-case `T`/`Z`).
  Naive date-times are rejected. The models accept the same strings from JSON
  (`validate_json`) and from already-parsed data (`validate_python`, e.g. `json.loads`, MCP or
  SQLite rows), and also `date` / `datetime` objects from Python code. In YAML, quote dates:
  an unquoted `2026-10-27` is a YAML timestamp, not a string, and the JSON Schema rejects it.
- A pipeline step is an object with exactly one key: `collect`, `transform`, `llm`, `act` or
  `notify`. Each lists plugin calls, either bare (`tasks.normalize`) or with parameters
  (`calendar.events: { days: 2 }`). A pipeline has **at most one** `llm` step
  (`contains` + `maxContains: 1`).
- The `llm` step has no `tools` key, and requires a budget with both `usd` and `tokens_out`.
- Plugin permissions (`network`, `secrets`, `paths`, `intents`) default to empty: no access.
- Sinks apply intents (PLAN.md §3.1, §3.3); `permissions.intents` lists the intents a plugin may
  propose or, for a sink, apply.
- Sizes are bounded: lists and maps at most 256 entries; note content at most 1 MiB (1 048 576
  characters, i.e. code points); titles 500, headings 200, notification bodies and messages 4096
  characters; captured `stderr` 64 KiB; URLs 2048; paths 1024 (255 per segment).

### Strings that reach the outside world

| Type | Rule |
|---|---|
| Host (`permissions.network`) | lowercase DNS name; the last label starts with a letter, so **no IP literal** (dotted, decimal, hex or IPv6); `localhost` and `*.localhost` rejected; single-label names (`coach`, a Docker/Coolify service) allowed; no port, scheme or wildcard |
| `http.post.url`, `runtime.url`, `homepage` | `https://` only; host as above; optional port 1–65535; no user info, fragment, backslash, whitespace or control characters |
| `notify.push.link` | `https://` or `obsidian://`, then `[^\x00-\x20\x7f]+` |
| Lockfile `repo` | `https://<host>/<segments>` with segments of `A-Z a-z 0-9 . _ -` (`.git` optional), no `.`/`..` segment, no port, user info, query or fragment, at most 512 characters |
| Note path (intents) | relative POSIX path: no leading `/`, no `\`, no control characters, no empty segment, **no segment starting with `.`** (so no `.`, `..`, `.obsidian/` or hidden files) |
| `permissions.paths` | normalised absolute POSIX path: no `.`/`..`/empty segment, no trailing `/`, not `/`, no `\` or control characters |
| Secret name | `^[A-Z][A-Z0-9_]*$` (not starting with `_`); `HORAE_*` reserved; `LD_*`, `DYLD_*`, `PYTHON*`, `PATH`, `HOME` rejected |
| `models.<alias>.base_url` (`horae.yaml`) | `https://` to a host name (`localhost` included), an IPv4 literal or a bracketed IPv6 literal; `http://` only to `localhost`, `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `[::1]`, `[fc00::/7]` or a single-label name (`ollama`); optional port; no user info, fragment, backslash, whitespace or control characters (decision 24) |
| Task title, headings, descriptions | one line: `^[^\x00-\x1f\x7f]+$` |
| Block id | `^[a-z0-9][a-z0-9-]{0,63}$` |
| Fingerprint (`task.create.fingerprint`, `task.update_status.task`) | `^[A-Za-z0-9._:-]{1,128}$` |
| Run id | `^[A-Za-z0-9._-]{1,128}$` (safe as a file name) |

Contracts constrain shape, not meaning. The runtime still has to:

- check `http.post` and `mcp-http` hosts against `permissions.network` using **the same parsed
  host** (`horae.contracts.url_host`), never a second URL parser (`horae.intents` does this for
  `http.post`, #20), and resolve names through the egress proxy so that a declared name cannot
  reach private or link-local addresses;
- **never follow redirects** in a sink that makes a request (`http.post`, `mcp-http`), or re-check
  the host of every hop with `url_host` against the same `permissions.network`: a declared host
  must not be able to bounce a request to an undeclared one;
- keep the run-wide number of proposed intents within `RunRecord.intents`'s bound,
  `horae.contracts.MAX_RUN_INTENTS` (256): `horae.intents.MAX_INTENTS` bounds one step only;
- escape text before it reaches a format that interprets it: sinks writing Obsidian Tasks must
  escape the emoji signifiers (`📅`, `⏰`, `✅`, `🔁`…) in titles, not only Markdown;
- install plugins with `git clone -- <repo>` and `protocol.allow` restricted to `https`, even
  though the lockfile already only accepts `https://` URLs.

### Rules only the pydantic models check

JSON Schema cannot express these; the models enforce them and the tests pin the difference:

- `run-record`: `changes[].intent` is an index into `intents`; `finished_at` is not before
  `started_at` (run and step records); `intents[].applied_by`, `applied_by_step` and
  `applied_by_call` are all set or all absent (the JSON Schema's `dependentRequired` checks
  presence, the model also rejects an explicit `null` next to a value); `pipeline_definition`
  is a valid pipeline named `pipeline`, at most 64 KiB as compact JSON (decision 33).
- `plugin-manifest`: the host of an `mcp-http` `runtime.url` is listed in `permissions.network`.
- Hosts (`permissions.network`, every `https` URL): a label starting with `xn--` must be a valid
  IDNA A-label. It must round-trip through Python's `idna` codec (IDNA2003, so no runtime
  dependency) and decode to letters, marks, digits and `-` only. The second rule rejects what
  IDNA2003 allows but IDNA2008 does not, such as emoji (`xn--ls8h`) and symbols (`xn--g6h`); it
  approximates IDNA2008 rather than implementing RFC 5892 exactly.
- `instance` (`horae.yaml`): a bracketed IPv6 literal in `base_url` must parse as an IPv6
  address (the schema only checks its characters and, for `http`, the `fc`/`fd` prefix).

### Known validator differences

- **Integral floats.** JSON Schema `integer` (and `const: 1`) accepts `2000.0`; the models reject it.
- **Trailing newline with Python's `jsonschema`.** It evaluates `pattern` with `re.search`, where
  `$` also matches before a final `\n`, so `"Buy milk\n"` passes `^[^\x00-\x1f\x7f]+$`. ECMA-262
  validators and the models reject it. The patterns use no look-around and no `\s`/`\d`/`\w`
  shorthands, so they mean the same in ECMA-262 and Rust `regex` (pydantic); the test suite
  validates with ECMA `$` semantics.
- **Formats.** `format` (`date`, `date-time`) is an annotation unless the validator asserts formats
  (`FormatChecker` in Python's `jsonschema`, with `jsonschema[format-nongpl]`). Every date and
  date-time also has a `pattern`, so a naive date-time fails either way; an impossible date such as
  `2026-02-30` fails only with format assertion (and always in the models).
- **Explicit `null`.** Optional fields accept `null` as "absent", in both.
- **Non-finite numbers.** The models reject `NaN` and `Infinity`; standard JSON cannot contain them.

## Lockfile canonical form

Serializing a `Lockfile` sorts plugins by name and every permission list, so a diff of two lockfiles
shows only real changes. Reading accepts any order.

## Examples

[`examples/<name>/valid/`](examples/) and [`examples/<name>/invalid/`](examples/) hold passing and
failing examples for every schema. Each valid example passes the JSON Schema, `validate_python` and
`validate_json`; each invalid one fails all three. `examples/pipeline/valid/daily-brief.yaml` and
`examples/plugin-manifest/valid/github.yaml` are the PLAN.md §3.2 and §3.3 examples, verbatim (a
test checks this). All other examples are synthetic.

## Decisions

Confirmed by the maintainer on 2026-10-28. PLAN.md left these open; each takes the most
conservative option, so loosening one later is a compatible change.

1. **Intent names.** §2.1 says `create_task`, `append_note`, `notify`; §3.3 lists the closed set
   `note.create`, `note.append_section`, `note.replace_owned_block`, `task.create`,
   `task.update_status`, `notify.push`, `http.post`. v1 uses the §3.3 names.
2. **Intent fields** are the minimum each sink needs: `task.create` has `title`, a **required**
   `fingerprint`, optional `due` and `time` (`HH:MM`, the ⏰ reminder; `time` requires `due`);
   `task.update_status` uses the fingerprint as task id and the statuses `todo`, `done`,
   `cancelled`, `snoozed` (`until`, a date or date-time, is required for `snoozed` and only
   allowed there); `notify.push` has `body`, optional `title` and `link`; `http.post` has `url` and
   a JSON `body` (no headers). The core (`horae.intents`) accepts only plain JSON data in
   `body` (string keys, finite numbers, nesting at most 64 deep) and caps its compact
   serialization (`json.dumps(body, allow_nan=False, separators=(",", ":"))`) at 1 MiB of UTF-8
   (`horae.contracts.types.MAX_BODY`).
3. **Fingerprint alphabet** `A-Z a-z 0-9 . _ : -`, 1–128 characters (fits `source:id` keys).
4. **Note paths** reject every segment starting with `.`, not only `.obsidian/`: hidden files and
   folders are never written by intents.
5. **Text limits**: title 500, heading 200, notification body 4096, note content 1 MiB.
6. **URLs are `https` only** for `http.post`, `mcp-http` runtimes (even to internal Docker
   service names), `homepage` and lockfile `repo`. Host names must be lowercase and cannot be IP
   literals or `localhost`.
7. **Notification links** are `https://` or `obsidian://` only.
8. **Secret names** reject the listed loader variables and reserve `HORAE_`. Other variables that
   change process behaviour (`NODE_OPTIONS`, `BASH_ENV`, `*_PROXY`, `SSL_CERT_FILE`…) are left to
   the runtime's environment builder, which must own the full deny list.
9. **`permissions.paths`** is `{ read: [...], write: [...] }` of absolute paths. What they are
   relative to (the container's filesystem, the instance root, a vault mount) is open for #20/#51.
10. **`permissions.network`** takes bare host names (single-label names allowed): no port, scheme
    or wildcard. **Any port on a declared host is allowed**: `http.post` to
    `https://hooks.example.org:8443/` passes when `hooks.example.org` is declared. Ports are not
    part of the permission; restricting them would need a new, explicit `host:port` form.
11. **Namespace prefix.** Tools are `<namespace>.<tool>` and skills `<namespace>/<skill>`. The
    namespace is **not** required to equal the plugin name, because PLAN.md uses prefixes that are
    not plugin names (`coach.brief` from `plugin-mcp-brief`). How a namespace resolves to a plugin
    is for the engine to define.
12. **"Implements" vs "proposes".** `permissions.intents` covers both the intents a plugin may
    propose and those a sink may apply. Whether these should be two lists is open.
13. **Trigger.** Optional: a pipeline without one runs only from an external scheduler
    (`horae run`). `cron` is five fields separated by single spaces, of `0-9 A-Z a-z * / , -`
    (no `@daily` macros), with an optional IANA `timezone` (instance default otherwise).
14. **One LLM step per pipeline** is enforced (PLAN.md says "by default"); a later opt-out would
    be an explicit key.
15. **`on_unchanged_input`** accepts only `skip`: §2.2 forbids an LLM call on unchanged input.
16. **LLM timeout.** `timeout_s` is optional; the core supplies a default.
17. **Transforms in `provides`.** §3.1 lists sources, sinks, notifiers, skills and pipeline packs,
    but §3.2 uses plugin transforms (`tasks.normalize`), so `provides.transforms` was added.
18. **Remote runtime.** `protocol: mcp-http` with an `https` `url` whose host is in
    `permissions.network`. `runtime` is optional so pipeline packs and skill-only plugins need none.
19. **Manifest metadata.** Optional `description` (one line), `license` (SPDX expression, syntax
    only) and `homepage` (https). No author or keywords.
20. **`horae.lock`.** §4 lists its content (name, version, SHA, approved permissions) but not its
    syntax; the schema validates the parsed document (the example is YAML) and adds the git
    `repo` (https only). Permission lists are written sorted.
21. **Run record.** v1 requires `pipeline_digest` (SHA-256, hex, of the canonical pipeline
    definition; the canonical form is fixed by the engine, see `horae.engine.canonical`) and `context`
    (`now`, IANA `timezone`, rendered template `vars`). Optional: `lock_digest`, `config_digest`,
    `input_hash`, per-step `error` / timings / capped `stderr`, LLM `attempt` and `schema_valid`,
    and `violations` (`egress_blocked`, `guard`, `undeclared_secret`, `undeclared_path`). Statuses:
    run `ok`, `noop`, `degraded`, `failed`, `undone`; step `ok`, `skipped`, `degraded`, `failed`.
    A dry run has no `changes`. Untrusted-content tagging is not part of v1.
    `intents[].proposed_by` is the proposing tool (`<namespace>.<tool>`) or, for intents of the
    `llm` step, the skill (`<namespace>/<skill>`) (added with #17, a compatible loosening).
22. **`$id` host.** `raw.githubusercontent.com/horae-dev/horae/main/schemas/…`, which this project
    controls and which serves the files once merged. A custom domain can come later as `v<N+1>`.

### Provisional decisions (#16, pending maintainer review)

Taken in #16 so that the loader could be built; each is the conservative option and can be
loosened later without breaking a valid file.

23. **`horae.yaml` (instance v1)** is kept minimal; every key is optional. `schema_version`
    (`1`, decision 26), `timezone` (IANA, default `UTC`), `state_dir` (default `state`, relative
    to the instance directory), `models` (alias → `backend` from the run-record list, `model`,
    `base_url` (decision 24) and `secret`, the name of the secret holding the API key or token),
    `plugin_defaults` and `plugins` (decision 26), `secrets` (`dir`, default `/run/secrets`,
    and `refs`: secret name → `{env: VAR}` or `{file: name}`, a single file name inside `dir`;
    decision 25) and `scheduler.mode` (`builtin`, the default, or `external`). It never holds a
    secret value.
    Rules the loader (not the schema) checks: time zones exist (`zoneinfo.available_timezones()`,
    with the `tzdata` package as a runtime dependency so that the answer does not depend on the
    host), `llm.model` aliases are defined, configured plugins are in `horae.lock` (skipped when
    `horae.lock` itself is invalid), a pipeline's `name` equals its file name.
24. **Backends and endpoints (A).** `ollama` and `openai-compatible` **require** `base_url`;
    `anthropic` **requires** `secret`; `claude-cli` needs neither. `base_url` is a `BackendUrl`:
    `https` is always allowed (any host name, `localhost` included, or an IP literal); plain
    `http` only to loopback (`localhost`, `127.0.0.0/8`, `[::1]`), private addresses (RFC 1918
    `10/8`, `172.16/12`, `192.168/16`; IPv6 ULA `fc00::/7`) and single-label names (a Compose or
    Coolify service such as `http://ollama:11434`), where TLS is usually not available and the
    traffic does not leave the host or the private network. No user info (credentials belong in
    a secret), no fragment. The JSON Schema expresses the same rule (one pattern and
    `if`/`then` for the required keys).
25. **Secrets come from files unless a reference says otherwise (B).** A secret with no entry
    in `secrets.refs` is read from `<secrets.dir>/<NAME>` **only**. Environment variables are
    read only through an explicit `{env: VAR}` reference. This keeps a plugin that declares a
    secret name from pulling an arbitrary variable of the host environment, and steers
    deployments towards secret files, which a plugin cannot read through
    `/proc/<horae-pid>/environ` (see the #24 design note, "Not enforced until M5"). An explicit
    env reference to a variable that is unset or empty is an error. One trailing newline (`\n`
    or `\r\n`) is stripped from every value; a value that still contains `\r`, `\n` or NUL, or a
    file that is not a regular UTF-8 file of at most 64 KiB, is an error. Files are opened once,
    non-blocking (a FIFO cannot hang `horae doctor`), and checked with `fstat` on that
    descriptor.
26. **`schema_version` and plugin timeouts (C).** `horae.yaml` takes an optional
    `schema_version: 1` (`true` and `1.0` are rejected, as everywhere). `plugin_defaults`
    (`startup_timeout_s`, default 10, and `call_timeout_s`, default 30) applies to every plugin;
    `plugins.<name>` may override either. Every timeout is 1 to 600 seconds.
    `Instance.plugin_timeouts(name)` returns the effective pair for the #24 plugin host.
27. **`horae doctor` (D).** Exit codes: `0` no problem, `1` problems found, `2` usage error,
    `70` internal error (`EX_SOFTWARE`; the doctor then prints only the exception type, never
    its message, traceback or locals, which may hold a resolved secret). `--json` prints
    `{"schema_version": 1, "ok", "instance", "problems": [{file, line, column, severity,
    message}]}`; `severity` is always `error` in v1 and is kept so warnings can be added.
28. **The environment deny list is the #24 host's (E).** `SecretName` rejects only the loader
    variables of decision 8. The full deny list of the #24 design note (`NODE_OPTIONS`,
    `BASH_ENV`, `*_PROXY`, `SSL_CERT_*`, `GIT_*`, …) is enforced by the plugin host when it
    builds a child environment, not by the schema: it is about what a variable does in a child
    process, not about whether a name is a valid secret name. The #24 `SecretResolver` protocol
    should return a `Secret` (not `str`), so values stay redacted until the host puts them in
    the child environment, and the host can redact every value from
    `SecretResolver.resolved()`.
29. **Problem positions (F).** A problem points at the **value** when the value is wrong
    (`scheduler.mode: cron` points at `cron`), and at the **key** when a key is unknown, invalid
    or missing (a missing key points at its parent's key). A mapping key that is not a valid
    name is shown as `<key at line N>`, never echoed, since it may be a secret pasted in the
    wrong place. Messages never show a regular expression, and a union (a pipeline step, a
    secret reference) gives a single problem, e.g. `unknown step kind 'colect' (expected one
    of: collect, transform, llm, act, notify)`.
30. **Intent routing in the run record (#17, maintainer decision D1).** Following ADR 0002,
    sources, transforms and the LLM step propose intents; `act` / `notify` entries are never
    called during a run, they name the sink or notifier that applies the intents later
    (#21/#22). Each recorded intent says which entry: `intents[].applied_by` (the entry's
    `<namespace>.<tool>`), `applied_by_step` (its step) and `applied_by_call` (its index in the
    step). The three are optional in v1 (a compatible addition) but go together, and the
    engine sets them on every intent it records; an applier must refuse an intent without
    them. `llm[].intents` records the raw intents returned with the response, before the core
    fills a target from the entry's parameters, for replays and for reuse on unchanged input
    (D2). `input_hash` is the LLM step's cache key (`horae.engine`), set only when the LLM output
    was accepted or reused. The routing and binding rules are the engine's, documented in
    `horae.engine.executor`; in short:
    - every `act` entry has a **required** `mode` (`create`, `append-section`,
      `replace-owned`, `tasks`, `post`); the note modes bind `path` and `post` binds `url`,
      compared as exact strings (no case, Unicode or URL normalisation);
    - a `notify.push` `link` is routed only to a `notify` entry whose trusted `link_prefix` it
      falls under; without `link_prefix` a proposed link is never routed. A prefix is an
      `https://` or `obsidian://` link with a host, no user info, query or fragment, and a
      clean path that ends with `/` (e.g. `obsidian://vault/Main/`), else `params_invalid`.
      Both are parsed (`urlsplit`), not compared as strings: same scheme, exactly the same
      netloc, and a link path under the prefix path whose rest has no empty, `.` or `..`
      segment, backslash, or `%2e` / `%2f` / `%5c` / `%25` (so no `/notes/../admin`, no
      `host.evil` or `host@evil`). The link may add its own query and fragment;
    - a `task.update_status` is routed only if its `task` fingerprint appears as a string value
      in the run's collected input; `task.create` binds no target;
    - two entries that may apply the same type to the same target make every such intent
      `intent_ambiguous`, by design: untrusted data never chooses between trusted entries;
    - the proposer's permissions are checked before routing (`intent_rejected` wins);
    - the run record model checks that a route names a real, later entry of the right kind
      that lists the intent.

    Routing binds where an intent goes, not what it says: note content, task titles,
    notification titles and bodies, and `http.post` bodies stay untrusted text, which appliers
    must treat as data. Reusing an unchanged input re-proposes identical intents, `notify.push`
    included, so appliers (#21/#22) must deduplicate notifications (e.g. by `input_hash` and
    intent); reused intents that no longer route are a cache miss and the LLM is called.

### Provisional decisions (#19, pending maintainer review)

31. **Run lifecycle (#19).** `horae.yaml` takes an optional `runs.timeout_s` (default 900, 1 to
    86400 seconds), the run timeout. Per-call limits stay where they are:
    `plugin_defaults.call_timeout_s` for tool calls (the #24 host) and the pipeline's
    `llm.timeout_s` for the LLM step (the engine). Other rules of the runner (`horae.runner`),
    which are not part of a schema (A to G are provisional maintainer decisions):
    - run ids are `<UTC start, %Y%m%dT%H%M%S%fZ>-<pipeline>-<6 hex>`, so they sort by start
      time and match `RunId`; a collision (`RunExists`) is retried once with a fresh id;
    - **(B) liveness is an OS advisory lock.** A run holds `flock(LOCK_EX | LOCK_NB)` on
      `<state_dir>/locks/<pipeline>.lock` (directory 0700, file 0600, opened
      `O_CLOEXEC | O_NOFOLLOW`) for its whole life; the kernel releases it however the process
      ends. A busy lock is retried for 0.2 s, then the run is refused with `PipelineLocked`,
      naming the running run read from the store (its start, pid and host) when there is one;
      nothing is written. With the lock held, any `running` row of that pipeline is dead by
      definition and is abandoned (`failed`, no record, journal kept) as a compare-and-set on
      its id and last heartbeat (`Store.abandon_run(heartbeat_before=...)`), then `begin_run`.
      The store's unique index on running runs stays as a second guard (`PipelineBusy` is
      reported as `PipelineLocked`). Heartbeats (every 10 s) stay for observability but no
      longer decide liveness: there is no stale delay and no pid or host check. Local
      filesystems only (no NFS or SMB, as ADR 0006 already requires for the database). POSIX
      only: Linux (the container target) and macOS (development); without `fcntl` (Windows)
      taking a lock raises `LocksUnsupported` (Windows is unsupported, no `msvcrt` fallback);
    - **(G)** recovery covers only the pipeline being started; other pipelines are recovered
      when they next start. `horae runs` shows a `running` row whose lock is free as
      `running (stale?)` (`"stale": true` in `--json`), checked without waiting and read-only (a
      missing lock file is a free lock; one that cannot be opened is not flagged);
    - **(A)** dry runs take the lock too;
    - **(C)** the heartbeat interval (10 s), the stop grace period (5 s) and the lock retry
      (0.2 s) are constants, not `horae.yaml` settings. A heartbeat waits at most 100 ms for
      the write lock (`HEARTBEAT_BUSY_TIMEOUT_MS`, on a second connection), then skips the
      beat; heartbeat failures of any kind are logged once per run (exception type only) and
      never stop the run;
    - **(D) timeouts.** Past `runs.timeout_s` the run is cancelled and given 5 s to stop. If it
      stops, its partial record is saved `failed` (the interrupted step `failed` with error kind
      `timeout`, later steps `skipped`) and `RunTimedOut(record)` is raised. If it finished
      anyway after the deadline (a step ignored the cancellation), it is saved `failed` with
      the error `the run timed out after N s (it finished after the deadline)`, never `ok`. If
      it still has not stopped, the run is abandoned with the error
      `timeout: the run timed out after N s and its step did not stop within 5 s` and
      `RunTimedOut` is raised with `record = None`;
    - **(F) cancellation.** When the caller cancels `Runner.run` (Ctrl-C, or
      `async with asyncio.timeout(...)`), the partial record is saved (kind `cancelled`; a run
      cancelled before its first step gets a minimal record, every step `skipped`, error
      `the run was cancelled before it started`), passed to the `on_record` callback, and a
      plain `asyncio.CancelledError` is re-raised, so `asyncio.timeout` turns it into
      `TimeoutError`. `RunTimedOut` is a `RunnerError`, not a `CancelledError`. The same 5 s
      grace applies (then the run is abandoned with `cancelled: ...`). A cancellation that
      arrives while the heartbeat stops is not swallowed;
    - a crash (any other exception) abandons the run with `run crashed (<ExceptionType>)`,
      never the exception's message; if that abandon fails, it is logged (types only) and the
      next run of the pipeline recovers the row. A row abandoned by someone else while the run
      runs is noticed by the heartbeat, the next step write or the final save: the run stops
      with `RunLost`;
    - clocks: timestamps and run ids use the wall clock, the run timeout the event loop's
      monotonic clock. A wall-clock step only shifts recorded times (and may reorder run ids
      started across it); since liveness is the lock's, it cannot make a live run look dead;
    - the template time zone is the pipeline trigger's `timezone` when set, else
      `horae.yaml` `timezone`;
    - **(E)** the store is `<state_dir>/horae.db`. `horae runs` lists runs (`--pipeline`,
      `--status`, `--limit`, `--json`) with the exit codes of decision 27. It opens the store
      with `Store.open(path, read_only=True)`: it never creates the store or any file (no
      `-wal` / `-shm` when no writer is active), never switches the journal mode and never
      migrates; a schema older than this Horae exits 1 (`SchemaTooOldError`: "run
      `horae run` once to migrate it"), a newer one too (`SchemaTooNewError`), and so do
      permission and other OS errors. The table has an `ERROR` column: the run's error on one
      line, control and bidi characters collapsed, at most 60 characters. `--json` prints
      `{"schema_version": 1, "instance", "runs": [{id, pipeline, status, stale, dry_run,
      abandoned, started_at, finished_at, duration_s, cost: {usd, tokens_in, tokens_out},
      error}]}`, where `instance` is the resolved absolute path and `error` is the run's full
      error, which carries step messages as capped by the engine (4096 characters).

### Provisional decisions (#21, pending maintainer review)

32. **Dry-run diff (#21).** `horae.apply` plans what applying a stored run would change and
    `horae diff <run-id>` shows it; nothing is applied (that is the journal's, #22). The
    rules (D1 to D8 are provisional maintainer decisions):
    - **(D1) the core owns note conventions.** The `Sink` port (`horae.apply.sink`) has two
      operations: `read(call) -> bytes | None`, a read-only fetch of a `note.*` intent's
      current target (`None`: no such note), and
      `apply(call, *, expected_before_sha256) -> Applied`, a compare-and-swap for #22 (the
      SHA-256 of the bytes the change was planned against, `None` meaning "must not exist";
      a mismatch changes nothing and raises `SinkError("target_changed")`). There is no
      sink-side preview: the core computes each note change from `read()` and
      `horae.apply.notes`, and builds task, notification and HTTP previews from the intent
      alone, without calling the sink, so a plugin cannot show one change and make another.
      What a sink returns is type-checked at runtime (`bytes` or `None`, else
      `preview_invalid`). Over MCP (#24) note bytes travel base64-encoded, at most 16 MiB
      (`MAX_NOTE_BYTES`, `note_too_large`). The sink receives a deep copy of the intent and
      its entry's parameters;
    - **(D2) Horae never creates content in a note it does not own.** A note is owned when
      its file name ends in `.assistant.md`, decided on the path the sink resolved without
      following symlinks. `note.create` (`note_exists` if present) and `note.append_section`
      (`note_missing` if absent) need an owned note. `note.replace_owned_block` creates a
      missing note or block only in an owned note; on any other note it only replaces a
      block that already exists (`not_owned` otherwise);
    - **markers and line breaks.** A block is the lines from `<!-- horae:begin <block> -->`
      to `<!-- horae:end <block> -->`, both included; a marker may end the note without a
      final newline. One case-insensitive recogniser (`horae:(begin|end)`, optionally with a
      block name) serves both directions: in a note, every line naming the block counts and
      must be exactly one canonical begin line followed by one canonical end line
      (surrounding spaces and a final `\r` aside), else `block_ambiguous`; in untrusted text
      (content, heading) it must not appear anywhere (`content_has_marker`). Untrusted text
      may break lines only with `\n` or `\r\n`: a lone `\r`, U+0085, U+2028 or U+2029 is
      refused (`content_line_break`). Inserted lines use the note's line ending (that of its
      first line, `\n` for a new or one-line note); the rest of the note is kept byte for
      byte;
    - **targets (for #22 too).** A sink resolves a note path without following symlinks on
      any component (`symlink`), refuses a file with more than one hard link (`hard_link`)
      and anything but a regular file (`not_a_file`), and decides ownership on that path.
      The built-in `LocalSink` walks the path with `dir_fd` and `O_NOFOLLOW`, opens the note
      `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, checks it with `fstat` and reads from
      the descriptor; a name that is too long or a parent that is not a directory is
      `not_a_file`, a permission or other OS error `unreadable`, a path holding `:` is
      refused (`path_invalid`). POSIX only: Windows is unsupported. `apply` (#22) must use
      the same resolution, on the descriptor it writes through;
    - **chaining.** Several intents on one note are planned in run order, each against the
      bytes the previous one leaves (`follows` names it; the note is read once), which is
      the order #22 applies them in, so `before_sha256` is exactly the
      `expected_before_sha256` of that `apply`. An intent that cannot be planned does not
      change what the next one sees;
    - **(D3) which runs.** `ok`, `noop` and `degraded` runs are planned. A `failed`
      (`run_failed`) or `undone` (`run_undone`) run is refused, and so is a run whose
      `changes` is not empty (`already_applied`: its journal, #22, says what changed);
    - **(D4) `horae diff` exit codes:** `0` planned without problems, `1` configuration or
      store problem, `2` usage error, `3` unknown, unfinished or refused run, `4` planned
      with problems (some intents cannot be applied), `70` internal error. A notification
      already sent (`duplicate`) is information, not a problem. With `--json`, an error is a
      JSON object on stdout, `{"schema_version": 1, "error": {"kind", "message"}}` (kinds
      `config_invalid`, `store_error`, `unknown_run`, `internal`), and a refused run is the
      change set with `"refused": {"kind", "message"}`; without it, errors go to stderr.
      Text output escapes every Unicode Cc, Cf, Zl and Zp character except tab; JSON output
      is ASCII and strict (a non-finite number becomes a string);
    - **(D5) what a dry run may write.** User data never: no vault change, no notification,
      no remote write. Horae's own state database (`<state_dir>/horae.db`) is not user data:
      `horae run --dry-run` (#26) will record its run there, and the write guard of
      `tests/test_dry_run.py` allows only that path (and its `-wal`, `-shm`, `-journal`)
      besides the test's scratch directory. `horae diff` itself opens the store with
      `Store.open(path, read_only=True)`: it creates, migrates and changes nothing (an OS
      error opening it exits 1). The guard covers Python-level writes, metadata changes,
      sockets, name resolution, SQLite connections and process creation; writes made below
      Python (`ctypes`, C extensions) and plugin subprocesses (#24's sandbox) are out of its
      scope;
    - **(D6) the vault** is given with `--vault` only (no environment variable, no
      `horae.yaml` key) until plugin sinks exist (#24); without it note intents are
      `vault_unset` problems;
    - **(D7) notification deduplication.** `dedupe_key` is the SHA-256 (canonical JSON) of
      the pipeline, the routing entry (`applied_by` and its recorded parameters), the run's
      `input_hash` (or, without one, its id), the intent, and its occurrence index (how many
      identical entry and intent pairs precede it in the run). A reused unchanged input
      re-proposes the same keys, while two identical notifications of one run are both
      sent. The applier (#22) records a key once sent (`Store.record_notification`) and
      skips it afterwards; the key reaches the notifier as `SinkCall.dedupe_key`;
    - **(D8)** `horae diff` ships in #21; `horae run --dry-run` printing the same diff is
      wired by #26.

### Provisional decisions (#23, pending maintainer review)

33. **Replay (#23): run record extensions and `horae replay`.** Compatible v1 additions, all
    optional, so records written before them stay valid (they are only not replayable). The
    engine (`horae.engine.execute`) writes them; D1 to D6 are provisional maintainer
    decisions:
    - **(D1) `pipeline_definition`**: the canonical pipeline definition that ran
      (`horae.engine.canonical_pipeline`, the input of `pipeline_digest`), at most 64 KiB as
      compact JSON (`MAX_PIPELINE_BYTES`; omitted when larger). It is named
      `pipeline_definition` because `pipeline` already holds the pipeline's name. The models
      check that it is a valid pipeline named `pipeline`; replay checks that its digest is
      `pipeline_digest`. Replay runs this definition, so a run replays as it ran after its
      file was edited (noted); without it, only a current definition with the same digest
      replays, else "pipeline changed" (unless `--against-current`);
    - **per-call raw proposals** (finding 1): `inputs[].call` (the source's index in its step)
      and `inputs[].intents` (the raw intents it returned, any JSON shape, absent when none);
      a collect source whose proposals failed the step is recorded too. A failed source's
      entry in the step `output` gains `call`. A `transform` step that ran has
      `steps[].calls: [{call, ref, params, input_hash, intents?, error_kind?}]`: each call
      made, `input_hash` the SHA-256 of the canonical JSON of the input it was given
      (intermediate outputs are not recorded). Replay answers each call by `(step, call)` and
      the engine re-validates and re-routes the raw proposals, so a target omitted by the
      proposer is filled again from the (possibly edited) entry, and a rejected or unroutable
      proposal fails the replayed step as it failed the run;
    - **engine-owned LLM fields** (finding 2): `llm[].model_alias` (the pipeline's alias),
      `input_hash` (SHA-256 of the attempt's input; not the cache key, which is
      `input_hash` of the run), `repair` (the errors sent with the attempt) and
      `schema_errors` (each capped at 4096 characters, at most 256; the repair retry is sent
      these capped errors). `request` stays whatever the backend reports, for information;
    - **(D3) `models`** (alias to concrete model, as given to the engine) and
      `lock_digest` / `config_digest`: the engine records what its caller passes; the runner
      (#19) passes `Instance.lock_digest()` (SHA-256 of the canonical JSON of the validated
      `horae.lock`) and `Instance.config_digest()` (SHA-256 of the canonical JSON of the
      validated `models` and `plugins` sections of `horae.yaml`, defaults filled in; the time
      zone, state directory and timeouts are not part of it). Replay uses the recorded
      `models` (the caller's only when the record has none, noted). A recorded digest that
      differs from the current instance's is an error, or a note with `--against-current`;
    - **(D5) `plugins[].provides`**: the tools and skills of the pipeline the run resolved to
      that plugin, sorted. `horae.replay.LockRegistry` resolves a ref to the plugin the run
      resolved it to when that plugin is still locked, else to the locked plugin named after
      its namespace, with the locked version, commit and permissions. So the PLAN.md §3.2
      brief (`coach.brief` from `mcp-brief`, the skill from `pack-daily-brief`) replays from
      the CLI. Replay asks its registry once, up front, for every ref of the pipeline and runs
      the engine on that frozen snapshot; a `PluginRegistry` does no I/O beyond local files and
      starts no process; `horae replay` never uses a live plugin host;
    - **comparison**: canonical pretty JSON of the intents (routing fields included), plus a
      summary of the run status, `input_hash`, step kinds, statuses and error kinds, counts,
      violation kinds and plugin versions (with `provides`). An `undone` run's status is
      compared as it was before the undo, derived from its steps (noted). Messages, timings,
      `stderr` and cost are not compared;
    - **(D4, refined by finding 7) `horae replay` exit codes**: `0` identical, `1`
      configuration or store problem (an invalid instance, a store that cannot be opened, a
      schema older or newer than this Horae: like `doctor` and `runs`), `2` usage error (a bad
      option, an unknown run, no store at the path), `3` not comparable (the pipeline,
      `horae.lock` or `horae.yaml` changed without `--against-current`, an unresolved plugin, a
      call the record cannot answer, a record that is not a valid v1 record or predates these
      fields), `4` different, `70` internal error (its type only). `--json` prints
      `{"schema_version": 1, "run_id", "pipeline", "outcome", "identical", "against_current",
      "definition", "pipeline_digest", "lock_digest", "config_digest", "error", "mismatches",
      "notes", "intents_diff", "summary"}` with every key also on errors (`null` or empty),
      each digest as `{recorded, current, changed}`;
    - **(D6)** `horae replay` opens the store with `Store.open(path, read_only=True)` and never
      creates it;
    - **(D2)** these extensions land with the replay itself (#23), not in a separate change.

    Remaining limits: per-call `stderr` and failure messages beyond their recorded excerpt are
    not reproduced (messages are not compared), and only the first call of a multi-call
    `transform` step has its input checked (later calls get the previous call's output,
    which is not recorded).
