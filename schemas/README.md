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
| Skill | [`skill/v1.json`](skill/v1.json) | `skill/v1.json` | `skills/<namespace>/<skill>/skill.yaml` of a plugin (§2.1, §3.4) |

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
  (`calendar.events: { days: 2 }`), or in the explicit form with a per-call timeout
  (`{ tool: calendar.events, params: { days: 2 }, timeout_s: 90 }`, decision 35). A pipeline
  has **at most one** `llm` step (`contains` + `maxContains: 1`).
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
- `skill`: two `input_view` paths may not overlap (one a prefix of the other) or disagree on
  whether a value is an object or an array (decision 39).
- `plugin-manifest`: the host of an `mcp-http` `runtime.url` is listed in `permissions.network`;
  no tool in `provides` is a sink operation of the plugin's own sink or notifier namespaces
  (`<ns>.read`, `<ns>.apply`, `<ns>.restore`, `<ns>.parent_exists`, `<ns>.identity`,
  decision 36).
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
27. **`horae doctor` (D).** Exit codes (kept by decision 37): `0` no problem, `1` problems
    found, `2` usage error, `70` internal error (`EX_SOFTWARE`; the doctor then prints only the exception type, never
    its message, traceback or locals, which may hold a resolved secret). `--json` prints
    `{"schema_version": 1, "ok", "instance", "problems": [{file, line, column, severity,
    message}]}`; `severity` is `error` or, since decision 38, `warning` (reported, not failing).
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
      `preview_invalid`). Over MCP note bytes travel base64-encoded, at most
      `MAX_MCP_NOTE_BYTES` (about 5.95 MiB, decision 36; `note_too_large`). The sink receives
      a deep copy of the intent and its entry's parameters;
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
      `horae run --dry-run` (#26) records its run there, and the write guard of
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
    - **(D8)** `horae diff` ships in #21; `horae run --dry-run` prints the same change set
      (#26, decision 37).

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

### Provisional decisions (#22, pending maintainer review)

34. **Journal and undo (#22).** No contract changes: the run record already has `changes` and
    the `undone` status. The rules (`horae.apply.apply`, `horae.apply.undo`, the runner):
    - **apply in the runner.** `Ports.sinks` (a `SinkRegistry`) is the runner's apply port.
      After the engine finishes `ok`, `noop` or `degraded` (not a dry run, not timed out or
      cancelled), the runner plans the run (`plan_changes`, with the notifications already
      sent) and applies it (`apply_changes`) while its row is still `running` and its
      heartbeat still beats, then saves the record with `changes` (indices into `intents`,
      with the core's target: note path, task fingerprint, `http.post` URL, or the
      notifier's tool). Without `Ports.sinks` nothing is applied. The apply is not under
      `runs.timeout_s`; each sink call (the plan's reads, the apply's read and apply, undo's
      read and restore) is bounded by `plugin_defaults.call_timeout_s` (kind `timeout`; the
      change stays "maybe applied"). A run whose caller cancels it after its steps finished
      but before the apply is saved `failed` ("cancelled before apply", nothing applied):
      never `ok` with intents that were not applied;
    - **one apply at a time (D6).** The runner's apply phase and `horae undo` hold the
      instance-wide apply lock `<state_dir>/locks/_apply.lock` (the same `flock` helper as the
      pipeline locks; a pipeline name never starts with `_`), in addition to the pipeline
      lock. The runner waits for it at most `APPLY_LOCK_WAIT_S` (30 s), then fails the run
      `apply_locked` with nothing applied; undo does not wait (`apply_locked`, exit 5 since
      decision 37);
    - **journal first.** For each planned change in intent order: `journal_append` (with the
      sink, `applied_by`, where it writes, `sink_identity`, and for a note its bytes before and
      after), then `Sink.apply` (a compare-and-swap on the planned `before_sha256`; the core
      re-reads the note and recomputes the bytes first), then `journal_mark_applied`. Notes
      are undoable; task changes, notifications and HTTP calls are journalled with
      `undoable = 0`. A notification whose `dedupe_key` was already sent
      (`Store.get_notification`) is skipped; once sent, `record_notification` and the mark
      share one transaction (a crash in between can send it twice, never lose it). An
      unchanged note is skipped;
    - **failures (D1).** A plan with problems or a refusal applies nothing (`plan_invalid`,
      `plan_refused`). Otherwise the first failing change stops the apply: earlier changes
      stay applied and recorded, later ones are not applied, and the run is saved `failed`
      with `error` = `<kind>: intent <i> (<sink>) could not be applied: <message>; <n>
      change(s) applied before it, the rest not applied` (kinds `target_changed`, the sink's
      `SinkError.kind` when it matches `^[a-z_]{1,32}$`, `timeout`, `host_error` for any
      other exception or kind, whose message is dropped, `apply_mismatch` when the sink
      reports other bytes than planned, `no_sink`). There is no automatic rollback: a run
      that fails mid-apply stays `failed`, and `horae undo` is the remedy. A crash during the
      apply abandons the run (decision 31); its journal remains. On `apply_mismatch` the
      bytes the sink reports are journalled (`actual_after_sha256`, a blob), and undo accepts
      them as "as the run left it";
    - **the store** (migration 3, unreleased, so edited in place) adds `journal.applied_by`
      (undo finds the sink even for an abandoned run, which has no record),
      `journal.sink_identity` (where that sink writes), `journal.actual_after_sha256` and
      `journal.undo_overwrote_sha256` (both blobs; `prune_runs` keeps every referenced blob),
      and an index on `(target, id)` for the newer-run check. All are NULL for older rows;
    - **sink identity.** A sink may have an `identity` (at most 512 characters;
      `horae.apply.sink_identity`): `LocalSink` / `LocalFileSink` use
      `local:<st_dev>:<st_ino>:<real path>` of the vault, `FixtureSink` a fixed id. Undo
      refuses `sink_mismatch` (exit 1, also under `--force`, before reading anything) when
      the sink an entry's tool resolves to now has another identity than the one recorded
      (e.g. `--vault` names an empty or another vault). A note missing together with its
      directory is never "already restored" when a change of it was applied
      (`parent_missing`: `RestorableSink.parent_exists`). Over MCP the identity is
      the plugin's (decision 36);
    - **undo restores bytes, not intents.** `RestorableSink` adds `read_target(target)`,
      `parent_exists(target)` and `restore(target, content, *, expected_sha256)`
      (`content=None` removes the note), a compare-and-swap. A synthetic `apply` was
      rejected: the closed intent set has no "write these bytes" or "delete" intent, and an
      abandoned run has no `SinkCall` to rebuild. Entries are grouped per (resolved sink
      identity, note) in sequence order, so two tools of one sink editing one note are one
      group: one write of the bytes the note had before the run's first change of it (a note
      the run created is removed), latest note first. Each note is read first: holding its
      original bytes it is *already restored* (resumability after a crash; also a change
      journalled but never applied); with the digest of the last applied entry's `after` (or
      `actual_after`), or of a later entry still `applied = 0`, it is restored; otherwise it
      *changed since* and the whole undo is refused before any write, unless `--force`, which
      keeps the overwritten bytes as a blob (`undo_overwrote_sha256`, reported). Once written,
      the note's entries are marked undone. When every undoable entry is undone, a run with a
      record is saved `undone`. Undoing an undone run is a no-op; a running run or a dry run
      is refused;
    - **last in, first out per note (D8).** If a later run (any pipeline) has an applied, not
      undone change of the same note through the same identity, undo refuses `newer_run`
      (exit 4) naming those runs, unless `--force`, which reports them (`later_runs`);
    - **crashes (D3, D4).** `horae undo` holds the pipeline lock, so the pipeline's `running`
      rows are dead: it abandons them first with the runner's helper
      (`horae.runner.recover_pipeline`, a compare-and-set on the last heartbeat), so a run
      killed mid-apply can be undone at once. An abandoned run stays `failed` (the store's
      constraint), its entries undone. A run with no undoable entry at all is refused
      `nothing_to_undo` (exit 3) and its status is left as it is. Directories created by
      apply are kept;
    - **not undone (D2, D7).** Task changes are not undone (no recorded before-state yet;
      left to the tasks story). Notifications stay sent and their dedupe keys are kept, so an
      undo never causes a surprise re-notification;
    - **`horae undo <run-id>`** (`--vault`, `--force`, `--json`) holds the pipeline's lock and
      the apply lock, opens the store read-write (migrating it) and writes with
      `LocalFileSink` (**provisional** until #24: `LocalSink`'s resolution, every
      file-system call in a worker thread; nothing is created before the compare-and-swap
      passes, then missing directories are made relative to the parent descriptor and each
      new entry's directory is `fsync`ed; the new bytes go to `.horae-<12 hex>.tmp`
      (`O_CREAT | O_EXCL | O_NOFOLLOW`, the note's mode, `fsync`), and just before `os.replace`
      the note's device, inode, size and mtime are checked against the compare-and-swap read;
      a removal renames the note to `.horae-<12 hex>.old`, hashes what it renamed, then
      unlinks it or puts it back with `link` + `unlink`, never over a new note; stale
      `.horae-*.tmp` files older than an hour are swept from touched directories; the
      remaining check-then-rename window is not closed against concurrent editors).
      `LocalSink` stays read-only for `horae diff`. Exit codes follow `horae diff`: `0`
      undone (or nothing left), `1` configuration, store, lock or sink problem, or
      `sink_mismatch`, `2` usage, `3` unknown, running or dry run, or `nothing_to_undo`, `4`
      refused because a note changed or a later run changed it (`--force`), `70` internal.
      `horae replay` used other codes (2 unknown run, 3 not comparable); #26 aligned them
      (D9, decision 37: an unknown run is 3 everywhere, and the pipeline and apply locks are
      5). `--json` prints `{"schema_version": 1, run: {id, pipeline, status,
      abandoned}, force, complete, marked_undone, refused, notes: [{target, applied_by,
      sink_identity, intents, action, status, forced, expected_sha256, found_sha256,
      later_runs, overwrote_sha256}], not_undoable: [{intent, intent_type, target,
      applied_by, applied}], problems}`; errors are `{"schema_version": 1, "error": {kind,
      message}}`.

### Provisional decisions (#24, pending maintainer review)

35. **Plugin host v1 (#24, design note and maintainer answers on #85).** Contract changes,
    both compatible v1 additions:
    - **per-call timeout (answer 8).** A plugin call has a third, explicit form,
      `{tool: <ref>, params: {...}, timeout_s: N}` (`ToolCall`; `params` optional, `timeout_s`
      1 to 600 like `plugin_defaults.call_timeout_s`). It cannot be confused with the mapping
      form, whose single key is a tool ref (always with a dot). The suggested
      `{ref: {params: {...}, timeout_s: N}}` was not used: it is ambiguous with a tool whose
      parameters are named `params` or `timeout_s`. Without `timeout_s` the explicit form is
      the same call as the mapping form and has the same pipeline digest; with one, the
      canonical form keeps `{tool, params, timeout_s}` (a timeout changes how the call runs).
      When omitted, `plugins.<name>.call_timeout_s`, then `plugin_defaults.call_timeout_s`
      apply. The engine passes it to `ToolCaller.call(..., timeout_s=)`;
    - **`secret_in_output` violation (answer 4).** The maintainer's answer "add the secrets
      from the output" is read as: a secret value (or its base64, hex, percent or JSON encoding)
      found in a tool's output or intents is **redacted** (`[REDACTED:<NAME>]`) and recorded
      as a `secret_in_output` violation; the step does not fail. Redaction covers every
      secret the instance resolved (`SecretResolver.resolved()`), in stderr, error messages,
      outputs and intents. A replay leaves this kind out of the compared violations (it
      describes the live process).

    Rules of the host (`horae.plugins.host`), not part of a schema:
    - `mcp>=2.3,<2.4` is a core dependency (answer 1). The host spawns the process itself
      (answer 2; `stdio_client` copies host variables) and keeps the SDK's `ClientSession`,
      which advertises no client capability and refuses sampling, roots and elicitation;
    - the child environment is exactly `PATH` (fixed), `HOME` and `TMPDIR` (empty per-run
      directories), `LANG=C.UTF-8`, `TZ` (the instance time zone) and the declared secrets
      (answer 3), with the deny list of decision 28 (`env_unsafe`);
    - one process per plugin per run, lazily started; one in flight per process; after a
      timeout, crash or protocol failure the process group is killed and the plugin may be
      started **once** more in the run, then `plugin_unavailable` (answer 6);
    - intents travel in the result's `_meta["dev.horae/intents"]` (answer 5); the untrusted
      input of a transform in the request's `_meta["dev.horae/input"]`, so the arguments are
      the trusted parameters only. Data is `structuredContent`, else a single text block that
      parses as JSON (answer 9);
    - **parallel `collect` (answer 7).** The calls of a `collect` step run concurrently (at
      most 8 in flight, started in declaration order); results, snapshots, stderr, violations
      and intents are recorded in declaration order, so records and replays do not depend on
      timing. Every source of the step is called and recorded, even when an earlier one's
      proposals fail the step (sequentially, the later ones were not called). Transforms stay
      sequential (each feeds the next); `act` / `notify` entries are never called;
    - `/proc/<pid>/environ` (answer 10): Horae never adds a resolved secret to its own
      environment, and secret files (decision 25, the default) are recommended over
      `{env: VAR}` references, whose values a same-user plugin can read on Linux. The proper
      fix is the M5 container isolation (#50, #51).

    Security review of the implementation (provisional, pending maintainer review):
    - **A. Sibling plugins.** Plugins run as the same user, so while two run at once one can
      read the other's environment, hence its declared secrets (`/proc/<pid>/environ` on
      Linux, `ps eww` on some systems). Accepted as a pre-M5 risk, mitigated by the
      first-party-only rule of the "declared" tier, and narrowed: the engine releases each
      plugin right after the last step of the plan that calls it (or as soon as the run stops
      early), through an optional `release(plugin)` method of the `ToolCaller`
      (`ReleasingToolCaller`); `PluginHost.release` stops its process, so a plugin is not
      alive during later steps that do not use it. A released plugin called again starts
      afresh without using its one restart; a failing `release` is ignored;
    - **B. Complete redaction from the first call.** When the host opens it resolves every
      secret declared by every locked plugin and every `secrets.refs` entry (one that cannot
      be resolved is skipped; the plugin needing it fails with `secret_missing` later), so
      redaction does not depend on which plugin started first (`SecretResolver.referenced()`
      lists the refs);
    - **C. More encodings.** For secrets of at least 8 bytes the redactor also matches the
      lower- and upper-case hex, and misaligned base64 (both alphabets) of the value embedded
      in a longer stream at byte offsets 1 and 2 (an HTTP Basic `user:<secret>` header): only
      the characters that depend on the value alone, `ceil(8k/6)` to `floor((8k + 8n)/6)` for
      offset `k` and `n` bytes, so up to 4 bits of the value can stay visible on each side;
    - **D. Short secrets and keys.** Encoded and partial forms are used only for secrets of at
      least 8 bytes. A shorter secret is matched by its raw value only, in every channel
      (stderr, messages, string leaves), where it is bounded (not next to an ASCII letter or
      digit: `token=abc123` and `<ref>: abc123` match, `xabc123y` does not), and as a whole
      JSON string or number; an error message is redacted whole first (a secret may contain
      or straddle `: `), then the body of the redacted `<ref>: <message>` is also matched
      as a whole value.
      The host logs a warning (name only) for each secret under 8 bytes it redacts, since its
      encodings are not redacted. *Superseded for short secrets by decision 43 H (#53): a
      secret under 8 bytes is now refused when it is resolved, so the bounded raw-value
      matching and this warning are gone; the rest of D (keys, messages, overlaps, numbers)
      stands.* Object keys
      are never rewritten: a key holding a secret fails the call with `output_invalid` and
      nothing is stored. When the matches of two secrets overlap, only the leftmost one is
      replaced (documented limit). A JSON number whose `json.dumps` is a secret becomes the
      token string; the SDK's own log records (`mcp.*`, `client`, `server`) are scrubbed when
      created: message redacted, arguments, traceback and stack dropped (every logger since
      decision 43 E);
    - **E. umask.** Every plugin process starts with umask `0o077`;
    - **F. Stderr window.** The per-call stderr window keeps its quiet-window heuristic
      (stderr and stdout are separate pipes, so nothing orders them); tests wait for a
      sentinel line instead of relying on timing. What a process wrote before it died between
      two calls goes with the next call; what it wrote after its last call is kept, redacted,
      in `PluginHost.leftover_stderr`;
    - **G. `runtime.command` pinning.** The lock pins version, commit and permissions, not the
      manifest's `runtime.command` or the plugin's files; pinning them is M4 (#44, #46). A
      relative command must resolve (symlinks included) inside the plugin's directory, and is
      passed as an absolute path;
    - the deny list (decision 28) also covers `GLIBC_TUNABLES`, `JAVA_TOOL_OPTIONS`,
      `JDK_JAVA_OPTIONS`, `CLASSPATH`, `PERL5LIB`, `PERLLIB`, `RUBYLIB`, `SSLKEYLOGFILE`,
      `OPENSSL_*`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `LOCPATH`,
      `GCONV_PATH`, `TZDIR`, `LANGUAGE`, `SHELLOPTS`, `PS4`, `GODEBUG`, `MALLOC_*`, `UV_*`,
      `PIP_*`, `NPM_CONFIG_*` and `VIRTUAL_ENV`;
    - calls to one plugin are serialised, so N calls to it in one step can take N times the
      call timeout; the run timeout (#19) bounds the whole. Group termination cannot reach a
      descendant that left the group (`setsid`/`setpgid`), and the final `killpg` after the
      leader is reaped has a (very unlikely) PID-reuse race; both are M5's (#50, #51).

### Provisional decisions (#25, pending maintainer review)

36. **Plugin sinks, fixture plugins and the walking skeleton (#25).** One contract rule (the
    manifest's reserved sink operation names, a pydantic-only rule: the generated schemas do
    not change). The rules (`horae.plugins.host.sink`, `horae.plugins.builtin`,
    `horae.runner.open_ports`):
    - **sink operations by convention.** An `act` / `notify` entry's tool (`<ns>.<tool>`, in
      `provides.sinks` or `provides.notifiers`) is never called. The core applies the intents
      routed to it through five tools of the same plugin, named `<ns>.read`, `<ns>.apply`,
      `<ns>.restore`, `<ns>.parent_exists` and `<ns>.identity`
      (`horae.contracts.plugin.SINK_OPERATIONS`), which the plugin lists in `tools/list` but
      **not** in `provides`, so no pipeline step can call them (the engine only calls provided
      tools); the core calls them with `PluginHost.call_operation` (same process, timeouts,
      restart and redaction as any call). `McpSinks` is the `SinkRegistry` (`Ports.sinks`):
      one `McpSink` per (plugin, namespace);
    - **reserved names.** The manifest contract refuses a plugin that provides `<ns>.<op>` for
      an operation `op` and one of its own sink or notifier namespaces `ns`; `HostRegistry`
      refuses any plugin providing a sink operation name of **any** installed plugin's
      namespaces (and a manifest built without validation). `call_operation` only calls
      `tool == f"{ns}.{op}"` with `op` in `SINK_OPERATIONS`, `ns` a sink or notifier namespace
      of that plugin and `tool` provided by no plugin (`tool_missing` otherwise): it is not a
      way to call arbitrary unprovided tools;
    - **the core computes the bytes (D1 of decision 32).** For a `note.*` intent `McpSink.apply`
      reads the note (`<ns>.read`), checks the planned SHA-256 (`target_changed`), computes the
      new bytes with `horae.apply.notes.note_after` and sends them to `<ns>.apply` with
      `expected_before_sha256`: the plugin only writes them, atomically, if the note still has
      that digest, and reports the digest of what it wrote. Other intents are passed through
      (`content` and `expected_before_sha256` `null`). `restore` is the same compare-and-swap
      for undo (`content: null` removes the note);
    - **`apply_mismatch` over MCP has no `actual_after`.** A sink reporting another digest than
      the planned one fails the change with `apply_mismatch`; only the digest came back, not
      the bytes, so the journal entry keeps `applied = 0` without `actual_after`, and
      `horae undo` takes the note for "changed since": restoring it needs `--force` (which
      keeps the overwritten bytes in the journal, decision 34);
    - **double read (accepted).** `apply_changes` reads a note to check it against the plan,
      and `McpSink.apply` reads it again to build the bytes it sends: two `<ns>.read` per note
      change. The adapter never sends bytes computed from a read it did not make itself, notes
      are small, and merging the reads would couple the adapter to the apply phase;
    - **one note-size cap over MCP.** Note bytes are standard base64, at most
      `MAX_MCP_NOTE_BYTES` = (`MAX_MESSAGE_BYTES` 8 MiB − a 64 KiB envelope margin) × 3/4 =
      6,242,304 bytes (about 5.95 MiB) decoded, either way (`note_too_large`; the built-in
      `LocalFileSink` keeps 16 MiB). A sink sends each result once, as `structuredContent`
      with a short text block (no second copy of the payload). The cap is checked before
      encoding on both sides: `McpSink.apply` / `restore` refuse larger content before the
      call, and `file-sink.read` of a larger note returns `note_too_large` (never an oversized
      message, which would cost the plugin its process with `output_too_large`). Malformed
      results are `output_invalid`. A sink error is a result `{"error": {"kind", "message"}}`:
      the kind must match `^[a-z_]{1,32}$` (else `host_error`), the message (already redacted
      by the host) loses its control and format characters and is capped at 500 characters. A
      host failure becomes `SinkError` with the host's kind (`timeout`, `plugin_crashed`,
      `tool_missing`…);
    - **`secret_in_output` from sink operations.** It is logged by secret name only (the host
      redacted the value) and kept by `McpSinks`; `apply_changes` adds those collected during
      a run (the runner's diff and apply) to `RunRecord.violations`
      (`McpSinks.take_violations`, deduplicated, at most 256 in all). Outside a run
      (`horae undo`, a dry run's diff built outside the runner) there is no record: they are
      only logged;
    - **trusted configuration.** `horae.yaml` has no per-plugin configuration in v1, so every
      sink operation receives `paths`, the plugin's path permissions as approved in
      `horae.lock`, and every tool call carries them in `_meta["dev.horae/paths"]`.
      `file-sink` writes to its single `paths.write` entry (`vault_unset` without one,
      `config_invalid` otherwise); its manifest asks for no path, since where the vault is is
      the user's choice, granted in the lock. **This channel stays for v1**: M4's
      `horae plugin add` (#44) must carry the `paths.write` entry into the lock it writes (a
      per-plugin configuration in `horae.yaml` is a later decision);
    - **identity (decision 34).** `mcp:<plugin>:<what <ns>.identity returns>` (its SHA-256 when
      longer than 512 characters). `sink_identity` stays synchronous, with a **mandatory**
      asynchronous prepare done by the core: a registry may have `prepare(tools)`, which
      `horae.apply.prepare_sinks` awaits before any identity is read, with only the tools
      about to be used (`apply_changes`: the run's `applied_by` tools, so a `notify.push` or
      `task.*` change, which reads nothing first, still journals its identity; `undo_run`:
      the journal's undoable entries). No caller prepares by hand. Until it is fetched the
      identity raises `identity_unknown`. Definitive failures (`identity_invalid`,
      `vault_unset`, `config_invalid`) are cached; any other kind (host kinds: a timeout, a
      crash) is retried once on the next prepare, then cached. `file-sink` returns
      `local:<st_dev>:<st_ino>:<real path>` of its vault, so undo refuses another vault
      (`sink_mismatch`). The prefix keeps a plugin from claiming the identity of the built-in
      `LocalFileSink`: `horae undo --vault` refuses a run applied through `file-sink`; without
      `--vault`, `horae undo` goes through the plugin host (#26, decision 37);
    - **built-in fixture plugins.** `fixture-source` (`fixture-source.serve`, data and intents
      from a JSON fixture file, default the packaged one; a `file` must be a regular file whose
      real path is inside a `paths.read` entry approved in the lock, opened with `O_NONBLOCK`
      and checked with `fstat`; its manifest asks for no path, and the walking skeleton's lock
      grants none, so only the packaged fixture is served there) and `file-sink`
      (`file-sink.write_note`, built on `LocalFileSink`, so the same path and write rules) are
      stdio MCP servers in `horae.plugins.builtin`, version 0.1.0. Their packaged manifests run
      `python3 -m <module>`; `horae.plugins.builtin.install(directory)` writes them to
      `<directory>/<name>/plugin.yaml` (mode 0644, a temporary file then `os.replace`, no
      symbolic link followed for the directories, `InstallError` when `sys.executable` is
      empty) with the command pinned to the running interpreter, until M4 installs plugins
      into their own environments (#44, #46);
    - **placeholder commit for built-ins.** The walking skeleton's lock pins both plugins at
      commit `0000000000000000000000000000000000000000` (a commit of the core cannot name the
      commit that contains it). It is **not fetched**: the plugins ship inside the core. M4's
      fetch and verify (#44, #46) must special-case built-in plugins rather than try to
      resolve that commit;
    - **live ports.** `horae.runner.open_ports(instance, plugins_dir)` builds the
      `HostRegistry` (`HostRegistry.from_directory`), opens a `PluginHost`
      (`host_for_instance`) and `McpSinks` for one run or one undo, and closes the host at the
      end of the block. Where plugins are installed is M4's; `horae run`, `horae diff` and
      `horae undo` open it on `<instance>/plugins` (#26, decision 37);
    - **the walking skeleton** (`examples/walking-skeleton`) pins both plugins with the
      placeholder commit and a vault path to replace (in a copy of the directory);
      `tests/test_e2e.py` builds its lock from it at test time. The end-to-end test runs dry
      run (under the write guard, which then allows the plugins' processes and Horae's own
      state: the store, `<state_dir>/locks` and `<state_dir>/plugin-runs`; the vault is also
      compared byte for byte), run, replay and undo with real subprocesses, and fails when the
      module takes 60 s or more;
    - **store first open (a flake fix found here).** `Store.open` (writable) initialises a
      database (create, WAL, migrate) under a POSIX advisory lock `<db>.init.lock` (0600,
      `O_NOFOLLOW`, `flock` retried with full-jitter backoff for at most the busy timeout, then
      `StoreBusy`); an open whose database header already says WAL and the current schema
      version skips it. Read-only opens never take it nor create the file. The lock file is
      left in place (removing it would race with the next opener).

### Provisional decisions (#26, pending maintainer review)

37. **CLI commands: one exit-code table, `--json` everywhere, `horae init` and `horae run`
    (#26).** No contract changes. The rules (`horae.cli`, `horae.cli_output`,
    `horae.cli_init`, `horae.cli_run`, `horae.cli_host`, `horae.cli_undo`):
    - **exit codes, the same for every command** (`init`, `run`, `runs`, `diff`, `undo`,
      `replay`, `doctor`; each command's `--help` lists the ones it uses, `horae --help`
      the table): `0` ok (no problem, listed, planned, identical, undone, created); `1`
      a configuration, store, plugin or runtime problem (doctor's problems, a run that
      `failed` or timed out or was lost, a sink failure, `sink_mismatch`); `2` usage error
      (a bad option, a missing argument, a malformed `--var`); `3` unknown or refused (an
      unknown run or pipeline, a refused run: failed, undone, already applied, a dry run,
      nothing to undo; a replay that cannot be compared; a non-empty `horae init` target);
      `4` different (a replay differs; a plan with problems, from `horae diff` or
      `horae run --dry-run`; an undo refused because a note changed); `5` locked (the
      pipeline is running, `pipeline_locked`; the apply lock is held, `apply_locked`: undo
      at once, a run after `APPLY_LOCK_WAIT_S`); `70` internal error (`EX_SOFTWARE`, only
      the exception's type is printed); `130` / `143` `horae run` interrupted by SIGINT /
      SIGTERM (128 + the signal; a `KeyboardInterrupt` reaching any other command is `130`,
      kind `interrupted`). Changes from decisions 32 to 34: `horae replay` of an
      unknown run (or with no store) is `3`, not `2`; `horae undo` refused by a lock is
      `5`, not `1`. Decisions 27 and 31 (E) are unchanged;
    - **`--json` on every command**: exactly one JSON document on stdout, ASCII, strict
      (`horae.apply.render.dumps`), `schema_version: 1`. An error is
      `{"schema_version": 1, "error": {"kind", "message"}}`, usage errors included (kind
      `usage`; `JsonUsageCommand` recognises `--json` among a command's arguments, before
      any `--`; `JsonUsageGroup` does the same for the `horae` group's own errors, `horae
      --json` and `horae nope --json`); `horae replay` keeps its report with every key and
      outcome `error`, its `error` being the same `{kind, message}` object (`null` when
      there is none; kind `not_comparable` for a replay that cannot compare), and `horae
      doctor` its report (`instance` is the resolved absolute path). An `OSError` is
      `store_error` when it names the store's files, else `io_error` (the file it names and
      its `strerror`, never the exception's text). Without `--json`, results go to stdout and errors to
      stderr as one line, `horae <command>: <message>`, with untrusted text escaped
      (`safe`: Unicode Cc, Cf, Zl and Zp except tab). No colour unless asked
      (`horae diff --color`). Common error kinds: `config_invalid`, `plugins_invalid`,
      `store_error`, `unknown_run`, `unknown_pipeline`, `pipeline_locked`, `apply_locked`,
      `internal`;
    - **`horae init [DIR]`** (default: the current directory) writes a working example
      instance, the walking skeleton: `horae.yaml` and `pipelines/daily-fixture.yaml` (the
      packaged `horae/init_template`, kept identical to `examples/walking-skeleton` by a
      test), a generated `horae.lock` (both built-in plugins at their version, the
      placeholder commit of decision 36, the permissions their manifests ask for, and
      `file-sink`'s `paths.write` set to the instance's own `vault/`, an absolute path),
      an empty `vault/`, `state/` (0700), `plugins/` (`horae.plugins.builtin.install`) and a
      `.gitignore` for those three. **It refuses** (exit 3, `target_not_empty` or
      `target_invalid`) a target that is not a new or empty directory (a file, a symbolic
      link, anything inside it): no `--force`, nothing is ever overwritten (files are
      opened `x`). **It is atomic**: the lock is validated first (a path it cannot hold, a
      backslash for instance, is `target_invalid`, exit 1, nothing written); the instance is
      built in a temporary directory, then a new target is one `rename` of a hidden
      sibling (`.horae-init-*`), and an existing empty target (opened `O_NOFOLLOW`) gets
      the entries of a hidden directory built inside it renamed up into it (the directory
      itself is kept, so a shell inside it stays there, and its parent need not be
      writable); on any failure the temporary directory, the entries moved and the parents
      `init` created are removed. `--json` prints `{schema_version, instance, vault,
      pipelines, plugins, files}`. **Instances are not relocatable in v1** (provisional):
      `horae.lock` holds the vault's absolute path and the installed manifests the absolute
      path of the interpreter; `init` says so, and `horae doctor` reports a copied or moved
      instance (below); Afterwards `horae doctor` passes and `horae run daily-fixture` writes
      `vault/Daily/<date>.assistant.md`;
    - **`horae run <pipeline> [--dry-run] [--var KEY=VALUE]... [--json]`**: loads the
      instance (`config_invalid`), checks the pipeline (`unknown_pipeline`, 3) and the
      installed plugins (`HostRegistry.from_directory` on `<instance>/plugins`:
      `plugins_invalid`, 1, before any process starts or any file is written), then
      `open_ports` and `Runner.run` (lock, recovery, execute, apply through `McpSinks`,
      whose identities the core prepares, decision 36). `--var` names match `VarName` and
      may not be a built-in variable (`now`, `timezone`, `date`; usage, 2); at most
      `MAX_ITEMS - 1` of them (the context holds `date` too; usage, 2), and a malformed one
      echoes only its name part (before `=`, truncated to 64 characters), never its value.
      A dry run records its run and prints the change set it would apply (`plan_changes`,
      rendered like `horae diff`, without a second header line; with problems, exit 4;
      when a sink plugin could not start, `plugin_unavailable`, `spawn_failed`,
      `startup_timeout` or `handshake_failed`, exit 1, kind `plugin_unavailable`: a
      problem, not a difference). The run's status gives the code: `ok`,
      `noop`, `degraded` 0; `failed` 1 (`run_failed`; `apply_locked` 5); `RunTimedOut` 1
      (`timeout`), `RunLost` 1 (`run_lost`), `PipelineLocked` 5. `--json` prints
      `{schema_version, run_id, run: {id, pipeline, status, dry_run, started_at,
      finished_at, intents, changes: [{intent, target}], cost, violations, error_kinds,
      error}, plan, error}` (`run_id` as soon as the run's row exists, from
      `Runner.run(on_begin=)`, also when no record was saved; `run` is `null` when none
      was, `plan` without `--dry-run`);
      `error_kinds` are the failed steps' kinds (also appended to the text error). **No LLM
      backend in M1**: a pipeline with an `llm` step fails with `llm_unavailable` (exit 1);
      M2 wires the backends into `open_ports(llm=...)`. **Signals** (follow-up of decision
      31 F): SIGINT and SIGTERM handlers (`loop.add_signal_handler`, removed afterwards)
      cancel the run's task and the command exits 130 / 143, naming the run. **During the
      engine phase** the runner saves the partial record (`failed`, kind `cancelled`),
      which is printed. **The apply phase is shielded** (provisional): once it has started
      planning, a cancellation is remembered, the apply finishes (each sink call bounded by
      `call_timeout_s`), the record is saved with its real status (its changes were
      applied: `ok`, say) and only then is the run reported interrupted; a cancellation
      while it waits for the apply lock stops the wait (`failed`, "cancelled before
      apply", nothing applied). If the run is nonetheless abandoned during the apply (a
      crash), its error says "run crashed during apply (<type>); run `horae undo <id>` to
      revert what was written", the command prints "abandoned during apply; run `horae
      undo <id>`" and `--json` has the id; a cancellation that abandons a run says
      `cancelled: ...`, never `run crashed (CancelledError)`. **A second signal while the
      run stops is ignored**: the runner's grace period (`STOP_GRACE_S`) still bounds the
      stop, after which the run is abandoned as before. Secrets: the run's error and
      messages come from the record and the host, which redact them; internal errors print
      the exception type only;
    - **sinks for `horae diff` and `horae undo`**: through the plugin host (`open_ports`,
      `McpSinks`) when the instance has a `plugins/` directory, so a run applied by
      `file-sink` is planned and undone by `file-sink` (decision 36's identity). `--vault`
      stays as an explicit override: the built-in `LocalSink` (diff, read only) or
      `LocalFileSink` (undo) on that directory, for instances without plugins or runs
      applied by the built-in sink; undo through it refuses a run applied by a plugin
      (`sink_mismatch`). Without plugins and without `--vault`, the built-in sinks have no
      vault (`vault_unset`), as before. `horae diff` through the host starts the plugins,
      which only read (`<ns>.read`); the store is still opened read-only;
    - **where plugins are installed**: `<instance>/plugins/<name>/plugin.yaml`
      (`horae.cli_host.PLUGINS_DIR`), until M4's `horae plugin add` (#44) decides otherwise;
    - **`horae doctor`** checks, beyond the configuration (`check_instance`,
      `horae.cli_host.instance_problems`): every `paths.read` / `paths.write` entry of
      `horae.lock` exists; an instance with its own `vault/` that the lock does not write
      to is a problem (a copied instance still writing to the original's vault); with a
      `plugins/` directory, the plugins match the lock (`check_plugins`) and each stdio
      command (its pinned interpreter) exists and is executable (`resolve_command`). Each
      is a problem (exit 1) naming `horae.lock` or the plugin's manifest;
    - **follow-ups** (out of scope here): enforcing the manifests' `horae` version range
      against `horae.__version__`; whether a second identical run should be `noop` rather
      than `ok`.

### Provisional decisions (#28, pending maintainer review)

38. **LLM backend interface, the `anthropic` backend, usage and cost (#28).** Compatible v1
    additions and the rules of `horae.llm` (interface) and `horae.llm.backends.anthropic`
    (SDK `anthropic` 1.17 over `httpx2`, Messages API, `anthropic-version: 2023-06-01`):
    - **contract changes** (all optional): `models.<alias>.price` in `horae.yaml`,
      `{input_per_mtok, output_per_mtok, cache_read_per_mtok?, cache_write_per_mtok?}`, US
      dollars per million tokens, each `0` to `10000`; `models.<alias>.max_output_tokens`
      (`1` to `1000000`), a cap on every call's `max_tokens`; `base_url` of a `backend:
      anthropic` alias must be `https://` (C). `Usage` (run records: `llm[].usage`, `cost`)
      gains `cache_read_tokens`, `cache_write_tokens` (when the provider reports them;
      `tokens_in` stays the input billed at the full price) and `priced` (`false` when the
      alias has no `price`: `usd` is then `0`). Without a cache price, cache reads are priced
      as input tokens and cache writes at twice the input price (conservative). A `price` is
      part of the `models` section, so changing it changes `config_digest` (decision 33),
      not the LLM cache key. The `horae doctor` report's `severity` may now be `warning`
      (decision 27): a warning is printed and listed but does not fail the check (`ok` stays
      `true`, exit `0`);
    - **A. no shipped price list**: prices change and differ per account, so the instance
      states the ones it pays. Without one, only `tokens_out` bounds the step, so `horae
      doctor` warns about each cloud (`anthropic`) alias used by an `llm` step that has no
      `price`;
    - **B. pre-flight budget**: `ModelBackends.render` refuses the attempt with
      `budget_exceeded`, before any request (and before the key is read), when the remaining
      `tokens_out` (capped by `max_output_tokens`) is below 1, or, with a price, when the
      input alone may cost more than the remaining `usd` or what is left buys no output
      token. The input is bounded without a `count_tokens` call: a token is at least one
      UTF-8 byte, so the canonical JSON size of the whole rendered payload (system,
      messages, schema) plus a margin of 512 tokens (`INPUT_TOKEN_MARGIN`, for the framing
      and structured-output instructions the provider adds) is the bound; `max_tokens` is
      then what the remaining `usd` buys after that bound. The engine still checks the
      reported spend after the call;
    - **C. explicit network path**: the core LLM client ignores the ambient environment: no
      `HTTP(S)_PROXY` / `ALL_PROXY`, no `.netrc`, no `SSL_CERT_FILE` / `SSL_CERT_DIR` (an
      explicit `httpx2.AsyncHTTPTransport(trust_env=False, verify=True)` and a client with
      `trust_env=False`; `verify=True` without `trust_env` is `httpcore2`'s default context
      on the system trust store, which reads no `SSL_CERT_*`, whereas
      `ssl.create_default_context()` would, through OpenSSL's default verify paths).
      Redirects are not followed; OpenTelemetry spans are off. An explicit proxy or CA option
      (`models.<alias>.proxy` / `ca_file`) is a later decision;
    - **D. intents**: raw intents live in a reserved top-level `intents` property of the
      skill's output object. `ModelBackends` validates the whole output, then, only when
      `intents` is present, requires a list (else `output_invalid`, with the usage), moves it
      into `LlmResponse.intents` and removes it from `output`. #27 (skill format) reserves
      the name in skill output schemas; #31 validates against the schema including it; #35
      routes the intents;
    - **E. what is recorded**: `LlmCall.request` is a summary, not the payload: `backend`,
      `model`, `max_tokens`, `body_sha256` / `body_bytes` (the exact bytes sent), and the
      SHA-256 and size of the skill prompt (`system_*`), the canonical input (`input_*`) and
      the output schema (`schema_*`), with `repair_errors` (how many errors were sent). The
      input is already in the run's data and `LlmCall.input_hash`; the full body is never
      recorded, dry runs included (they make no LLM call);
    - **G. the #31 validator is mandatory before production use**: structured outputs drop
      the constraints they do not support (`anthropic.transform_schema` moves them into
      descriptions), so without `ModelBackends(validate=...)` an output may break
      `maxLength`, `pattern`, numeric bounds and the like;
    - **two layers**: `ModelBackends` is the engine's `LlmBackend` (alias → backend, model,
      `base_url`, `secret`, `price`; prompt from a `PromptSource`; `max_tokens` from the
      budget); a `ProviderBackend` per alias makes one request with no tools. Both split an
      attempt into `render` (pure: the exact provider payload, a `RenderedRequest` whose
      `body()` is canonical JSON bytes frozen at render time; no I/O, no secret) and `send`
      (one network call with a fresh copy of exactly those bytes; a payload changed after
      rendering is refused with `invalid_request`), so the core can inspect what leaves the
      host first (T10, #34/#53); `complete` is `send(render(...))`;
    - **the API key comes from the declared secret only** (`models.<alias>.secret`, through
      the instance's `SecretResolver`, decision 25), resolved on the first call. The SDK reads
      `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, credential profiles and
      `ANTHROPIC_BASE_URL` only when it is not given a key and a URL, so it always gets a
      placeholder key and an explicit `base_url` (default `https://api.anthropic.com`). SDK
      retries are disabled (`max_retries=0`): the engine owns the single repair retry;
    - **headers are rebuilt**: the SDK merges `ANTHROPIC_CUSTOM_HEADERS` from the
      environment into every request, which can override any header. A request hook on its
      HTTP client refuses any request but `POST <base_url>/v1/messages` to the alias' origin
      and replaces every header with exactly `host` (from the request URL), `accept` and
      `content-type` (`application/json`), `content-length` (the body's length),
      `anthropic-version` (`2023-06-01`), `user-agent` (`horae`) and `x-api-key` (the
      declared secret, set at the last moment). No `Authorization`, `anthropic-beta`,
      `x-stainless-*` (platform, retry or timeout) or `accept-encoding` header is sent. A
      refusal carries its reason in its own exception (no shared state);
    - **logging**: at `DEBUG` (`ANTHROPIC_LOG=debug` or a logging configuration) the SDK logs
      request options, the prompt and the input included. Building a client pins the
      `anthropic`, `httpx2` and `httpcore2` loggers, and their children that set a lower
      level, to `WARNING`;
    - **request shape**: `system` is Horae's preamble (the user message is untrusted data,
      never instructions; answer with JSON matching the schema), then the skill's rendered
      prompt. One user message holds the input as JSON inside `<input>` tags, then, on the
      repair retry, the previous attempt's schema errors as a JSON array inside
      `<schema_errors>` tags (framed as data, not instructions). In both, every `<` is
      written as the JSON escape `<` (valid JSON, since `<` only occurs inside
      strings), so the data cannot close its tag. The output is constrained with structured
      outputs (`output_config.format: {type: json_schema, schema}`, the schema passed through
      `anthropic.transform_schema`); the text must parse as one JSON value: no `NaN` or
      infinite number (`1e999`), no duplicate key, no nesting beyond the parser's limit;
    - **error kinds** (messages are Horae's: the HTTP status and the API error `type`, never
      the API's text; raised outside the SDK's exception handler, so neither `__cause__` nor
      `__context__` reaches the SDK error and its request): `timeout` (SDK timeout, 408, 504),
      `rate_limited` (429), `auth_failed` (401, 403, an unresolvable secret), `overloaded`
      (529, 503), `invalid_request` (400, 404, 409, 413, 422, an unknown alias, a schema
      structured outputs cannot take, a payload changed after rendering), `output_invalid`
      (refusal, `max_tokens` or context-window truncation, tool or other non-text content,
      unacceptable JSON, `intents` not a list), `host_error` (connection failure, a refused
      request, another status); `budget_exceeded` (B), `backend_unavailable` (an alias whose
      backend has no implementation yet) and `skill_unavailable` (no prompt for the skill)
      come from `ModelBackends`. Every rejection after a successful HTTP response carries the
      usage the response reported; when it reported none usable, the call is charged
      conservatively (the input bound and the whole `max_tokens`);
    - **wiring**: `open_ports` gives the runner a `ModelBackends` over the instance's
      aliases when a pipeline has an `llm` step (sharing the host's `SecretResolver`; lazy;
      closed with the ports), so `horae run` no longer fails with `llm_unavailable` (decision
      37) when an alias is configured. `horae doctor` reports a model alias used by an `llm`
      step whose backend is not available (`models.<alias>.backend`), besides its secret;
    - **integration points (#27)**: the prompt, viewed input, output schema and skill version
      come from the `LlmRequest` (`RequestPrompts` reads the fields #27 added: `prompt`,
      `output_schema`, `skill_version`). A request without a prompt or schema (an engine
      given no `SkillSource`) stops with `skill_unavailable` before any request;
      `skill_unavailable` means "no usable prompt for this skill here", and #27's
      `skill_invalid` (a skill that is present but malformed) is reported while planning,
      before the LLM layer is reached. `open_ports` (`horae.runner._ports`) builds both the
      owned `ModelBackends` and the `DirectorySkills` source (`Ports.skills`, decision 39);
      the reserved `intents` property is the one #27's schema check requires of a skill
      that declares intents (`horae.skills.INTENTS_PROPERTY` is `INTENTS_KEY`).

### Provisional decisions (#27, pending maintainer review)

39. **Skill format and loader (#27).** A new contract, `schemas/skill/v1.json`
    (`horae.contracts.skill.SkillManifest`), the loader `horae.skills`, and one optional
    run-record field, `llm[].skill_digest` (a compatible addition). Numbered 39 because #28
    (decision 38) merges first. Choices to verify:
    - **(A) layout.** A skill `<namespace>/<skill>` is the folder
      `skills/<namespace>/<skill>/` of the plugin that lists it in `provides.skills`
      (`HostRegistry.plugin_for_skill`, then the plugin's installed directory). The namespace
      is a folder level so one plugin can ship skills of two namespaces without a clash. The
      folder holds `skill.yaml`, the prompt (`prompt.md` by default) and the output schema
      (`output.schema.json` by default); other files are ignored;
    - **(B) `skill.yaml` and race-safe reads**: `name` (must equal the skill ref), `version`
      (semver), `description` (one line), `input_view` (1 to 256 paths), optional `prompt` and
      `output_schema` file names, optional `intents`. File names are **one path segment**
      (`^[A-Za-z0-9_][A-Za-z0-9_.-]*$`), so `../`, absolute paths and hidden files cannot be
      written. The loader opens the plugin root and walks `skills`, the namespace and the
      skill one component at a time relative to the previous descriptor (`dir_fd`), each
      `O_DIRECTORY | O_NOFOLLOW`, then opens each file relative to the skill folder with
      `O_NOFOLLOW | O_NONBLOCK` and checks it with `fstat`: a regular file with exactly one
      hard link. **Every symbolic link inside the skill tree is refused** (a symlinked skill
      folder, namespace or `skills` folder too, and a link between two files of the folder),
      and so is a hard link to a file elsewhere. Swapping a component for a link between a
      check and the read cannot redirect the read. Sizes: `skill.yaml`, prompt and schema at
      most 64 KiB each. Every problem, including a non-finite number, a lone surrogate, a
      `$ref` chain or a recursion too deep, is a `SkillError` (`skill_invalid`);
    - **(C) input view: dotted paths with `[]`**, not JSON Pointer: `tasks[].title`,
      `[].data` (a `collect` output is an array at the root), `matrix[][].v`. JSON Pointer
      names one location (`/tasks/0/title`) and has no "every element", and its `~0`/`~1`
      escapes are hard to review in YAML. Names are `[A-Za-z_][A-Za-z0-9_-]*`: a key with
      another character cannot be selected in v1; the bracket-quoted form (`["a.b"]`) is
      reserved for it. A path selects the whole value it ends on; overlapping or
      shape-contradicting paths are refused. `input_view` is a list of paths **or a mapping
      `path: {options}`** (for #34's field classes, `events[].attendees[]: {class: person}`):
      options are free-form but bounded (at most 16 per path, names like the paths', values
      booleans, finite numbers, one-line strings or lists of at most 64 of them); `class`,
      when given, is one of `person`, `email`, `phone`, `place`, `amount`, `free_text`. v1 does
      not act on options; they join the skill's digest only when present, so a mapping without
      options has the list form's digest. **Applying** the view (`InputView.apply`) keeps
      exactly the declared keys that are present, maps `[]` over arrays (keeping order,
      dropping elements of the wrong shape) and re-encodes the result with `sort_keys`, so
      object keys are sorted at every depth (inside selected whole values too) and nothing
      is shared with the input; a value of the wrong shape selects nothing (the root: `null`,
      see G). Missing fields are not an error: the input is untrusted data;
    - **(D) prompt: no template syntax in v1.** The prompt is sent verbatim; `{{` and `}}` are
      refused so a later syntax (e.g. `{{date}}` from the run context, never from the input)
      is a compatible addition. It must be non-empty UTF-8 without a character of the Unicode
      categories `Cc`, `Cf`, `Co`, `Cs`, `Cn`, `Zl`, `Zp` (but tab and newline: no CR, so the
      digest does not depend on line endings), which covers the bidirectional controls,
      zero-width characters, U+00AD, U+180E, the tag characters U+E0000 to U+E007F and U+2028 /
      U+2029, nor another invisible character (the variation selectors U+FE00 to U+FE0F and
      U+E0100 to U+E01EF, the combining grapheme joiner, the Hangul fillers). The input is
      never interpolated into the prompt: it travels separately, as data (tenet 5);
    - **(E) output schema: draft 2020-12, self-contained, regex-free and bounded**
      (`horae.skills.check_output_schema`): the metaschema, an optional `$schema` that must
      be 2020-12, a root `"type": "object"`, only local `$ref`s (at most 32 chained, no cycle,
      no `%`: the validator percent-decodes pointers; resolved with `referencing`, as the
      validator resolves them), every `$defs` / `definitions` entry bounded even when
      unreferenced, and no `$id`, `$anchor`, `$dynamic*`, `$recursive*` or `$vocabulary`; **no `pattern`,
      `patternProperties`, `propertyNames` or `"format": "regex"`** (a plugin author's regex
      is a ReDoS risk; #31 must still validate outputs in a worker it can kill on a timeout);
      every subschema a value meets has a `type`, `enum`, `const`, `$ref` or combinator; a
      `string` needs `maxLength`, an `array` `maxItems` (and `items` past its `prefixItems`),
      an `object` `additionalProperties: false` (as the backends' strict structured-output
      modes require), an `integer` or `number` a finite lower and upper bound. The **size
      bound** is in **UTF-8 bytes** of the compact JSON (`separators=(",", ":")`,
      `ensure_ascii=False`) and is a sound worst case: a string counts `6 * maxLength + 2`
      (every code point escaped as `\uXXXX`), a number the digits of its larger extreme (an
      `integer` two more, for `3.0`; a `number` or an extreme of 10^15 or more at least 25, the
      longest float `repr`), `enum` / `const` their longest serialisation (`1` may be `1.0`);
      `anyOf` / `oneOf` take the largest branch, `allOf` the smallest **bounded** branch
      (unconstrained branches are skipped; refused only if none is bounded), and a keyword
      that does not bound is skipped when another one does. #31 re-serialises the validated
      output in that form, so the bound holds for what it stores. A property test
      (`hypothesis`, a new dev dependency) checks it on random bounded schemas. The bound
      must be at most `MAX_OUTPUT_BYTES` (256 KiB); the file nests at most 64 levels and is
      strict JSON (no duplicate keys, no `NaN`, no number overflowing to infinity). This adds
      **`jsonschema` as a runtime dependency** (it was a dev dependency; #31 needs it to
      validate outputs);
    - **(F) intents.** `intents` is a list of types or a mapping `type: max` (1 to 256, #35):
      at most `max` intents of that type per `llm` step; more is a `guard` violation that
      degrades the step (decision 46; it was `intent_rejected` before #35); a reused output
      over the limit is a cache miss. Omitted or empty: the skill
      proposes none. They must be within the plugin's approved `permissions.intents` (load
      error otherwise). The engine checks the LLM's intents against the plugin's permissions
      **narrowed to the skill's**, on fresh and reused outputs alike, and a
      `task.update_status` fingerprint must appear in the **viewed** input (what the model was
      shown), not merely in the data collected. **The reserved output property** (#28): the
      LLM proposes intents in a top-level `intents` property of its output, which the backends
      extract; a skill that declares intents must declare `properties.intents` as a bounded
      `"type": "array"` in its output schema, and a skill that declares none may not;
    - **(G) the engine** (`execute(..., skills=SkillSource)`, `Ports.skills`, built by
      `open_ports` as `DirectorySkills` over `<instance>/plugins`): the skill is loaded while
      planning, **before** the backend is checked, so an invalid skill fails the run before
      any plugin is called (`skill_invalid` on the `llm` step, even without a backend; a port
      bug is `host_error`), and `horae doctor` loads every skill an `llm` step uses (a
      `SkillError` is a problem). `LoadedSkill` exposes `name`, `version`, `digest`, `prompt`,
      `output_schema` (a fresh deep copy on each access: the skill keeps canonical bytes),
      `intents`, `intent_limits`, `output_bound`, `view_paths` and `view(data)`. `LlmRequest`
      gains `prompt`, `output_schema` (a fresh copy per attempt), `skill_version` and
      `skill_digest`, and its `input` is the **viewed** input, so a backend (#28) only sends
      it. A viewed input of `null` (the input does not have the view's shape) degrades the
      step **without calling the LLM** (`input_invalid`). `LlmCall.skill_version` is the
      loaded skill's version (not the backend's), `LlmCall.skill_digest` its digest, and
      `LlmCall.input_hash` the hash of the viewed input. Without a skill source (fakes, older
      callers) nothing changes; whether a real backend must refuse a request without a prompt
      is a question for the maintainer;
    - **(H) cache key.** With a skill, the key (`RunRecord.input_hash`) is the SHA-256 of
      `{skill, plugin: {name, version, commit}, model, input: <viewed input>, skill_version,
      skill_digest}`, where `skill_digest` is the SHA-256 of the canonical JSON of `{name,
      version, prompt, output_schema, input_view, intents}` (plus `view_options` and
      `intent_limits` when the manifest has any; not `description`). A new version, an edited
      prompt, schema, view or intents is a cache miss; a change to an undeclared input field
      is not (the model would see the same thing). Skills are loaded once per run
      (`DirectorySkills` caches them), so a run sees one version of the files;
    - **(I) replay.** `replay(..., skills=)` passes the source to the engine; `horae replay`
      reads skills from `<instance>/plugins` when it exists (local files only, like
      `LockRegistry`). A skill edited since the run replays as different, by design; when
      both records carry `llm[].skill_digest`, the report adds the note "skill <name> changed
      since the run" and an `llm.<skill>.skill_digest` summary field.

### Provisional decisions (#30, pending maintainer review)

40. **The `openai-compatible` and `ollama` backends (#30).** Two compatible v1 additions and
    the rules of `horae.llm.backends.openai_compatible` and `horae.llm.backends.ollama`, which
    keep every rule of decision 38 (render/send, the key only from the declared secret,
    rebuilt headers, the explicit network path, quiet loggers, strict JSON, the pre-flight
    budget, clean error chains, the summary recorded as `LlmCall.request`, reserved
    `intents`) and share its code (`horae.llm.backends._shared`):
    - **contract change** (optional): `models.<alias>.structured_output`, `json_schema`
      (the default when unset), `json_object` or `none`, accepted for `backend:
      openai-compatible` only (the model and the JSON Schema both refuse it on another
      backend). It is the fallback for a server that lacks JSON Schema structured output;
    - **contract change** (optional): `models.<alias>.max_tokens_field`, `max_tokens` (the
      default when unset) or `max_completion_tokens`, `openai-compatible` only (refused on
      another backend by the model and the JSON Schema), for OpenAI's reasoning models,
      which refuse `max_tokens`. There the limit also bounds the hidden reasoning tokens, so
      a low `tokens_out` can end in `length` with no visible output (an `output_invalid`
      truncation, charged); the payload keys follow the field, so a payload rendered for one
      field is refused by an alias set to the other;
    - **A. no SDK**: both talk to their API with a direct `httpx2` client (one JSON `POST`).
      The `openai` SDK would add a dependency and what the `anthropic` backend has to
      neutralise (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID` /
      `OPENAI_PROJECT_ID`, its own retries, platform headers and debug logging); the
      `ollama` client reads `OLLAMA_HOST`. Nothing here reads the environment. The posted
      bytes are the canonical JSON of the rendered payload, so they equal
      `RenderedRequest.body()` byte for byte;
    - **B. what `base_url` means**: for `openai-compatible` it is the API root **with** its
      version prefix, as the OpenAI SDKs take it (`https://openrouter.ai/api/v1`,
      `https://api.mistral.ai/v1`, `http://vllm:8000/v1`), and the only request is `POST
      <base_url>/chat/completions`; for `ollama` it is the server root (`http://ollama:11434`)
      and the only request is `POST <base_url>/api/chat`. A request to any other origin,
      path or query is refused (`host_error`). `http://` follows decision 24 (loopback,
      private and single-label hosts only);
    - **C. the key is optional**: `secret` is optional for both; when declared it is sent as
      `Authorization: Bearer <key>` (for `ollama`, to a proxy in front of it), else no
      `Authorization` header is sent. The headers are exactly `host`, `accept`,
      `content-type`, `content-length`, `accept-encoding: identity`, `user-agent: horae`
      and that one (`anthropic` sends `accept-encoding: identity` too);
    - **D. `openai-compatible` request**: `model`, `max_tokens`, `stream: false` and two
      messages (`system`: the preamble, then the skill's prompt; `user`: the tagged input and
      schema errors of decision 38). `json_schema` sends `response_format: {type:
      json_schema, json_schema: {name: horae_output, schema, strict: false}}`: `strict:
      false` because OpenAI's strict mode refuses schemas that do not mark every property
      required or that use keywords it lacks (`maxLength`, `pattern`), and the #31 validator
      checks the full schema anyway (decision 38 G). `json_object` sends `{type:
      json_object}` and `none` no `response_format`; both append the schema to the system
      prompt. With `json_schema` the schema is **not** also put in the prompt: it is in
      `response_format`, and repeating it would make the request larger and give the model
      two copies to reconcile; the trade-off is that a server which silently ignores
      `response_format` gets no schema text (the #31 validator still refuses its output;
      such a server should use `json_object` or `none`). `strict: false` is recorded here
      as the decision (see above). `max_tokens` by default (what the compatible servers
      take; `max_tokens_field` above for reasoning models). OpenRouter's `provider` routing
      fields (`order`, `allow_fallbacks`, `data_collection`...) are not sent and cannot be
      configured: a later per-alias option, since routing decides which company sees the
      data;
    - **E. `openai-compatible` response**: exactly one choice, `finish_reason: stop`, and
      `message.content` one JSON value. `length` / `model_length` (truncation), a non-empty
      `message.refusal` or `content_filter` (refusal), `tool_calls` / `function_call` and any
      other finish reason are `output_invalid` with the usage. Usage is `prompt_tokens` /
      `completion_tokens`; a reported `prompt_tokens_details.cached_tokens` (at most
      `prompt_tokens`) becomes `cache_read_tokens` and is taken out of `tokens_in`. An error
      status carries the API's error `type` when it is a short identifier;
    - **F. `ollama` request and response**: `model`, `stream: false`, `format` (the output
      schema: Ollama's structured outputs, Ollama 0.5 or later), `options.num_predict` (the
      `max_tokens`) and the same two messages. `done` must be `true`; `done_reason: length` is
      a truncation, tool calls and any reason but `stop` (or none, as older servers send) are
      `output_invalid`. Usage is `prompt_eval_count` / `eval_count`; Ollama may omit
      `prompt_eval_count` for a cached prompt, and a missing count is charged conservatively
      (decision 38). Ollama's `error` text is never shown: the message has the status only
      (a missing model is HTTP 404, `invalid_request`);
    - **G. cost and `horae doctor`**: without a `price` a call costs `usd: 0` (`priced:
      false`), as for every backend. `horae doctor` treats `ollama` as local (no price
      warning) and `openai-compatible` as cloud (the warning of decision 38 A, which never
      fails the check), even when its `base_url` is a local vLLM: the backend name is the
      only signal kept simple here. The #34 design note proposes an explicit `local:` flag
      per alias; when it lands it replaces this rule;
    - **H. shared conformance suite**: `tests/test_llm_backends_shared.py` runs the same
      tests over `anthropic`, `openai-compatible` and `ollama` with recorded synthetic
      responses (`tests/fixtures/llm/<backend>/`: success, truncation, refusal where the API
      has one, tool call, 401, 429, 5xx, malformed JSON, missing usage); a new backend joins
      it by adding a `Kind` in `tests/llm_backend_kinds.py`;
    - **I. the response is bounded** (every backend, `anthropic` included, through
      `_shared.BoundedTransport` around the client's transport): `accept-encoding: identity`
      is sent; the cap of a call is `response_cap(max_tokens) = min(8 MiB, max(1 MiB, 64 ×
      max_tokens))` bytes of decoded body (64 bytes a token: a token is a few bytes of text,
      which JSON may escape up to 6 bytes a character, inside the provider's envelope). A
      success whose `content-length` exceeds it is refused before its body is read; one that
      grows past it while read (chunked) is refused at the cap. A server that compresses
      anyway is inflated by Horae (`gzip`, `deflate`), at most one byte past the cap per step,
      so a gzip bomb (305 KB inflating to 300 MiB) is refused with about one cap in memory;
      any other `content-encoding` (or a list of them) and corrupt compressed data are
      refused. All of these are `output_invalid` with the conservative usage (decision 38:
      the input bound and the whole `max_tokens`), since the provider may have billed the
      call. An error body is cut at 64 KiB (its status still maps the error);
    - **J. the body is read strictly**: decoded as UTF-8 only (`bytes.decode("utf-8")`, no
      charset sniffing: UTF-16, a stray `0xff` or an encoded surrogate are refused), then
      parsed by the strict JSON parser of decision 38; it must be one JSON object. `anthropic`
      now reads the raw response itself the same way (the SDK posts the canonical bytes with
      `cast_to=httpx2.Response` and still maps HTTP and connection errors; no SDK model is
      built from the response). Any exception other than an `LlmError` while reading the
      result is `output_invalid` with the conservative usage, never a crash. An error body
      that is not UTF-8 gives the status only;
    - **K. reported usage is untrusted**: a token count must be an integer from 0 to `10**12`
      (`10**400` or `10**308`, which would overflow the cost to infinity, are unusable: the
      call is charged conservatively). A usable report is then floored: `tokens_in` (with the
      cache tokens) at least `ceil(request bytes / 4)` and `tokens_out` at least `ceil(UTF-8
      bytes of the output text / 4)`, the latter capped at `max_tokens` (what a provider
      honouring the request may bill). The larger value is the one recorded, priced and
      charged to the budget; the reported value is not kept separately (kept simple: one
      `Usage`). A server that reports `0 / 0` is still charged for what it plainly sent;
    - **L. `http://` is checked at connect time** (decision 24 limits the *host name*; this
      limits where it *resolves*): for an `http://` `base_url` the transport's network backend
      (`_shared.LanOnlyNetwork`) resolves the host when it opens a connection and refuses
      unless **every** address is loopback (`127/8`, `::1`), RFC 1918 (`10/8`, `172.16/12`,
      `192.168/16`) or IPv6 ULA (`fc00::/7`), an IPv4-mapped address being checked as IPv4;
      link-local (`169.254/16`, `fe80::/10`, where cloud metadata services answer) and CGNAT
      (`100.64/10`) are refused. It then connects to the checked address itself, so a second
      lookup cannot rebind the name. A refusal is `host_error` ("request refused: an http
      endpoint that resolves to a public address"), like every other refused request, and
      nothing is sent; a failed lookup is a connection error (`host_error`, or `timeout`
      within the step's timeout). `https://` endpoints are not restricted (TLS
      authenticates the host);
    - **M. `horae doctor` warns about a key over `http://`**: an alias used by a pipeline
      with a `secret` and an `http://` `base_url` gets a warning (never a failure): the key
      crosses the local network in clear, readable by anyone on the path;
    - **N. the `base_url` path cannot traverse**: besides decision 24, a `base_url` whose
      path has a `.` or `..` segment or an encoded `/`, `.` or `\` (`%2F`, `%2E`, `%5C`, any
      case) is refused, since a server or proxy may decode it into a path the guard never
      compared (`https://h/%2e%2e/v1`, `https://h/a%2Fb`, `https://h/v1/../x`).

### Provisional decisions (#33, pending maintainer review)

41. **Budgets per day and the usage ledger (#33).** (39 and 40 are taken by #27 and #30.)
    A compatible v1 addition and the rules of `horae.runner._budget`, the store and `horae
    usage`:
    - **contract change** (optional): `horae.yaml` `budgets: {daily?, pipelines?}`, where
      `daily` is `{usd?, tokens_out?}` (at least one, and not `null`: each `anyOf` branch of
      the JSON Schema requires its property as a number; `usd > 0`, `tokens_out >= 1`) for every
      pipeline together, and `pipelines.<name>.daily` the same for one pipeline (a name that
      is not a pipeline of the instance is a loader problem). Both apply; the lower
      allowance wins. The day is the instance's `timezone` day (not a trigger's). `usd`
      bounds only priced aliases (an alias without `price` records `usd: 0`, decision 38 A);
      `tokens_out` bounds every call. The step's own `budget` is unchanged and still bounds
      both attempts together;
    - **reservation**: before every LLM attempt, the runner first prepares the request
      outside any lock (`ModelBackends.prepare`: the alias, the skill's prompt from the
      `PromptSource`, which may read files, the provider backend and the input bound); then,
      in one store transaction (`BEGIN IMMEDIATE`, so the processes of other runs wait for
      it), it reads the day's spend from the ledger (settled calls at their cost, unsettled
      ones at their reservation), caps the attempt's budget with what is left of each
      ceiling (see A for a refusal), renders the request with that budget
      (`ModelBackends.render(capped, prepared)`: pure sizing and payload, no I/O; its
      pre-flight of decision 38 B may refuse it too) and reserves the most it can cost
      (`ModelBackends.worst_case`: the input bound at the input price, or at the cache write
      price for a provider backend that `may_write_cache`, plus `max_tokens` at the output
      price: the bound `max_tokens` already keeps within the budget). Another `LlmBackend` (a
      test fake) reserves the capped budget itself. Two runs of different pipelines
      therefore never both spend the last of a ceiling (`tests/test_budgets.py` has a
      two-process test whose `day_spend` waits between the read and the reservation: it
      fails with a deferred transaction). Not the apply lock: an LLM call can take minutes
      and must not block other runs' applies;
    - **settling, right after the call** (not with the step record: failed attempts cost
      money too and have no `LlmCall`): the reported usage (also of a failed call,
      `LlmError.usage`; zero for a backend's cached response; a cost above the reservation
      is logged, amounts only, and the row marked `over_reservation`); a refusal that reports no
      usage and means the provider processed nothing (`budget_exceeded`,
      `backend_unavailable`, `skill_unavailable`, `invalid_request`, `auth_failed`,
      `rate_limited`, `overloaded`) releases the reservation; any other failure without
      usage (a timeout, a lost connection, a cancellation) keeps the reservation as an
      `estimated` cost. **Crash consistency**: a process that dies between reserving and
      settling leaves its `reserved` row, which keeps counting for its day: a crash can
      over-count, never under-count (until the run is abandoned or recovered, see D). A store failure while reserving refuses the call
      (`host_error`); while settling it is logged (type only) and the reservation stays;
    - **the ledger** is the `llm_usage` table (migration 4): per attempt `run_id`,
      `pipeline`, `step`, `attempt`, `alias`, `backend`, `model`, `state` (`reserved` or
      `settled`), tokens in and out, cache read and write tokens, `usd`, `priced`,
      `estimated`, `over_reservation`, `at` (UTC) and `day` (local). The ledger outlives its
      runs (C);
    - **`horae usage [--pipeline P] [--since DATE] [--until DATE] [--by day|pipeline|model]
      [--json]`** groups the ledger by day and pipeline (or by day, pipeline, or backend and
      model), oldest first, with a `TOTAL` row; dates are local days, inclusive. Cells of
      calls of an unpriced alias are flagged `*`, calls in flight `+`, calls settled at
      their reservation `~` and calls that cost more than reserved `!`, with a footnote
      each; a cost under a ten-thousandth of a dollar shows as `<$0.0001`. `--by model`
      groups by backend, model and alias: a call that reached no provider has a `null`
      backend and model (`-` in the table). The store is opened read-only (decision 31 E: a missing store
      lists nothing, an older schema exits 1). Exit codes as decision 37: `0` listed, `1`
      configuration or store problem, `2` usage error (a malformed date, `--since` after
      `--until`, an unknown `--by`), `70` internal. `--json` prints `{schema_version: 1,
      instance, by, pipeline, since, until, rows: [{<by keys>, calls, tokens_in, tokens_out,
      cache_read_tokens, cache_write_tokens, usd, unpriced_calls, in_flight_calls,
      estimated_calls, over_reservation_calls}], total}`;
    - **(A) a minimum useful output**: a call is refused (`budget_exceeded`, no request,
      nothing reserved) when what a daily ceiling leaves cannot buy
      `MIN_OUTPUT_TOKENS = 256` output tokens, or fewer when the step's own budget buys
      fewer (`min(256, what the step alone allows)`): an answer cut short is worse than
      none. The refusal names the daily ceiling (`the instance daily budget leaves ...`),
      also when the pre-flight of decision 38 B refuses only because of it; a step whose own
      budget is too small is refused in the step's own words, as before;
    - **(B) `horae doctor`** warns when a `usd` ceiling (`budgets.daily` or
      `budgets.pipelines.<name>`) covers a pipeline whose `llm` alias has no `price`: its
      dollars are then unbounded, only `tokens_out` bounds it;
    - **(C) the ledger lives independently of runs**: `run_id` is nullable, `ON DELETE SET
      NULL`, so `prune_runs` never changes a day's totals; the ledger's own retention is
      `Store.prune_usage(older_than)` (settled rows of earlier local days; reserved rows are
      kept);
    - **(D) abandon and recovery**: `abandon_run` (the stale-run recovery calls it too, a
      compare-and-set on the run's status) turns the run's `reserved` rows into `settled`
      ones at their reservation, `estimated = 1`; `horae usage` tells calls in flight (`+`,
      `in_flight_calls`) from settled estimates (`~`, `estimated_calls`);
    - **(E) dry runs are charged**: they call the model. PLAN.md's "a dry run writes
      nothing" is about user data (decision 32 D5): Horae's own state, the run record and
      the usage ledger, is written;
    - **(F) the day is stored at reservation time**: a later `timezone` change does not
      rewrite history (past rows keep their day); local midnight and DST transitions follow
      the instance's time zone (a 23- or 25-hour day is one day);
    - **provider prompt caches**: a `ProviderBackend` declares `may_write_cache` (default
      `False`; `anthropic` is `False`, since Horae sends no `cache_control`; `claude-cli`
      #29 is expected to be `True`). For such a backend the input bound is priced at the
      cache write price (`cache_write_per_mtok`, else twice the input price) in both
      `max_tokens` and `worst_case`, so a settled call never exceeds its reservation; a
      backend without it that reports cache writes beyond the bound is flagged
      `over_reservation` (and logged);
    - **untrusted usage (decision 40 K)**: a backend's floored usage (input at least a
      quarter of the request bytes, output at least a quarter of the output bytes but at
      most `max_tokens`) and its conservative charge (`input_bound` of the payload sent plus
      the whole `max_tokens`) stay within the reservation: `ModelBackends.prepare` sizes the
      input bound on the payload at the largest `max_tokens` the request allows (its
      `tokens_out`, capped by the alias' `max_output_tokens`), so the payload actually sent,
      with a `max_tokens` no larger, is never bigger. A count a provider reports above the
      bound but under `10**12` is kept (decision 40) and flagged `over_reservation`;
    - **follow-ups**: a monthly ceiling; provider-side spend limits (#63); calling
      `prune_usage` from a retention setting.


### Provisional decisions (#53, pending maintainer review)

43. **Secret redaction in logs and run records (#53, design note and maintainer answers on
    #100; security review findings and maintainer decisions on #106).** (39 and 42 are taken
    by #27 and #29.) Contract change: none; a store migration (`0005_secret_flags`). The
    store's findings reuse the `secret_in_output` violation of decision 35 (so a replay leaves
    them out of the compared violations, as it does for the host's). **Maintainer
    decisions** (binding, on #106): the journalled `after` / `actual_after` bytes are exempt
    and flagged only for a secret new compared to `before` (C), and secrets under 8 bytes are
    refused (H). **Provisional**, pending maintainer review: B (identifiers exempt and
    flagged), E (every logger, warnings, exception hooks), F (`secure_delete`), G (key
    collisions). Rules of `horae.secrets`, the store, the engine, the LLM layer and the CLI:
    - **A. one shared redactor and a process registry.** `Redactor`, `token`, `SecretInKey`
      and the encodings of decision 35 C moved to `horae.secrets.redact` (re-exported by
      `horae.plugins.host.redact`); the log scrubber to `horae.secrets.logs` (re-exported by
      `horae.plugins.host.logs`). Every `SecretResolver.resolve` adds the secret to a
      process registry (`remember`; `current()` is a `Redactor` of every secret this process
      resolved, rebuilt when the set grows; it only grows, so a rotated value stays
      redacted). `Instance.prime_redaction()` resolves, for redaction only, every secret the
      instance names (`secrets.refs`, the secrets `horae.lock` grants, the model aliases'
      `secret`), skipping one that cannot be resolved; `Runner.run` and the CLI commands that
      read the instance call it first; those that read `horae.yaml` alone (`runs`, `diff`,
      `usage`, `undo`) prime the same names (`horae.config.loader.redaction_names(config,
      lock)`, shared with the instance), reading `horae.lock` best effort (an absent or
      invalid lock adds nothing). A provider backend still resolves its own key lazily,
      through its own resolver. A JSON number whose `json.dumps` is a secret's value is a
      **numeric match** (`pin: 12345678`): the checks that refuse see it (`Redactor.find`),
      the host and the CLI redact it, the store flags it without changing its type;
    - **B. what is redacted, and identifiers (provisional).** Only **free text** is
      rewritten. **Identifiers the system needs are exempt and flagged** (kept as written,
      neither refused nor rewritten; a hit is a `secret_in_output` violation): run ids,
      pipeline names, timestamps, statuses, kinds, plugin names and refs, tokens, digests,
      the journal's `target`, `applied_by` and `sink_identity`, a task's `fingerprint`,
      `source` and `target`, notification keys, the usage ledger's `alias`, `backend` and
      `model`, a run's `host`, the pipeline definition. Rewriting them made the record
      invalid (a run refused for a secret that happened to equal a word of its own
      structure), broke undo (a redacted journal target no longer names the note) and
      replay. Only the LLM cache refuses. The free-text allowlist (`horae.store._gate`): step
      `output`, `error.message`, `stderr`, call `params` and `intents`; input `params`,
      `data` and `intents`; the intent payload fields `content`, `heading`, `title`, `body`;
      `context.vars` values; LLM `request`, `response`, `intents`, `repair`,
      `schema_errors`; violation details; the run's `error`; a task's `title`; an abandoned
      run's `error`. A pipeline definition holding a secret's value is not copied into the
      record (`pipeline_definition` is `None`; its parameters refuse the run at plan time);
    - **C. the store's secret gate (answers 1–3), fail safe.** Every table, just before
      writing, with the registry's redactor: free text redacted (JSON: string leaves and
      object keys), identifiers and numeric matches flagged (B). **Refused**
      (`SecretRefused`, an `InvalidRow`, nothing written, name only): an LLM cache entry
      holding a secret in any string, key or number (answer 3; callers treat it as "do not
      cache", `Store.cache_put_safe`, for #32), and, as a residual, a record or step that
      would no longer validate once its free text is redacted (a token longer than the room
      its field has left). **The journalled `after` bytes and a sink's `actual_after`
      bytes: exempt, and flagged only when the secret is new** (maintainer decision on
      #106): kept byte for byte (apply's mismatch check and undo depend on them); a hit is
      recorded only when the secret occurs more often than in the journalled `before` ("the
      store found secret X in the content Horae wrote to <target>, not in the note before"),
      so a secret the user already had in their note is not flagged. **Exempt** (answer 2):
      the journalled `before` bytes and the bytes a forced undo `overwrote`, the user's own
      note, which undo restores byte for byte (docs/security.md T09). Every finding is logged
      (`horae.store`, names only) and kept in the `secret_flags` table, in the same
      transaction as the row that held it; `save_run` turns a run's findings (and those in
      the record itself) into `secret_in_output` violations, then deletes them, so a record
      saved by another `Store` or process still gets them; `abandon_run` deletes them (no
      record); when the violations would pass `MAX_ITEMS`, the rest is aggregated into one
      violation;
    - **D. trusted inputs are refused (answer 4).** The engine fails the run at plan time,
      before any call, `params_invalid` naming the secret only, when a template variable
      (`horae run --var`) or a rendered parameter holds the value (or a known encoding) of a
      resolved secret, in a string, an object key or as a number's whole value; the variable
      is redacted in the record's `context`. A skill's prompt or output schema holding one
      (in a string, an object key or as a number's whole value) is refused at plan time too:
      `_plan` loads the skill through the engine's `SkillSource` (#27, decision 39) and fails
      the run with `params_invalid` (`<skill>: the prompt or output schema holds secret
      <NAME>`) before any step runs. `ModelBackends.prepare` keeps the same check as a
      backstop (an engine given no `SkillSource`, a `PromptSource` that reads other files),
      before rendering, reserving or sending.
      `params_invalid` releases a ledger reservation;
    - **E. logs, warnings, uncaught exceptions (provisional).** `horae.secrets.logs.install()`
      (the CLI entry point, `Runner.run`, an opening `PluginHost`; idempotent) wraps the log
      record factory: **every** record of every logger (the root, `asyncio`, `urllib3`,
      `py.warnings`, `uvicorn`, `openai`, `mcp`... not a list of prefixes any more) is
      redacted when it is created (message formatted and redacted with the registry and every
      open host, `args` dropped, `exc_info` and `stack_info` replaced by the exception's type
      name), whatever handler sees it. It also calls `logging.captureWarnings(True)` and sets
      `sys.excepthook` and `threading.excepthook` to hooks that redact the report they print.
      The HTTP loggers are pinned to `WARNING`. The LLM layer: `ModelBackends.send` redacts
      what a provider backend returns (an `LlmError`'s message and stderr, raised again as a
      new error with no `__cause__` / `__context__`; the output, intents and schema errors;
      logged by name); the engine redacts an `LlmError` message from any `LlmBackend` before
      recording it. The pipeline digest and the cache key are computed on the real values
      (they are hashes);
    - **F. deleted bytes (provisional).** Every writable store connection (the main one and
      the heartbeat's) sets `PRAGMA secure_delete = FAST`, so a deleted or replaced row is
      zeroed where SQLite rewrites its page anyway. A database written before #53 can hold
      unredacted values in rows and free pages: docs/operations/retention-and-backups.md
      documents a one-off `VACUUM`;
    - **G. key collisions (provisional).** When redacting two keys of one JSON object gives
      the same text, the later one is renamed `[REDACTED:NAME]#2` (`#3`...) and a violation
      says so: nothing is lost silently;
    - **H. short secrets (maintainer decision on #106; supersedes decision 35 D).** A
      declared secret shorter than 8 bytes of UTF-8 (`MIN_SECRET_BYTES`) is **refused when it
      is resolved**: `SecretError` "... is too short for reliable redaction (min 8 bytes)",
      never echoing the value. `horae doctor` reports it as an error; a plugin that declares
      it fails with `secret_missing`, a model alias with its backend's secret error (the run
      needing it is refused); priming skips it with a warning naming it (`horae.secrets`).
      The redactor has no short-secret case any more (`Redactor` raises `ValueError` if it is
      handed one), and the host no longer warns about one;
    - **I. CLI output.** Every command prints through `horae.cli_output.echo`, which redacts
      the text; `to_json` scrubs the document as data first (numbers too, so the JSON stays
      valid). `horae.apply.render.safe` redacts **before** it escapes control and invisible
      characters, and text is redacted before it is cut (an HTTP body at 200 characters, a
      run's error at 4096), so neither a secret holding such a character nor a secret cut in
      half slips through. An identifier holding a secret is printed redacted (B keeps it in
      the store, not on the terminal);
    - **limits**: unknown encodings are not recognised and only the leftmost match of two
      overlapping secrets is replaced (decision 35); the gate knows only the secrets this
      process resolved (primed from the instance); an 8-byte secret still matches inside
      unrelated text that contains it (the reason identifiers are flagged, not rewritten);
      a secret in an identifier is in the store (flagged), but not in the logs nor the CLI.

### Provisional decisions (#32, pending maintainer review)

45. **The LLM cache and the $0 path (#32).** Numbering: 44 is #31's (PR #108), opened in
    parallel. Neither is renumbered: whichever of the two merges second places the #31 section
    (44) before this one, so the list stays in order. A compatible v1 addition to `horae.yaml` and
    the rules of `horae.runner._cache`, the engine, `horae undo` and the new `horae cache
    purge`:
    - **contract change** (optional): `horae.yaml` `cache: {ttl_s?, max_entries?}`, integers;
      `ttl_s` 60 to 31622400 (366 days), default 604800 (7 days); `max_entries` 1 to 100000,
      default 1000. Not part of `config_digest` (decision 33 D3): it changes what is reused,
      never what a replay computes;
    - **one source.** The store's `llm_cache` table is the single source of reusable outputs
      for live runs; the runner no longer reads `Store.last_accepted_llm` (D2, decision 30),
      which stays for tools and tests. Migration 6 adds the index `llm_cache_created` (on
      `created_at`, which trimming orders by). A store upgraded from before #32 starts with an
      empty cache: its first run per input calls the model once;
    - **two keys (G).** The **engine key** (`RunRecord.input_hash`, unchanged: SHA-256 of the
      canonical JSON of the skill, the skill plugin's name, version and commit, the resolved
      model, the viewed input, and with a loaded skill its `skill_version` and
      `skill_digest`, decision 39) is what the run records and what replay and undo use. The
      **stored key** (`StoreLlmCache.stored_key`) is SHA-256 of `{input_hash, alias,
      version}`: `alias` is the step's model alias as `horae.yaml` declares it at the time,
      without `secret` and `price` and without unset fields (backend, model, `base_url`,
      `structured_output`, `max_output_tokens`, `max_tokens_field`, and a later field such as
      the `claude-cli` command), `version` is `LLM_KEY_VERSION`
      (`horae.llm.backends._shared`, bumped whenever `SYSTEM_PREAMBLE` or the request framing
      changes; a test pins the preamble's digest to it). So the same model name on another
      backend or endpoint, or with another output mode, is a miss; a new price is not. The
      entry's `response` is `{output, intents, backend, model, skill_version, input_hash,
      run_id, created_at}` (`input_hash` the engine key, `run_id` the producing run); its
      `tokens_*` and `cost_usd` are what producing it cost (both attempts), for information;

      | | engine key (`input_hash`) | stored key (`llm_cache.key`) |
      |---|---|---|
      | skill name, version, digest | yes | via `input_hash` |
      | skill plugin name, version, commit | yes | via `input_hash` |
      | resolved model name | yes | via `input_hash`, and in `alias` |
      | viewed input | yes | via `input_hash` |
      | alias backend, `base_url`, `structured_output`, `max_output_tokens`, `max_tokens_field`, command | no | yes |
      | alias `secret`, `price` | no | no |
      | `LLM_KEY_VERSION` (preamble, framing) | no | yes |
      | pipeline, act/notify bindings | no | no (re-checked on reuse, C) |
    - **the engine reads, the runner writes.** `execute(cache=LlmCache)`: the engine awaits
      `cache.lookup(key, model_alias=..., output_schema=...)` when `previous` does not match
      the key; a hit is reused exactly as D2 (no backend call, `LlmCall` with `cached=True`,
      zero usage and the request marker `{skill, model, reused_input_hash, source_run}`,
      `source_run` the producing run; intents validated and routed again; a reuse whose
      intents no longer route is a miss; the same step and run statuses: `noop` when nothing
      is proposed). A hit names the alias' **current** backend and model (equal to the stored
      ones, since they are in the stored key, unless the alias is no longer declared). No
      backend call means no ledger row: a cached output is charged zero (decision 41). An
      exception from `lookup`, or an entry for another key, is a miss. A store error while
      reading, writing or purging is logged (type only) and never fails the run;

      | rule | when |
      |---|---|
      | **write** (`remember`, after the record is saved) | the run is `ok` or `noop` (H: not `degraded`, `failed` or `undone`), not a dry run (D); the `llm` step `ok`; its last call schema-valid with recorded intents; no call of the step a reuse (a hit never extends an entry); no resolved secret in the entry (`cache_put_safe`, decision 43); two processes writing one key: the last write wins |
      | **read** (`lookup`) | not `--no-cache`; the entry exists, `expires_at` not passed and `created_at` within the current `ttl_s` (A); well-formed (else deleted); the full output (the output with its reserved `intents` put back) passes the skill's full output schema (F, else deleted); its intents still validate and route in this pipeline (C) |
      | **delete** | expiry and `max_entries` at the start of each non-dry run (first in, first out); invalid on read (F); `horae undo` of a run (B); `horae cache purge` (E) |
    - **(A) bounded staleness.** An entry is live for `ttl_s` from when it was produced (its
      `expires_at`, and also `created_at` against the current `ttl_s` at lookup: lowering
      `ttl_s` applies at once to every entry; raising it applies to entries written from then
      on). A hit does not extend it: an unchanged input asks the model again at most once per
      `ttl_s` (PLAN.md §2.2's "no LLM call when inputs unchanged" holds within that window).
      After expiry a non-deterministic model may answer differently, so an unchanged input may
      produce new intents and notifications **once per `ttl_s`** (appliers still deduplicate
      what is identical). At the start of every non-dry run, entries past `ttl_s` or their
      `expires_at` are deleted, then the oldest beyond `max_entries` (`Store.cache_trim`,
      first in, first out: a hit does not make an entry recent), best effort;
    - **(B) undo invalidates.** When `horae undo` marks a run `undone`, every entry for that
      run's engine key is deleted in the same store transaction (`Store.cache_delete_input`,
      which matches the `input_hash` the entries record, whatever alias they were stored
      under), also when the undone run was itself a cache hit: running the same input again
      asks the model instead of re-applying what was just undone. A partial undo (not every
      change undone) leaves the entry;
    - **(C) cross-pipeline and cross-history reuse stays.** Any earlier accepted output is
      reused, not only the last run's (input A, B, then A again makes two calls), across
      processes, and across pipelines that run the same skill, model alias and viewed input.
      This is safe because routing and bindings are re-checked on every reuse: an intent
      whose explicit target is bound differently in the other pipeline does not route, which
      is a miss (the model is called);
    - **(D) dry runs read, never write.** A dry run reuses a live entry (a preview costs
      nothing on unchanged input) but neither stores its output nor purges: a preview must
      not change what later real runs do;
    - **(E) `horae run --no-cache`** (debugging) skips the lookup: the model is called even on
      unchanged input, and its accepted output still replaces the entry (a fresh answer is a
      valid answer). `Runner.run(cache=False)` is the same. **`horae cache purge [--all |
      --older-than DAYS] [--skill NAME] [--json]`** deletes entries on demand (`--skill`
      alone: all of that skill's): it opens the store writable but never creates it, takes no
      lock, prints how many entries were deleted and how many remain (`{schema_version,
      instance, all, older_than_days, skill, deleted, remaining}`); exit codes per decision
      37: 0 purged (possibly nothing), 1 configuration or store problem, 2 usage error
      (nothing selected, `--all` with `--older-than`), 70 internal;
    - **(F) re-validated on read.** A cached output is checked against the skill's full output
      schema at every lookup, by the validator of the backend path: #31's
      `horae.llm.validate.validate_output` once it is installed, until then
      `horae.runner._cache.check_output` (the same size bound, `MAX_OUTPUT_BYTES`, and
      `jsonschema`'s draft 2020-12 validator; `default_validator` switches by itself, so #31
      needs no change here). An entry that fails, or that cannot be validated (fail closed),
      is a miss and is deleted. So an entry written before a validator existed, or tampered
      with in the store, is never reused unchecked;
    - **replay stays exact.** Replay never reads the store's cache: a run that reused an entry
      is replayed with that output as `previous` (`recorded_previous`, decision 33, which
      keeps `source_run`), so the replay takes the same path; a run that called the model is
      replayed from its tape;
    - **follow-ups**: a per-skill `ttl_s`; LRU instead of first in, first out if hit rates
      show it matters.

### Decisions (#35, design note and maintainer answers on #99)

46. **The action guard on LLM proposals (#35).** Numbering: 44 is #31's (PR #108), 45 #32's.
    No contract change: the run record's `violations[].kind` already has `guard`, and the
    step's `error.kind` is the free-form `ErrorKind` `guard_violation`. `horae.engine.guard`
    runs on every batch of intents the `llm` step proposes (fresh, `previous` or a cache hit),
    after schema validation and the permission check, **before routing**:
    - **(1) per-type limits.** A skill's `intents: {type: max}` (decision 39 F) caps the intents
      of each type per step; more is a guard violation (it was `intent_rejected`). A type the
      skill does not declare stays `intent_rejected` (a permission breach, the run fails);
    - **(2) embeds are always refused**, even when the URL is in the input (they fetch when the
      note renders, without a click): any `![` (Markdown and reference images, Obsidian
      `![[…]]` embeds), and the HTML tags `img image iframe frame frameset object embed video
      audio source track picture link script style svg math meta base form input portal applet
      bgsound param` whatever their attributes;
    - **(3) refused schemes.** `javascript:`, `vbscript:`, `data:` and `file:` at a word start
      and followed by a non-space character, anywhere in the text; inside any HTML tag also
      with whitespace or control characters in the scheme; a protocol-relative `//host` as a
      Markdown link destination, a link reference definition or an HTML attribute value;
    - **(4) the input set** (answer 3). Every URL in the text (`scheme://…`, `mailto:…` and
      `www.…` runs, up to whitespace, `<`, `>` or a control character) must be an **exact
      string** extracted the same way from the **viewed** input (string values and object keys
      of what the model was shown), case-sensitive, after dropping at most 16 trailing
      punctuation characters (`.,;:!?)]}'"*_~` and backtick) on either side. So a known URL may
      be copied, but never extended (`…?q=<data>`, `…/<data>`). A `notify.push` `link` must be
      exactly such a URL **and** fall under the entry's `link_prefix` (decision 30). A
      per-pipeline `strict_links` (only trusted parameters and `link_prefix`) is a follow-up;
    - **fields scanned**: `note.*` `content` and `heading`, `task.create` `title` and
      `fingerprint`, `notify.push` `title`, `body` and `link`, every string and object key of an
      `http.post` `body`. Targets bound by routing (`path`, `http.post` `url`) are not text;
    - **robust parsing, bounded work.** Every check runs on the text as written and on its
      HTML-entity-decoded forms (up to three passes: `&#x3c;img`, `&amp;lt;img`), ASCII
      case-insensitively. Scans are linear (`find` loops that resume after each match, regular
      expressions without nested quantifiers); a test scans 1 MiB of hostile text per field;
    - **(answer 4) the whole step degrades.** A violation records one `Violation(kind="guard")`
      and the step `degraded` with `error.kind` `guard_violation`: none of its intents is
      routed (also the valid ones), its output is not used, later steps are `skipped`, the
      run is `degraded`, and no `input_hash` is recorded (so the runner never caches it). The
      LLM call is recorded with its raw intents (replay and audit);
    - **(answer 6) content-free detail.** `Violation.detail` (= the step error's message, at
      most 512 characters) names only closed-set values: the proposer (`skill:<name>`), the
      intent type and position, the field and the kind of problem, e.g. ``skill:daily-brief/
      rank proposed note.create #2 with an image or embed in `content` ``, never the URL, its
      host or the text. The same line is logged at `warning` (`horae.engine`);
    - **reuse (decision 45).** On a reuse, a guard violation is a **cache miss**: nothing of
      the entry is recorded (no violation) and the model is called. For a cache hit the engine
      awaits the cache's optional `discard(key, model_alias=...)`; `StoreLlmCache.discard`
      deletes the entry (logged, store errors swallowed), so a poisoned entry is not looked up
      again. A refused `previous` (replay) is only a miss;
    - **(answer 5) scope.** LLM proposals only; `collect` and `transform` intents are not
      guarded in v1 (first-party plugins in the D tier, T04); hostile plugins are #52's;
    - **fencing** (design note C). The backends already fence the input: it is sent as JSON
      inside `<input>` tags with every `<` written `<` (`horae.llm.backends._shared.
      escaped`), and repair errors likewise inside `<schema_errors>`, so data can never close
      the fence (`tests/test_llm_backends_shared.py`). A per-call nonce adds nothing to that
      and would make requests non-deterministic, so none is added and `LLM_KEY_VERSION` is
      unchanged;
    - **known limits**: obfuscated URLs (`hxxps`, zero-width characters inside the scheme)
      are not links in Obsidian and pass; a URL the input brings may be copied (tenet 5 as
      written); a brief citing a new URL from the model's own knowledge is refused.
