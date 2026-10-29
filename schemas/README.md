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
| Eval case | [`eval-case/v1.json`](eval-case/v1.json) | `eval-case/v1.json` | `skills/<namespace>/<skill>/evals/<case>.yaml` (§5) |
| Eval recorded response | [`eval-recorded/v1.json`](eval-recorded/v1.json) | `eval-recorded/v1.json` | `evals/recorded/<backend>/<case>.json` (§5) |

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
      strict JSON (no duplicate keys, no `NaN`, no number overflowing to infinity).
      **Amended by #31 (decision 44 G):** the product of `anyOf` / `oneOf` branch counts
      along any path of the schema, `$ref`s followed (memoised per `$ref`), is at most
      `MAX_COMBINATOR_FANOUT` = 1024 (a validator tries every branch, so nested alternatives,
      through a chain of `$defs` say, make one validation exponential); alternatives side by
      side (`properties`, `allOf`) do not multiply. This adds
      **`jsonschema` as a runtime dependency** (it was a dev dependency; #31 needs it to
      validate outputs). **Amended by #36 (decision 48 D4):** an `enum` or a `const` needs a
      `type` beside it (`SchemaProblem` naming the JSON pointer: Anthropic's structured
      output, `transform_schema`, refuses one without), and `horae doctor` reports a loaded
      skill's schema that a backend's structured output still refuses (`schema_problems`,
      with the pointer of the deepest refused subschema): an error when an alias of that
      backend runs the skill, else a warning;
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
      price: the bound `max_tokens` already keeps within the budget). **Amended by #113
      (decision 42's amendment (#113, live run)):** the output part is `output_attempts ×
      max_tokens` at the output price (a backend that may bill several outputs for one
      call, `claude-cli`: 4; 1 for the others), and `max_tokens` divides the step's
      remaining `tokens_out` and `usd` by `output_attempts` so it still fits. Another
      `LlmBackend` (a test fake) reserves the capped budget itself. Two runs of different pipelines
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
      is `True`, decision 42). For such a backend the input bound is priced at the
      cache write price (`cache_write_per_mtok`, else twice the input price) in both
      `max_tokens` and `worst_case`, so a settled call never exceeds its reservation; a
      backend without it that reports cache writes beyond the bound is flagged
      `over_reservation` (and logged). Likewise a `ProviderBackend` declares
      `output_attempts` (default 1; `claude-cli` is 4, #113): `worst_case` reserves that
      many outputs of `max_tokens` and `max_tokens` divides the step's budget by it; an
      unusable declared value (not an `int` of at least 1, or a `bool`) is
      `backend_unavailable`, a larger one is clamped to `MAX_OUTPUT_ATTEMPTS` (16);
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


### Provisional decisions (#29, pending maintainer review)

42. **The `claude-cli` backend with isolation (#29, the design note on
    [#97](https://github.com/horae-dev/horae/pull/97); every recommendation accepted).**
    One compatible v1 addition and the rules of `horae.llm.backends.claude_cli`. **Not a legal
    assessment**: personal headless use of `claude -p` on your own plan is PLAN.md's ⚠️
    unverified reading (risk #70); this backend is "personal use under your plan's terms",
    and `anthropic` with an API key stays the recommended backend for published setups
    ([ADR 0005](../docs/adr/0005-claude-cli-backend-and-its-terms.md)).
    - **contract change** (optional): `models.<alias>.command`, an absolute path (no control
      characters, at most 1024 characters), accepted for `backend: claude-cli` only (the model
      and the JSON Schema both refuse it on another backend). Unset, `claude` is looked up on
      the fixed child `PATH` (`/usr/local/bin:/usr/bin:/bin`), **never** Horae's own `PATH`
      (answer 6). The native installer puts `claude` in `~/.local/bin`, which is not on it:
      such setups set `command`. `command` must name the **native binary**: the npm package's
      `claude` is a `#!/usr/bin/env node` script, and `node` is usually not on the fixed
      `PATH`, so its `--version` fails; the error then says so (finding 9). Symlinks are
      resolved when the backend first runs it, so one run uses one release;
    - **token mode only (answer 1)**: the alias' `secret` (a `CLAUDE_CODE_OAUTH_TOKEN` made
      with `claude setup-token`) is passed as `CLAUDE_CODE_OAUTH_TOKEN`, revealed into the
      child's mapping only. The contract keeps `secret` optional (a login mode may come
      later), but an alias without it is refused at the first call (`backend_unavailable`,
      nothing runs) and by `horae doctor` (an error). A secret that cannot be resolved, or
      holds a NUL, is `auth_failed`. Checked locally on 2.1.284: with an empty `HOME` and no
      token the CLI answers "Not logged in" (no ambient login, keychain included, is used);
    - **render, then send** (decision 38): the payload is `{argv, stdin, max_tokens}`;
      `argv` is fixed but for the model and the system prompt, `stdin` holds the untrusted
      input and the repair errors (fenced and escaped as for every backend; never in `argv`,
      which `ps` shows), `max_tokens` the output budget (passed as
      `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, F; it also sizes the stdout cap, the usage floor and
      the conservative charge). `send` refuses any other payload (`invalid_request`): `argv`
      must have exactly the template's length and equal it item for item (a shorter or
      longer list is refused before any item is read, finding 4). The system prompt (`SYSTEM_PREAMBLE`, then the skill's
      trusted prompt) goes in `--system-prompt`, at most 64 KiB (`invalid_request`);
    - **the flags (answer 3)**: `-p --output-format json --model <model> --tools ""
      --strict-mcp-config --mcp-config {"mcpServers":{}} --setting-sources ""
      --no-session-persistence --disable-slash-commands --permission-mode dontAsk
      --max-turns 1 --system-prompt <system>`. On 2.1.284 every flag is listed by `claude
      --help` but `--max-turns` (hidden), and every one is **accepted** by the parser (checked
      locally with an empty `HOME`: an unknown option fails with `unknown option`, these do
      not). Their **behaviour** is verified by the live test
      (`tests/test_llm_claude_cli_live.py`, `@pytest.mark.live`, skipped unless
      `HORAE_LIVE_CLAUDE=1` and a token are given, never in CI): no tool runs and none is
      reported, user settings and their hooks are not loaded, user and project MCP servers
      do not start, no `CLAUDE.md` reaches the model (in the working directory, in `HOME` or
      in an ancestor of the workspace), the system prompt replaces the default one, no
      transcript or memory is written, no ambient login is used, the output limit is
      honoured. **Each negative assertion has a positive control** (finding 2): the same
      source with the one flag or switch that removes it turned back on must fire its
      canary first, else the test fails as broken for that release. **(E) merge gate, not
      yet run live**: the maintainer runs the live test once and pastes its output into
      #105's body before the flag set is final and the PR merges. `--bare`, `--restricted`
      and `--safe-mode` are **not** in `argv` (B): `--bare`'s help says OAuth and the
      keychain are never read under it, which seems to exclude token auth; the live test
      prints what each does with the token (evaluation only) and one is adopted only once a
      live run shows it keeps token auth;
    - **the environment**: exactly `PATH` (the fixed one), `HOME` and `TMPDIR` (fresh empty
      per-call directories, 0700), `LANG=C.UTF-8`, `TZ=UTC`, `DISABLE_AUTOUPDATER=1`,
      `DISABLE_TELEMETRY=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
      `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (B),
      `GIT_CEILING_DIRECTORIES` (the workspace root, C), `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (the
      call's `max_tokens`, F; not for the version check) and the token; nothing is inherited
      (`ANTHROPIC_*`, `*_PROXY`, `NODE_OPTIONS`, `CLAUDE_CONFIG_DIR` never reach it), built on
      the plugin host's `base_env`. **To verify live**: that the switches stop auto-update,
      telemetry and non-essential traffic on the pinned release (not checkable from the
      test: no network capture), that `CLAUDE_CODE_DISABLE_CLAUDE_MDS` and
      `CLAUDE_CODE_DISABLE_AUTO_MEMORY` do what their names say (the `CLAUDE.md` and memory
      live tests), and that `CLAUDE_CODE_MAX_OUTPUT_TOKENS` bounds the output (its live test;
      a reported `output_tokens` above `max_tokens` is `output_invalid` anyway, see the
      result);
    - **the process** (the plugin host's posture, decision 35): an argument list (no shell),
      the working directory an empty per-call directory, umask 077, a new session (its own
      process group), the per-call directories removed afterwards, also when the caller
      cancels the call (the group is killed first; finding 10). The step's timeout, then
      `SIGTERM` to the group and `SIGKILL` 2 s later (`timeout`, no usage: the ledger keeps
      the reservation as an estimate); stdout at most `response_cap(max_tokens)` bytes (1 to
      8 MiB, the HTTP backends' body cap, T33; `output_invalid`, charged conservatively, the
      group killed at once; finding 7); stderr captured head and tail (64 KiB), its tail
      drained for at most 1 s after the exit before the reader stops (finding 8), and
      redacted with every resolved secret before it reaches an `LlmError`;
    - **(C) the workspace**: each call's `home`, `tmp` and `cwd` are in a fresh directory
      under `<state_dir>/llm-runs` (Horae-owned, created 0700, refused when it is a symlink
      or not this user's, made 0700 when looser), not the system temporary directory, which
      other users share; `GIT_CEILING_DIRECTORIES` is that root, so git finds no repository
      above it (the instance is often one). The factory receives it through
      `horae.llm.backends.model_backends(work_dir=...)`, which `horae.runner.open_ports`
      calls with `<state_dir>/llm-runs`; a backend built without one (a test, a library use,
      `horae doctor`'s version check, which writes nothing in the state directory) falls back
      to a fresh 0700 directory in the system temporary directory. A `CLAUDE.md` in the state
      directory or the instance is an ancestor of the working directory: B's switch and
      `--setting-sources ""` keep it out (live-tested with a control);
    - **(D) managed settings**: administrator-managed settings (`/etc/claude-code/
      managed-settings.json` and `managed-mcp.json` on Linux, the same names under
      `/Library/Application Support/ClaudeCode/` on macOS) apply to every `claude` run and
      no flag turns them off: they can add hooks, an environment and MCP servers. `horae
      doctor` **warns** when one of them exists (by name; their contents are never read).
      They, and remote-managed settings (pushed by an organisation's admin console, not
      visible as a file), are an **accepted residual** of T11: they are set by the machine's
      administrator or the plan's organisation, both already trusted with the token;
    - **the version pin (answers 2, 7)**: `CLAUDE_CODE_VERSION_RANGE = ">=2.1.284,<2.2"`
      (2.1.284 is the release checked). `<command> --version` runs once per backend (one per
      alias per run), in the same isolated environment without the token; its output must be
      `X.Y.Z (Claude Code)`. Outside the range, or unreadable, the alias is
      `backend_unavailable` for the rest of the run and nothing is sent (when the command is
      a `#!` script whose interpreter is not on the fixed `PATH`, the message names the
      interpreter and says to point `command` at the native binary); `horae doctor` runs
      the same check (an error). Bumping the range needs the live test's output for the new
      release in the bump PR's body and the version in `docs/security.md` T11, and a
      re-verified retry count of a truncated output (`OUTPUT_ATTEMPTS`, the amendment
      below);
    - **the result (answer 5)**: stdout must be UTF-8 and one JSON object (strict parsing)
      with `type: "result"`. `is_error` (anything but `false`): `api_error_status` through the
      HTTP status table, `error_max_turns` → `output_invalid`, a result starting "Not logged
      in" → `auth_failed`, else `host_error` (no `error_max_budget_usd` mapping: Horae passes
      no `--max-budget-usd`, so it is dead code until it does; finding 11); the usage is
      attached only when the result reports spend (so a refusal releases its reservation,
      decision 41). A non-zero exit without a result is `host_error`. A success needs
      `subtype: "success"` (finding 5), `num_turns: 1`, no `permission_denials`, a stop reason
      other than `tool_use` or `max_tokens`, a reported `output_tokens` no larger than
      `max_tokens` (else the limit was not honoured: `output_invalid` with the usage as
      reported, the conservative choice, which the ledger flags `over_reservation` when it
      passes the reservation; finding 1), and a `result` that parses as one JSON value (no
      fence stripping; `--json-schema` is not used, schema validation is #31's); any other
      result is `output_invalid` with its usage. Messages are Horae's; the CLI's text never
      is;
    - **usage and cost (answer 4)**: tokens from `usage` (`input_tokens`, `output_tokens`,
      cache reads and writes), floored as decision 40 K, charged conservatively when missing
      or unusable. `total_cost_usd` (a finite number from 0 to 10⁶, else ignored) is notional
      under a subscription but **counts** against the step and daily budgets: `usd` is the
      larger of it and the alias' `price` applied to the tokens, and `priced` is `true` when
      either is known. `may_write_cache` is `true` (decision 41: Claude Code caches by itself),
      so the input bound is priced at the cache write price. **(A) `price` is required** for
      a `claude-cli` alias an `llm` step uses: `horae doctor` reports its absence as an
      **error** (not a warning), and the backend refuses the first call without one
      (`backend_unavailable`, nothing runs), so every call has a dollar reservation before
      it is made (the notional `total_cost_usd` is known only after). A price under the
      plan's notional rates still bounds the call; the larger of the two settles it.
      **Follow-up**: pass the remaining step `usd` as `--max-budget-usd` (a second fence)
      once `Completion` carries it, with its `error_max_budget_usd` mapping;
    - **conformance**: `claude-cli` joins the backend-agnostic tests of
      `tests/test_llm_backends_shared.py` with a fake `claude` (`tests/claude_cli_fake.py`);
      the HTTP-only ones are skipped there with their reason;
    - **the security review (on #105)**: eleven findings (numbered above where they changed a
      rule) and six provisional decisions, **A** (`price` required), **B** (the `CLAUDE.md`
      and auto-memory switches now; `--bare` / `--restricted` / `--safe-mode` only after a
      live run), **C** (the workspace under `<state_dir>/llm-runs`, `GIT_CEILING_DIRECTORIES`),
      **D** (doctor warns about managed settings; managed and remote-managed settings an
      accepted residual), **E** (the first live run with positive controls in the PR body is
      the merge gate) and **F** (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`), pending the maintainer's
      review like the rest of this decision.
    - **Amendment (#113, live run)**: the first live run against Claude Code 2.1.284 (the
      `haiku` alias) found three things the fake did not show; the rules above are amended
      as follows, and with them the whole live test passes (17 tests, every positive control
      fired). (1) **Code fences.** With Horae's system prompt the model answers
      ```` ```json ```` fenced JSON (also with thinking off), which the strict "one JSON
      value" rule refused. A `result` that is **exactly one** enclosing Markdown code fence
      (an optional `json` info string, any case, whitespace around it) around one JSON value
      is now accepted (`claude_cli.unfenced`): the opening line exactly three backticks and
      the info string (trailing spaces or tabs, LF or CRLF), the closing three backticks on
      a line of their own at the end; prose before or after it, two fences, a fence inside
      text, a one-line fence, `~~~` or four-backtick fences stay `output_invalid`. The
      parser is linear string operations, no regular expression (a backtracking pattern
      was quadratic on a whitespace-only result as large as the stdout cap, blocking the
      event loop; review of #113). `SYSTEM_PREAMBLE` is unchanged (it is shared by
      every backend and part of the LLM cache key). `--json-schema` (the CLI's structured
      output) was considered and **rejected**: it works through an internal
      `StructuredOutput` tool (`num_turns: 2`, `stop_reason: "tool_use"`), which breaks the
      one-turn, no-tool invariants. (2) **Thinking.** The model thinks by default: an
      `{"ok": true}` answer cost 126 output tokens (115 of them thinking) and, at a 64-token
      limit, took a hidden second turn. `MAX_THINKING_TOKENS=0` joins the switches (B): 13
      output tokens, `thinking_tokens: 0`, one turn, and `max_tokens` bounds the answer
      itself (live-tested with a control: without the switch, the same call thinks). (3)
      **The output limit.** An answer longer than `max_tokens` is not cut: Claude Code
      retries the truncated output on its own up to 3 times, then exits 1 with `is_error`,
      `subtype: "success"`, no `api_error_status`, `num_turns: 4` and `output_tokens` 4 ×
      `max_tokens` (256 at 64). So one call can bill **4 outputs** of `max_tokens`: a
      provider backend now declares `output_attempts` (1 when absent; an unusable declared
      value is `backend_unavailable`, one above `MAX_OUTPUT_ATTEMPTS` is clamped to it;
      `claude-cli`: `OUTPUT_ATTEMPTS = 4`), carried on `PreparedRequest` and
      `RenderedRequest`; `worst_case` (decision 41) reserves `output_attempts × max_tokens`
      output tokens, and `max_tokens` divides the step's remaining `tokens_out` and `usd` by
      it so that the reservation still fits the step's budget (a `claude-cli` step with
      `tokens_out: 2000` gets `max_tokens` 500; the alias' `max_output_tokens` caps each
      output, not divided). So authors should size a step's `tokens_out` and `usd` for 4
      outputs when its alias is `claude-cli`. The conservative charge and the stdout cap
      cover 4 outputs too. On a success the bound stays `max_tokens` (`num_turns` must be 1:
      one output). An `is_error` result with no HTTP status whose reported `output_tokens`
      is at least `max_tokens`, and that shows the CLI's own retry or limit handling (more
      than one turn or a `success` subtype; with neither it stays `host_error`), is now
      `output_invalid` ("the output was truncated at the limit", on the reported count,
      never the CLI's text); it was `host_error`. It is charged its reported usage, its
      output at least `OUTPUT_ATTEMPTS × max_tokens` when it took more than one turn (the
      CLI billed the retries even if a release reported only the last one). The live
      output-limit test also checks the reported output is at most 4 × `max_tokens`. The
      retry count is the CLI's behaviour, not a documented contract: bumping
      `CLAUDE_CODE_VERSION_RANGE` must re-verify it live (and change `OUTPUT_ATTEMPTS` if a
      release retries more or fewer times). The same run printed that `--restricted` and `--safe-mode` keep token
      auth (a success) and `--bare` does not ("Not logged in"); none is adopted (B: a
      separate decision).

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

### Provisional decisions (#31, pending maintainer review)

44. **Schema-validated output with one repair retry (#31).** (42 is taken by #29.) Contract
    change: none (`llm[].schema_valid`, `repair` and `schema_errors` exist since decisions 21
    and 33; `validator_unavailable` is a new step error kind, an `ErrorKind` string). Rules of
    `horae.llm.validate`, `horae.llm._schema_errors` and `ModelBackends`. A security review
    of the first implementation found 8 issues; **review decisions A to F** below are
    provisional, for the maintainer to confirm:
    - **A. the validator is the default** (review decision D): `ModelBackends.validate`
      defaults to `horae.llm.validate.validate_output` (imported lazily: `horae.llm.validate`
      imports `horae.skills`, which imports `horae.llm.base`), and so does
      `horae.llm.backends.model_backends` (what `open_ports` builds), so decision 38 G holds
      for every `ModelBackends`, not only the one the runner builds. The only opt-out is the
      explicit sentinel `horae.llm.UNVALIDATED` (test doubles of the backends). A `Validator`
      is called `validate(output, schema, timeout_s=...)` and may return an awaitable; a
      synchronous one (test doubles) runs inline;
    - **B. what is validated**: the provider's parsed output, **before** the reserved
      `intents` are split off, against the skill's **full** output schema
      (`RenderedRequest.output_schema`, the skill file; never the provider's transformed
      copy, which drops constraints: `anthropic.transform_schema` moves `maxLength` into a
      description, and the validator still reports `/x: maxLength`), with `jsonschema`'s
      `Draft202012Validator`. Formats are annotations (draft 2020-12's default): asserting
      them would depend on optional packages (`rfc3339-validator`...), so the same output
      would be valid in one install and not in another. Then the reserved `intents`, when
      present, must be an array of objects. First, in the parent, the output re-serialised as
      decision 39 E bounds it (compact, `ensure_ascii=False`, UTF-8) must fit
      `MAX_OUTPUT_BYTES`; a larger one is the single repair error `SIZE_ERROR` (the model's
      fault) and never reaches the worker;
    - **C. a killable, isolated worker**: each validation runs in a fresh child process,
      awaited through asyncio pipes, so the event loop is never blocked:

      | | |
      |---|---|
      | interpreter | `sys.executable -I -B horae/llm/_schema_errors.py` (isolated: no `PYTHON*` variables, no user site, not the script's folder on `sys.path`; no bytecode written) |
      | environment | empty (`env={}`) |
      | descriptors | stdin (the request) and stdout (the answer) pipes, stderr `/dev/null`, nothing else (`close_fds`) |
      | working directory | `/` |
      | session | its own (`start_new_session`): no signal of the terminal reaches it |
      | CPU | `RLIMIT_CPU` of the timeout plus one second, set by the worker on itself |
      | memory | `RLIMIT_AS` at `MAX_WORKER_MEMORY` (256 MiB; the worker is about 26 MiB once `jsonschema` is imported), `RLIMIT_DATA` where that cannot be set; macOS refuses both, so the ceiling is Linux only there (review decision A). An allocation past it is a `MemoryError`: exit status 3, `WORKER_ERROR` |
      | timeout | `min(VALIDATION_TIMEOUT_S, the attempt's remaining deadline - VALIDATION_MARGIN_S)` (10 s and 0.5 s; the interpreter start included), then killed and reaped |
      | cancellation | the awaiting task cancelled (the step's or the run's deadline): killed and reaped |
      | answer | read with a bound: at most `MAX_WORKER_OUTPUT` + 1 bytes (64 KiB), a longer answer is not read and the worker is killed; it must be a JSON array of at most 16 strings of 1 to 200 characters |

      Not a thread (it cannot be stopped) nor a process pool (one task cannot be killed
      without breaking the pool, and a forked pool inherits the parent's resolved secrets,
      descriptors and threads). The cost is one interpreter start, about 0.5 s with
      `jsonschema`, at most twice per `llm` step. The worker imports only the standard
      library and `jsonschema` (nothing of `horae`, no instance state). `horae doctor`
      checks, when a pipeline has an `llm` step, that `sys.executable` and the worker script
      exist and that a trivial validation, run exactly as above, gives the expected error
      (`horae.llm.validate.worker_problem`);
    - **D. fail closed, and no repair for the validator's own failure** (review decision
      C): a timeout (`/: timeout: the output could not be validated in time`), a worker that
      cannot start, exits non-zero or answers anything else (`/: validator: the output could
      not be validated`), an unusable schema (`/: schema: ...`) or an output that is not plain
      JSON (`/: json: ...`) is never a pass: `validate_output` raises, and `ModelBackends`
      raises `LlmError("validator_unavailable", <that fixed message>, usage=<the attempt's
      usage>)` (so does any exception of another validator: `the validator failed
      (<type>)`). It is not the model's fault, so **no repair** is made: the step degrades at
      once with kind `validator_unavailable`, its cost charged to the run and settled in the
      ledger (decision 41). No intent of the attempt is kept, as for any failed attempt;
    - **E. repair errors carry no data**: each error is `<pointer>: <keyword>: <description>`,
      where the pointer holds array indexes and property names **the schema declares** only
      (any other segment is `*`; at most 120 characters, cut on a segment), the keyword is
      the failing JSON Schema keyword (`false` for a `false` schema, `invalid` for an
      unlisted one) and the description is Horae's fixed wording, with at most a number or
      type names taken from the **schema** (trusted, the skill author's). Never an instance
      value, an excerpt of the output, an undeclared key, or `jsonschema`'s own message
      (which quotes the value). The errors are de-duplicated, sorted in the **natural order**
      of their pointers (segment by segment, an index before a name, indexes by value:
      `/b/2` before `/b/10`; then by keyword and description), at most `MAX_ERRORS` = 16 (the
      last one then says how many more there were) of at most 200 characters each; at most
      1000 failures are examined. One output and one schema give one list, so a live run
      sends the same repair for the same output; a replay serves the **recorded** errors
      (`horae.replay`), so a later change of this order (or of the wording) changes no
      replay of a recorded run, but changes what a new run records. The provider backends
      still frame the errors as data inside `<schema_errors>` (decision 38). The repair
      attempt sends the same viewed input, prompt and schema plus these errors: nothing new;
    - **F. an invalid output proposes nothing**: when the validator reports errors,
      `ModelBackends` drops the output's `intents` whatever their shape (`LlmResponse.intents`
      is empty, so `LlmCall.intents` of an invalid attempt is `[]`; a non-list `intents` is a
      schema error, no longer `output_invalid`). The engine (unchanged) routes and keeps
      nothing from an attempt with schema errors, makes exactly one repair attempt with the
      remaining step budget (and, through the ledger, the remaining daily allowance;
      decision 41), and after a second invalid output degrades the step (`schema_invalid`):
      no intent of the step enters the record, later steps are skipped, the run is
      `degraded`, which `horae runs` shows (and `--status degraded` lists);
    - **G. a schema cannot make one validation exponential** (review decision B): the
      loader caps the product of `anyOf` / `oneOf` branch counts along any path of the
      output schema, `$ref`s followed (memoised per `$ref`), at `MAX_COMBINATOR_FANOUT` =
      1024, else `skill_invalid` (decision 39 E, amended). An 18-level `$defs` chain that
      branches twice per level (2^18 paths) is refused; alternatives side by side (in
      `properties`, `allOf`) do not multiply. What the cap does not bound (an `anyOf` over a
      long array keeps every branch's errors for every item) the worker's memory ceiling
      does;
    - **H. the deadline and the attempt's cost** (review decision E): the validator is
      given what is left of the attempt's deadline less `VALIDATION_MARGIN_S`, so it times
      out (D: `validator_unavailable`, the usage charged) before the engine's own deadline
      cancels the step. Should the step or the run be cancelled during the validation all
      the same, the cancellation carries the attempt's usage
      (`horae.engine.ports.attach_usage` / `attached_usage`): the ledger settles with it
      (not with the reservation as an estimate) and the engine adds it to the run's cost;
    - **I. `jsonschema` is pinned to one minor version** (review decision F): `jsonschema
      >=4.26,<4.27`. Which outputs are valid, and which errors (keyword, path, order) a
      repair sends, depend on the validator's behaviour, so its version is part of the
      replay contract: the record holds the errors sent, and a new minor version is an
      explicit upgrade (with this decision's tests), not a silent change between installs;
    - **follow-ups**: a warm worker if the start-up cost matters (#29's CLI backend is
      slower anyway); a memory ceiling on macOS (none of its `setrlimit` limits applies);
      #51 (sandboxing plugins) may reuse the worker's isolation.

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
      `horae.llm.validate.validate_output` (`default_validator`; the interim synchronous
      `check_output` is removed with #31, so no validation runs on the event loop), with its
      killable worker, CPU and memory ceilings, at most `CACHE_VALIDATION_TIMEOUT_S` (10 s),
      and the loader's fan-out cap on the schema (decision 44 C, G). An entry that fails, or
      that cannot be validated (fail closed; `validator_unavailable` on a cache read is a
      **miss**, never a degraded step: the model is then called), is a miss and is deleted.
      So an entry written before a validator existed, or tampered with in the store, is
      never reused unchecked;
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
    - **(0) structurally conservative** (security review): rather than chasing each
      obfuscation, the guard refuses whole classes of text the parsers it guards against could
      read differently: control and format characters, any non-trivial tag-like token, any
      link destination with whitespace or a backslash (rules 0a, 0b, 3, 5);
    - **(0b) Markdown escapes.** A renderer drops `\` before ASCII punctuation, so the text
      it renders differs from the text as written (`[x](java\script:…)`, `<https\://…>`).
      Every check also runs on a view with those escapes removed (after each entity-decoding
      pass), and the text is refused if **any** view fails. A backslash is refused outright
      inside a link destination, a reference definition and a tag-like token or autolink;
    - **(0a) no control or format character.** Any Cc character but `\n` (tab and carriage
      return included), any Cf character (zero-width, joiners, bidi controls, BOM, soft
      hyphen) and U+2028 / U+2029 refuse the text before any other scan, also when an entity
      decodes to one (`h&#9;ttps:`). Note text already refused lone CR and U+2028 / U+2029
      (`content_line_break`); this extends it to all guarded text. A ZWJ emoji sequence is
      refused too (known cost);
    - **(2) embeds are always refused**, even when the URL is in the input (they fetch when the
      note renders, without a click): any `![` (Markdown and reference images, Obsidian
      `![[…]]` embeds);
    - **(3) tag-like tokens: an allowlist, exact form.** Every `<` followed by a letter, `/`,
      `!` or `?` starts a tag-like token, up to its `>`. It is allowed only as exactly
      `<name>`, `</name>` or `<name/>` with a name of `b i u s em strong code pre br hr p ul ol
      li blockquote h1`–`h6 kbd sup sub mark del` (no attribute, no `:`, no space), or as an
      autolink: content up to `>` that `urls` reads as one URL with a `scheme://` or a
      special scheme, and no whitespace, quote or `=` (the URL then goes through (4)).
      Everything else is refused: other tags (`img`, `iframe`, `svg`, `a`, `div`, `span`,
      custom elements…), any attribute (`on*`, `style`, `background`, `poster`, `srcset`,
      `ping`, `href`, so also `url(` and `@import`), `<name:…>` forms, comments, doctype,
      processing instructions and an unclosed `<b`. A denylist of embed tags, then an
      autolink exemption, were rejected in review;
    - **(4) URLs.** Backslashes are read as `/` first (`https:/\host`, `\\host`). A URL is
      a run (up to whitespace, `<`, `>` or a control character) starting at `scheme:` followed
      by `/` (any `[a-z][a-z0-9+.-]*`, whatever precedes it), at a scheme of `http https ftp ws
      wss obsidian mailto javascript vbscript data file blob` followed by any non-space
      character (`ftp:host`), at `//` at a token boundary (protocol-relative, not after a
      scheme character, `:` or `/`), or at `www.` at a word start. A URL with a **refused
      scheme** (`javascript vbscript data file blob`) is always refused. Every other URL
      (answer 3) must be an **exact string** extracted the same way from the **viewed** input
      (string values and object keys of what the model was shown), case-sensitive, after
      dropping at most 16 trailing punctuation characters (`.,;:!?)]}'"*_~` and backtick) on
      either side. So a known URL may be copied, but never extended (`…?q=<data>`,
      `…/<data>`). A `notify.push` `link` must be exactly such a URL **and** fall under the
      entry's `link_prefix` (decision 30). A per-pipeline `strict_links` (only trusted
      parameters and `link_prefix`) is a follow-up;
    - **(5) link destinations.** After `](` (up to the next `)`, or the line end when there
      is none) and after a line's leading `[label]:` (not a `[^footnote]:`; up to the line
      end; container prefixes skipped, (H2)), leading spaces skipped: a destination holding
      **any whitespace or backslash** (so also a title,
      `[x]( javascript :…)` or a line break) or starting with a refused scheme (also inside
      `<…>`) is refused; its URLs go through (4). A refused scheme in prose (`data: three
      items`: colon then a space) is not a link in Markdown and passes;
    - **fields scanned**: `note.*` `content` and `heading`, `task.create` `title` and
      `fingerprint`, `notify.push` `title`, `body` and `link`, every string and object key of an
      `http.post` `body`. Targets bound by routing (`path`, `http.post` `url`) are not text;
    - **robust parsing, bounded work.** Every check runs on the text as written and on its
      HTML-entity-decoded forms (up to three passes: `&#x3c;img`, `&amp;lt;img`) and their
      Markdown-unescaped forms (0b), ASCII
      case-insensitively. Scans are linear (`find` loops that resume after each match, regular
      expressions without nested quantifiers, pointers to the next `)`, line end or whitespace
      reused while ahead); a test scans 1 MiB of hostile text per field, and a seeded fuzz
      (case flips, decimal and hex entities, Markdown escapes, controls and format characters inserted into
      embeds, script links and unknown URLs) never passes;
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
      awaits the cache's `discard(key, model_alias=...)` (a port member, (H4)); `StoreLlmCache.discard`
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
    - **known limits**: obfuscated URLs that are not links in Obsidian (`hxxps://`) pass;
      zero-width and other format characters are refused (0a); a URL the input brings may be copied (tenet 5 as
      written); a brief citing a new URL from the model's own knowledge is refused; being
      conservative, a Windows path (`C:\x`), `a:/b`, `x<b and c` (an unclosed tag-like
      token), a Markdown link title and a ZWJ emoji not in the input are refused too.
    - **hardening round (provisional, pending maintainer review).** Conservative: v1
      protects the **Obsidian rendering surface**, not only CommonMark. In text bound for a
      note or a notification (every guarded field but `http.post` `body`), on every view:
      - **(H1a) active code fences.** A fence of three or more backticks or tildes, after any
        leading spaces (more than CommonMark's three too), blockquote `>` or list prefix,
        whose info string starts (case-insensitively, after spaces or `{`) with `dataview`,
        `dataviewjs`, `templater`, `mermaid`, `js`, `javascript`, `html`, `button` or `query`
        (`ACTIVE_FENCES`): Obsidian or a common plugin runs, queries or draws these instead of
        showing code. Other fences (`python`, none) pass;
      - **(H1b) Templater and inline queries.** `<%` and `%>` anywhere (Templater, inline
        script), `` `= `` and `` `$= `` (Dataview inline queries, their default prefixes);
      - **(H1c) frontmatter.** In a note intent's `content` (all three `note.*` types), a
        first non-blank line of three or more `-` (a frontmatter block can set `cssclasses`,
        aliases and plugin properties). This complements decision 32's note conventions
        (`content_has_marker`, `content_line_break`); a thematic break later in the text
        passes;
      - **(H1d) Mermaid `click`.** `click <id> href`, `click <id> call` or `click <id>` then a
        quote, at a line's content start or after a `;`, anywhere (also outside a fence,
        although (H1a) already refuses a `mermaid` fence). A callback without a quote
        (`click A cb`) is not matched: it needs a Mermaid block, which (H1a) refuses;
      - **(H1e) wikilinks.** A `[[…]]` (up to `]]` or the line end, alias included) holding
        `://` or a refused scheme (`javascript:` …) is refused; plain internal wikilinks
        (`[[Daily/2026-10-27|today]]`) pass. Embeds `![[…]]` stay refused by (2);
      - **(H1f) `obsidian:` URIs** must be in the input set like any URL (4); a miss is
        reported as its own kind (`an obsidian URI not in the input`), and the case-sensitive
        match means `OBSIDIAN://…` is refused unless written so in the input;
      - **(H2) container prefixes before link definitions.** Before a `[label]:`, any spaces
        and tabs, blockquote `>` and list markers (`-`, `*`, `+`, `1.`, `1)`, followed by a
        space, a tab or the line end) are skipped, repeatedly (`> - [r]: javascript:x`). A
        footnote definition `[^x]:` is not a link definition but gets **no exemption**: its
        text goes through the URL, embed and tag checks like any other text (an unknown URL
        in a footnote is refused);
      - **(H3) entity decoding fails closed.** When a fourth `html.unescape` would still
        change the text after the three allowed passes, the text is refused (`too many levels
        of HTML entities`): the guard never checks a text whose rendering it has not seen.
        Text with four or more nested `&amp;` levels is refused (known cost);
      - **(H4) `discard` is part of the `LlmCache` port.** `LlmCache.discard(key, *,
        model_alias)` is now a Protocol member, so mypy rejects a cache without it;
        `horae.engine.NoDiscardCache` is an abstract base whose `discard` is a documented
        no-op (a refused hit then stays a miss each lookup, never routed). The engine awaits
        it directly (exceptions still ignored). A refused `previous` (replay, decision 33;
        the D2 reuse of decision 30) is not a cache entry: it is only a miss and is left as
        is;
      - **(H5) logging and source.** Details and the `warning` line stay content-free. The
        guard's `source` must be `skill:` followed by the pipeline configuration's `SkillRef`
        (`<namespace>/<skill>`, pattern-checked, at most 128 characters); anything else is a
        `ValueError` (a programming error, never input or model text);
      - **(H6) ADR 0002** keeps its accepted text; what #17 and #35 added after acceptance is
        a dated amendment at its end;
      - **bounded work**: the new scans are line loops and `find` loops that resume after each
        match (wikilinks reuse the next `]]` and line end), plus one regular expression
        without nested quantifiers (`click`); a test scans 1 MiB per hostile shape.

### Provisional decisions (#34, pending maintainer review)

47. **De-identification before a cloud model, and no secret in a request (#34, design note
    `docs/design/0034-deidentification.md` and the maintainer's answers on PR #98).**
    Numbering: 46 is #35's (the action guard). Compatible v1 additions to `horae.yaml` and the run
    record, and the rules of `horae.llm.deid`, `horae.llm.base`, the engine and the cache:
    - **(A) local or cloud, explicit.** `models.<alias>.local: true | false` (optional,
      strict boolean). Unset: `ollama` is local, every other backend (`anthropic`,
      `claude-cli`, `openai-compatible`) is cloud (`LOCAL_BACKENDS`,
      `ModelAlias.is_local`); never inferred from `base_url`. `horae doctor`'s price warning
      now applies to every cloud alias by this rule (replacing decision 40's
      `CLOUD_BACKENDS`): an `openai-compatible` alias with `local: true` is not warned, an
      `ollama` alias with `local: false` is. `local` joins the alias' part of the cache's
      stored key and `config_digest` only when set (an unset `local` is left out of both, so
      the digests of an instance that does not use it are unchanged and old runs replay
      without a "horae.yaml changed" error);
    - **(B) what is declared.** Field classes in the skill's input view (#27, decision 39:
      `path: {class: person|email|phone|place|amount|free_text}`, `amount` also
      `buckets: [numbers]`), plus an optional **vocabulary**: `horae.yaml`
      `deidentification: {vocabulary: <file name>}`, a file of `secrets.dir` (personal data:
      never in the instance repository) read like a secret file (`SecretResolver.read_file`:
      one path segment, a regular file of at most 64 KiB, opened once; not registered for
      redaction). Its format is one `class: term` per line (`person`, `email`, `phone`,
      `place`; `#` comments; 2 to 200 characters, no control character, at most 4096
      terms); a plain format, not YAML, so no alias expansion and no parser echoing content.
      Errors name the line, never the term. `horae doctor` reports an unusable vocabulary
      as an error;
    - **(C) the transforms** (`horae.llm.deid.pseudonymize`, a pure function of the viewed
      input, the classes and the vocabulary): every scalar leaf **and every object key**
      under a `person`, `email`, `phone` or `place` field becomes `[PERSON_1]`, `[EMAIL_1]`,
      `[PHONE_1]`, `[PLACE_1]` (a value over `MAX_VALUE_CHARS`, 1 KiB, is withheld as
      `[PERSON]`, not reversible); an `amount` becomes a bucket label (`<0`, `50-200`,
      `>=1000`; default bounds 0, 10, 100, 1000, 10000; `[AMOUNT]` for a value that is not a
      finite number), not reversible. **Every other string of the viewed input and every
      object key** (`free_text` fields and unclassed ones alike: provisional B below) is
      scanned for the known values: the vocabulary terms, the values of this input's
      token-class fields (so "Lunch with Bob" is covered when Bob is an attendee) and each
      name part of a `person` value or term (at least 3 letters, mapped to the whole name's
      pseudonym: provisional C), replaced by their pseudonym; a phone's last 9 digits with
      any `[\s.\-()]` separators and up to 4 digits of prefix; an amount's renderings
      (provisional D), replaced by its bucket label. **Matching**: the string is NFKC-
      normalized and stripped of format characters (zero-width, bidi controls, soft
      hyphen), cut into tokens (letter runs `[^\W\d_]+`, digit runs, white-space runs, other
      single characters), and compared token by token with the known values (letters
      case-folded on both sides, `STRAUSS` = `Strauß`; any white-space run, NBSP and
      ideographic space included, equals any other): a known value matches whole letter
      runs only (`Alice` in `alice_smith`, `user_alice`, `alice123`; not in `Malice`),
      leftmost then longest. A trie over the tokens, at most `MAX_TERM_TOKENS` (48) deep:
      linear in the text whatever the number of terms (600 KB and 4096 terms well under a
      second). A string in which nothing is found is sent byte-identical. Text of the input
      shaped like a pseudonym (`[PERSON_1]`, any case, fullwidth brackets after NFKC) is
      first **neutralized** to `(PERSON_1)` (counted `neutralized`), so the model can never
      be handed a pseudonym that re-identification would turn into a real value. Two keys
      of one object that are the same once pseudonymized fail closed (`deid_unavailable`).
      A classed path under another classed path is refused (`DeidError`, a defence in depth:
      the skill contract already refuses overlapping view paths);
    - **(D) stable per run, no key** (maintainer answer 3). Pseudonyms are numbered per
      class in order of first occurrence: the classed leaves and keys in canonical order
      (sorted keys, array order), then the matches in scanned strings; the same value
      (the same tokens) keeps its pseudonym. The same input always gives byte-identical
      requests, so a replay or a cache hit recomputes the same mapping. Cross-run stability
      (keyed HMAC) is a later option. `DEID_VERSION` is 2 (the matching of the privacy
      review);
    - **(E) where it runs and what is stored.** The engine applies it (`execute(deid=...)`,
      the `horae.engine.ports.Deidentifier` port, `horae.llm.deid.Deidentifier` built by the
      runner and by `horae replay` from `horae.yaml`), for a skill with classes and a cloud
      alias (on by default, no opt-out in v1: answer 7), because the engine is the one
      place that sees the three paths (a call, a cache hit, a replay). Order: input view →
      pseudonymize (in a worker thread, within the step's `timeout_s`) → `LlmRequest` (`input` pseudonymized, `classified`, `deidentified` = the
      summary) → `ModelBackends` renders (appends `DEID_NOTE`: copy placeholders verbatim)
      → T10 check → send → schema validation → the attempt is recorded in `llm[]` and its
      usage spent → **re-identify** the accepted output and raw intents (the growth is
      summed first, `Pseudonyms.growth`: past `MAX_REIDENTIFIED_BYTES`, 256 KiB, the step
      degrades with `output_too_large` and nothing is substituted; the call and its cost
      stay recorded) → the permission check, the action guard (decision 46) and routing, all on the
      **re-identified** intents and against the **real** viewed input (`guard_for(viewed)`):
      a URL or note link present in the real input matches, and no pseudonym reaches the
      guard's link and tag rules (they are put back first; an unresolved one is left as the
      model wrote it and judged as any other text). The mapping lives only in memory: `llm[].response` and `llm[].intents`
      are what the provider returned (pseudonymized), `llm[].input_hash` the hash of what
      was sent, `llm[].request.deid` `{version, input_sha256, counts, unresolved}` (a digest
      of what was sent, never one of the mapping or of a value, which could be guessed);
      the step output and the routed intents carry real values. The LLM cache stores the
      pseudonymized output (from `llm[]`) and the digest of what was sent
      (`deid_input_sha256`), re-identified on a hit; the usage ledger holds no data. A
      reused output (`PreviousLlm`: a cache hit, or a replay's recorded reuse, whose
      `request.deid.input_sha256` is kept) is reused only when its `deid_input_sha256`
      equals this run's de-identified digest (`None` on both sides without
      de-identification): otherwise a miss, and a replay reports a mismatch ("the
      de-identified input differs (vocabulary changed…)"). Both checks of `ModelBackends`
      (`deid_required` and T10) live in `ModelBackends` only: an engine given another
      `LlmBackend` (a test double, the replay tape) has neither, so the runner always wires
      `ModelBackends`. **For #29 (`claude-cli`, PR #105)**: the subprocess' argv and stdin
      must be derived only from the decoded frozen body (`RenderedRequest.body()`), never
      from the `Completion` or the request, so the bytes checked are the bytes sent. As a backstop, `ModelBackends.prepare` refuses a `classified` request to a
      cloud alias that was not de-identified (`deid_required`, an engine given no policy);
      a policy that cannot de-identify (unreadable vocabulary) degrades the step
      (`deid_unavailable`) before anything is sent. Both are not billed;
    - **(F) the cache key** (decision 45 G). For a cloud alias, the stored key adds
      `deid: {version, vocabulary}` (`DEID_VERSION`, the vocabulary file's SHA-256, or
      `"unavailable"`), so another vocabulary or transform version is a miss and an entry is
      only re-identified with the mapping that made it; a local alias' key is unchanged.
      `LLM_KEY_VERSION` is not bumped (the preamble is unchanged; `DEID_NOTE` is part of a
      de-identified request only, and those keys changed anyway). Entries of cloud aliases
      written before #34 are therefore a miss once;
    - **(G) unresolved pseudonyms are left and noted** (answer 4): a pseudonym the input
      never issued, or a mangled one (`Person 1`, `PERSON1`, `[person_1]`), stays as the
      model wrote it and is listed in `llm[].request.deid.unresolved` (at most 16, 40
      characters each); no repair retry. Re-identification puts back the original value's
      **text** (`_text(value)`): a string stays a string, also when it is exactly one
      pseudonym of a number (a phone given as a number comes back as `"33612345678"`), so
      the validated output keeps its types (finding 5);
    - **(H) no secret in a request (T10, the #14 addition).** `ModelBackends.send` scans the
      exact frozen body (`RenderedRequest.body()`) with the process registry
      (`horae.secrets.redact.current()`), primed once with **every secret the instance
      declares** (`ModelBackends.redaction_names` = `Instance.redaction_names()`, the #53 set:
      `secrets.refs`, the lockfile's plugin secrets, the aliases' `secret`), not only those
      this process resolved: the raw bytes and the decoded JSON (strings, object keys,
      whole numbers), every encoding of decision 43. For **every** backend, local ones
      too (a secret in a request is never legitimate, and a local endpoint may log it).
      A match raises `secret_in_request` naming the secrets (never a value; the log
      line gives only their count) before the provider backend is called: nothing is sent, the usage ledger
      releases the reservation (`NOT_BILLED`, decision 41), the step is `degraded` and the
      engine records a `secret_in_request` **violation** (run record v1, a compatible
      addition to `violations[].kind`: answer 6);
    - **(I) after the privacy review (provisional, pending maintainer review).** The
      maintainer's answers on PR #98 stand (1: explicit `local:`, never inferred from
      `base_url`; 2: classes in the input view plus a vocabulary in `secrets.dir`; 3: per
      run; 4: leave and note; 5: render then send; 6: a `secret_in_request` violation;
      7: on by default). Provisional answers to the review's open questions:
      - **A**: `local` stays explicit and is never inferred; `horae doctor` **warns** when
        an `ollama` alias leaves `local` unset and its `base_url` host is neither loopback,
        private, single-label nor a local name (`.localhost`, `.local`, `.lan`,
        `.internal`, `.home.arpa`), or its model tag ends with `-cloud`;
      - **B**: once a skill declares any class, the known-value matcher runs over every
        string of the viewed input (not only `free_text` fields) and every object key;
      - **C**: each name part (at least 3 letters) of a `person` value or term is a term
        mapped to the same pseudonym as the whole name (privacy over meaning: `[PERSON_1]`
        for "Bob" is put back as "Bob Martin");
      - **D**: an amount's renderings (`1234.56`, `1234,56`, `1,234.56`, `1.234,56`,
        `1 234,56`, `1'234.56`, the whole part `1234`; at least 3 digits, not all zeros) in
        a string become its bucket label (not reversible, not a pseudonym; counted in
        `amount`); object keys under a token-class field become pseudonyms (and are put
        back by re-identification);
      - **E**: bucket bounds written by the skill are accepted as they are (skills are
        trusted code, reviewed with their plugin): a skill could choose narrow buckets
        that reveal an amount, as it could simply not classify the field;
      - **F**: the T10 check scans every secret the instance declares (the primed #53
        registry), not only the ones this process resolved (see H);
      - **G**: `input_hash` (the engine's cache key, decision 47 E) stays an unsalted
        SHA-256 of the real viewed input: it is stored locally only (run record, cache),
        never sent, and a salt would break replay and the cache across processes; a
        dictionary attack on it needs the store, which holds the real outputs anyway;
    - **(J) amendment: the secret check before de-identification (security review of
      PR #110).** H scanned only the rendered request, after pseudonymization: a classed
      value or an amount rendering inside a secret (secret `sk-live-alice-smith-1234567`,
      classed person "Alice Smith") was rewritten, so the scan of the whole value no longer
      matched while the rest of the secret was sent (a parser differential). So:
      - the engine scans the **real viewed input** (its canonical JSON bytes, every
        encoding, and its strings, object keys and whole numbers: the same check as
        `secrets_in`) for every resolved or primed secret **before** it is pseudonymized,
        and before the LLM cache is consulted; a match degrades the step with
        `secret_in_request` and records the same violation (naming the secrets, never a
        value), nothing is sent and nothing is billed; `horae eval` (#36, decision 48) runs
        the same check (`horae.engine.secrets_in_viewed`) on each case's viewed input, offline
        and live, before de-identifying: the case is an error (`secret_in_request`) and
        nothing is rendered or sent;
      - `ModelBackends.send` keeps its check of the rendered bytes (defence in depth: the
        prompt, the schema and anything added when rendering);
      - belt and braces, the pseudonymizer replaces no known value, phone or amount
        rendering whose span in a scanned string overlaps a resolved secret, so a secret is
        never sent in part even if the engine's check were bypassed;
      - the engine's check sees the process registry, which the runner primes with every
        declared secret before a run (#53); a declared secret first resolved by
        `ModelBackends` (an engine used without the runner) is caught by the backend's check;
    - **limits**: matching misses nicknames, initials, typos and transliterations, and
      over-matches a known value that is also a common word; a value of more than 48
      tokens is not looked for in text; values in unclassed fields that equal no known value
      are sent as they are (the input view stays the primary control);
    - **follow-ups**: per-instance pseudonyms by keyed HMAC; NER-assisted detection in
      `free_text`; de-identification of data sent to plugins.

### Provisional decisions (#36, pending maintainer review)

48. **Skill evaluation harness: golden cases, recorded responses, `horae eval` (#36).**
    Numbering: 46 is #35's, 47 is #34's (de-identification). Two new contracts,
    `schemas/eval-case/v1.json` (`horae.contracts.evals.EvalCase`) and
    `schemas/eval-recorded/v1.json` (`RecordedResponse`), and the rules of `horae.evals` and
    `horae.cli_eval`:
    - **(A) where the cases live.** Next to the skill, in its folder:
      `skills/<namespace>/<skill>/evals/<case>.yaml` (a case name is `[a-z0-9][a-z0-9_-]{0,63}`,
      at most 256 cases) and `evals/recorded/<backend>/<case>.json` (one recording per
      backend, `<backend>` one of the `models.<alias>.backend` names). The skill loader ignores
      `evals/`, so the cases do not change the skill's digest. Every eval file is opened
      `O_NOFOLLOW` (a link is refused), must be a regular file, and is size-bounded (256 KiB a
      case, 512 KiB a recording); the folders must be real directories;
    - **(B) a case** has `schema_version: 1`, the step `input` **before** the input view (plain
      JSON: a YAML date must be quoted), optional `description`, `targets` (the routed `path` or
      `url` of each target-bound intent type, which a pipeline's `act` entries bind in a run; a
      note type without one gets `Evals/<case>.md`), `budget` (a live call's; default
      `{usd: 0.05, tokens_out: 1024}`) and `expect`: `schema_valid` (default `true`), the exact
      `output` (without its reserved `intents`), `assertions` (each a JSON pointer, RFC 6901, into
      `{"output": ..., "intents": [...]}` with exactly one of `equals`, `length`, `contains` (a
      substring, or an array element) and `exists`), the exact raw `intents` or `intent_counts`
      per type, and `guard` (`pass`, the default, or `violation` for a case that proves a hostile
      output is refused). Equality is canonical JSON equality;
    - **(C) a recording is provider-agnostic** (maintainer's preference): the parsed JSON
      output as the provider backend returned it (the reserved `intents` included), its `usage`
      and `stop_reason`, the concrete `model`, the skill's name, version and digest, and the
      SHA-256 of what was sent (`request`: the skill's prompt, the viewed input, the output
      schema: `RenderedRequest.summary`'s digests). Never the provider's payload, headers or a
      secret. A recording whose skill digest or version, prompt, input or schema digest differs
      from the current one is **stale**: reported (`stale: [skill, prompt, input, schema]`), a
      warning, not a failure (the repository's own CI test refuses stale fixtures);
    - **(D) offline is the run's path, minus routing.** `horae eval <skill>`: the input view
      (`Skill.view`), the request rendered by the **real** provider backend of the alias
      (`ModelBackends.render`: prompt, viewed input, schema, `max_tokens` from the budget, its
      pre-flight), then the recorded result handed back by a provider backend whose `render` is
      the real one and whose `send` returns the recording, so `ModelBackends.send`'s own path
      runs: the production validator (`validate_output`, the killable worker; a
      `validator_unavailable` is an error), the intents split off (an invalid output proposes
      nothing), the redaction. Then the intents' omitted targets are filled from `targets`,
      `validate_intents` checks them against the plugin's `permissions.intents` narrowed to the
      skill's, and the action guard (`check_intents`) runs with the viewed input as what the
      model was shown and the skill's per-type limits. Without `--alias`, each recorded backend
      is evaluated with a synthetic alias (the recorded model, a placeholder secret name and a
      `.invalid` endpoint, which `render` never uses; nothing is resolved or sent); `--alias`
      renders with that instance alias and only its backend's recordings. A backend without a
      factory (`claude-cli` today) is an error (`backend_unavailable`). No network, no secret,
      no cost: CI runs every skill's cases (`tests/test_evals.py`, over `tests/fixtures/plugins`
      and the built-in plugins);
    - **(E) the skill** comes from the instance's installed plugins (`<instance>/plugins`,
      checked against `horae.lock`, `plugins_invalid` 1) or, with `--plugin-dir`, a plugin
      folder or a folder of plugins (folders without `plugin.yaml` are skipped; see M); it is
      loaded as the engine loads it (`load_skill` with the plugin's `permissions.intents`,
      `skill_invalid` 1). An unknown skill is `unknown_skill` (3), a malformed name a usage
      error (2), an unknown `--case` `unknown_case` (3), an invalid case or recording
      `eval_invalid` (1), a skill without cases `no_cases` (1);
    - **(F) `--live` spends money, explicitly, and records it** (D1, review findings 1 to 4).
      It needs the instance (its alias and secret; `--alias`, required when the instance has
      more than one, `unknown_alias` 3). Every case is rendered first and the eval's worst
      case (the sum of `ModelBackends.worst_case`, what the usage ledger reserves) is shown on
      stderr. A **cloud alias without a `price`** (`ModelAlias.is_local` false, decision 47 A)
      is refused (`unpriced_alias`, 3) unless `--allow-unpriced` (D3); `--max-usd` must be a
      finite amount over 0 (usage, 2). The worst case must fit `--max-usd` and what the
      instance's `budgets.daily` has left today (`Store.day_spend`), else `budget_exceeded`
      (3), nothing sent. **`--max-usd` is required unless a daily `usd` ceiling bounds the
      eval** (a `tokens_out`-only ceiling does not bound the cost: usage, 2). Then `--yes`, or
      a confirmation on a terminal (refused: `not_confirmed`, 3; not a terminal without
      `--yes`: usage, 2), then **the allowance is read again** (it may have changed while the
      confirmation waited). Each case is sent **once** (no repair retry: the eval reports
      validity, it does not repair) with the alias' declared key, through a provider backend
      that keeps the raw result for `--record`. A report is written to
      `<state_dir>/evals/<namespace>/<skill>/<UTC timestamp>.json` (the `--json` document,
      with `report` set) and a `.txt` summary beside it;
    - **(G) `--record`** (D2, findings 6 and 12) needs `--live` **and `--plugin-dir`** (a
      plugin's own folder: never the instance's installed plugins, which `horae.lock` pins;
      usage, 2). It replaces the recording of each case **whose live answer passes the case**
      (so a case expecting `schema_valid: false` or `guard: violation` records the bad answer
      it proves is caught); any other answer is not written and the case says why
      (`recorded_skipped`). The output and stop reason are redacted with the process registry
      first (decision 43). A recording is written atomically and durably (finding 11):
      `mkstemp` in the backend folder (`O_EXCL`, random name, 0600), written, `fsync`ed,
      `chmod` 0644, `os.replace`d, then the folder `fsync`ed; a recording that cannot be
      written is reported on stderr and the eval goes on;
    - **(H) results and exit codes** (decision 37). Each case (per backend) is `pass`, `fail` (a
      check failed) or `error` (it could not be evaluated: `no_recording`, `input_invalid`,
      `eval_invalid`, `deid_unavailable`, `case_failed`, a backend or validator error). Exit
      `0` every case passed, `4` some failed, `1` some could not be evaluated, the report could
      not be written, or any problem above, `2` usage, `3` unknown or refused, `70` internal,
      `130` interrupted. `--json`: `{schema_version, mode, skill, skill_version,
      skill_digest, plugin, alias, run, cases: [{case, backend, model, status, schema_valid,
      schema_errors, intents_error, guard (pass | violation | not_run), guard_detail, stale,
      checks: [{name, ok, message, diff}], usage, worst_case, recorded_diff, recorded_path,
      recorded_skipped, stop_reason, error}], summary: {cases, passed, failed, errors, stale,
      usage}, estimate, report, report_error, error}` (`error` is `eval_failed`,
      `eval_errors` or `interrupted`, else `null`); a command error is `{schema_version,
      error}`. **The report has its own version, `EVAL_REPORT_SCHEMA_VERSION` = 1**
      (finding 14), not the CLI error envelope's. Diffs are unified diffs of indented
      canonical JSON (sorted keys, ASCII), at most 400 lines; text output escapes untrusted
      characters (`safe`). Schema errors and guard details stay content-free (decisions 44
      and 46);
    - **(I) `pack-daily-brief`** ships three cases for `daily-brief/rank-and-word` with
      `anthropic` recordings (synthetic): `overdue-first` (assertions, one note), `tasks-only`
      (exact output, no intent) and `injected-embed` (a recorded output that obeyed an injected
      image embed; the guard must refuse it). The fixture pack satisfies the issue's "used by
      pack-daily-brief" for now (D7): a real `pack-daily-brief` repository adopting the cases
      is future work. Rendering them for `anthropic` showed that the fixture's output schema
      could not be used for structured output (`transform_schema` needs a `type` beside
      `enum` and `const`): the fixture now declares `"type": "string"` there, and the loader
      requires it (D4);
    - **(J) a live eval is a run of its own** (D1). Under its own lock
      (`<state_dir>/locks/_eval-<sha256 of the pipeline, 16 hex>.lock`: another live eval of
      the skill is `eval_busy`, 3) the eval opens the store **for writing** (created when
      missing, as a run does), abandons a dead eval's `running` row (`recover_pipeline`), and,
      once the gate passed, `begin_run`s a row of pipeline **`eval:<skill>`** (a pipeline name
      never holds `:`; a skill name too long for the store's 64 characters keeps its start and
      ends with a digest). Each case is then sent through **`LedgerLlm.complete`**, like a
      run's `llm` step: prepared, reserved atomically against the daily ceilings inside
      `BEGIN IMMEDIATE` (`MIN_OUTPUT_TOKENS` floor; a ceiling another process spent since the
      pre-flight refuses the case, `budget_exceeded`, nothing sent), sent, then settled or
      released; each case is its own `step` of the row. `Store.finish_bare_run` then marks
      the row `ok` (every case sent) or `failed` (interrupted), with the cases' cost; the row
      never gets a run record. **Migration 7** rebuilds the `runs` table so that a finished
      `eval:` row may have no record (its CHECK allows `substr(pipeline, 1, 5) = 'eval:'`).
      `horae runs` hides `eval:*` rows unless `--all` (or `--pipeline eval:<skill>`); `horae
      usage` and the daily ceilings count them like any other pipeline's;
    - **(K) failures midway** (finding 5). A case whose sending raises anything but a
      cancellation is an error result (`case_failed`, the exception's type only) and the next
      case is sent. Each result is kept as soon as it is known: an interruption (Ctrl-C) or a
      bug midway still finishes the run row (`failed`), settles what the ledger can (an
      interrupted call keeps its reservation, `estimated`), writes the report of the cases
      sent (`error: interrupted`), then exits 130 (or 70). A report that cannot be written is
      said on stderr and in `report_error`, the JSON document is still printed, and the exit
      code is at least 1;
    - **(L) report files** (finding 10). Each folder level under the state directory
      (`evals/`, the namespace, the skill) is created 0700 and must be a real directory (not
      a link); the `.json` and `.txt` are written 0600 to new temporary names (`O_CREAT |
      O_EXCL | O_NOFOLLOW`), `fsync`ed, then renamed into place, the `.txt` first and the
      `.json` last; an existing report is never replaced;
    - **(M) eval files** (findings 7 and 13). Dotfiles (`.DS_Store`, editors' swap files,
      temporary recordings) in `evals/`, `recorded/` and the backend folders are skipped. A
      case file whose name is not a case name, an invalid case and an invalid recording are
      **error results of that case** (`eval_invalid`, the file named), the other cases still
      run; `--case` must be a case name (usage, 2). With `--plugin-dir`, an invalid
      `plugin.yaml` of the folder itself is `plugins_invalid` (1); in a folder of plugins, a
      skill no valid manifest provides is `plugins_invalid` when some manifest is invalid
      (it may be that one's), else `unknown_skill`; **two plugins providing the skill are
      refused** (`plugins_invalid`);
    - **(N) the judge shares the engine's rules** (D5, findings 8 and 9). `_Replay` is kept
      (offline renders with the real backend and replays the recording), but a case **fails**
      (`stop_reason` check) when the recorded or live stop reason is a refusal or a
      truncation (`horae.llm.backends.UNUSABLE_STOP_REASONS` = `REFUSAL_STOP_REASONS`
      `{refusal, content_filter}` | `TRUNCATION_STOP_REASONS` `{max_tokens,
      model_context_window_exceeded, length, model_length}`, shared by the backends). The
      engine's `fill_target(payload, binds)` fills omitted targets (from the case's
      `targets`) and `check_task_fingerprints(typed, shown)` refuses a `task.update_status`
      of a task the model was not shown (`intents_error`), both used by `_Run.propose` and by
      the eval. A case also fails when its usage is over its budget (`usage_within_budget`).
      `budget_from: <pipeline>` (a case taking a pipeline's `llm` step budget) is not done:
      a follow-up;
    - **(O) de-identification** (decision 47). A classified skill's viewed input is
      pseudonymized for a cloud alias before it is rendered (offline and live, with the
      instance's policy when an instance is loaded, else the alias' own with no vocabulary),
      the request says so (`classified`, `deidentified`), and the output and intents are
      re-identified before they are judged, as a run does; a vocabulary that cannot be used
      is `deid_unavailable` and nothing is sent. A recording holds what the provider saw (the
      pseudonymized output), never the real values; offline, the same pseudonymization gives
      the same request digests (not stale);
    - **D4** (amends decision 39 E): the skill loader requires a `type` beside `enum` /
      `const`; CI renders every fixture and built-in skill through every backend in
      `FACTORIES` (render only, never sent, `tests/test_evals.py`); `horae doctor` reports a
      schema a backend's structured output refuses, with the offending JSON pointer, and the
      Anthropic render's error now carries that pointer and its cause;
    - **follow-ups**: `budget_from`; a `--repair` live mode that exercises the repair retry;
      `horae eval --all` over every installed skill; a JSON Schema for the eval report
      (`schemas/eval-report/v1.json`); a real `pack-daily-brief` repository with its cases.

### Provisional decisions (#39, pending maintainer review)

49. **The built-in scheduler, external triggers and push monitors (#39).** Numbering: 48 is
    #36's; #38 and #42 may be adding decisions in parallel, so this number may move when they
    merge. Compatible v1 additions to the instance contract (`scheduler.monitors`,
    `scheduler.max_concurrent`, `scheduler.misfire_grace_s`), one new runtime dependency
    (`cronsim`) and one command (`horae serve`); no store migration.
    - **A, cron library: `cronsim`** (`>=2.7,<3`, pinned in `uv.lock`). Pure Python, no
      dependency, typed (`py.typed`), about 1,200 lines with its `explain` module, maintained by the author of
      Healthchecks.io, where it evaluates every check's schedule, so its daylight-saving
      behaviour is exercised in production. `croniter` was considered: larger, and it went
      through an end-of-life notice and a change of maintainers; APScheduler is a whole
      scheduler with its own job store and executors, more than needed (Horae has its run
      lock and store). `cronsim` also validates the fields (`horae.config.cron.cron_problem`): an
      expression the contract's shape accepts but that cannot fire (`0 0 30 2 *`, `61 * * *
      *`) is a loader problem at `trigger.cron` (also in `horae doctor`).
    - **B, time zone.** A trigger is evaluated in its own `timezone`, else `horae.yaml`
      `timezone` (decision 13; UTC when neither is set); fire times are stored and reported
      in UTC. Daylight saving follows Debian cron, as `cronsim` implements it: a time in the
      skipped hour fires once at the first instant after the gap, a time in the repeated hour
      fires once (its first occurrence), and an expression whose hour field is `*` fires at
      every real occurrence (`tests/test_scheduler.py`, Europe/Paris in March and October).
    - **C, mode.** `scheduler.mode` (`builtin` by default) is overridden by the
      `HORAE_SCHEDULER` environment variable when it is set and not blank: `builtin` or
      `external`, matched without regard to case (`External` is `external`), anything else
      a configuration error (`horae serve` exits 1, `horae doctor` reports it). In `external`
      mode `horae serve` fires nothing (an external scheduler runs `horae run <pipeline>`,
      which is unaffected by the mode); the process still runs, for the HTTP API of #37.
    - **D, missed runs and overlap.** Missed fire times are **skipped**, never caught up: on
      start each pipeline waits for its first fire time after now, and a loop that wakes late
      (a suspended host) runs the fire time it waited for once, then the first one after
      now. The push monitor is what reports a missed run. A fire time while the pipeline's
      previous scheduled run is still running or waiting for a slot, or whose run finds the
      per-pipeline `flock` held (a manual `horae run`: `PipelineLocked`; a scheduled run
      takes the same lock as any run, decision 31), is **skipped and recorded**: a warning
      log line and the pipeline's `skipped` / `last_skip` / `last_skip_reason` (`running`,
      `locked`) in the scheduler state. A skipped fire time pings no monitor and writes no
      run row (the run in progress pings and records its own). A clock stepped back never
      fires the same fire time twice (the next one is after both the last one fired and now).
    - **D2, misfire grace.** A fire time that would start more than
      `scheduler.misfire_grace_s` late (default 3600, 1 s to 7 days), after a suspended host
      or while waiting for a slot (D3), is skipped and recorded (`last_skip_reason:
      misfire`), not run: a run long after its time is more surprising than a missed one,
      which the monitor reports.
    - **D3, concurrency.** Scheduled runs are **serialised by default**:
      `scheduler.max_concurrent` (1 to 16, default 1) runs at most at once. A fire time that
      finds no free slot waits, first in first out (state `waiting`), and starts when a run
      ends, unless it is then past its misfire grace (D2); a stop drops the waiting fire
      times. Interaction with the apply lock (decision 34): whatever `max_concurrent` is, two
      applies never overlap, since every run's apply holds the instance-wide apply lock and
      waits for it at most `APPLY_LOCK_WAIT_S` (30 s) before it fails `apply_locked` with
      nothing applied. With `max_concurrent: 1` scheduled runs never wait on each other's
      applies (only on a manual `horae run` or `horae undo`); with more, runs proceed side by
      side and their applies queue on the lock, so a long apply can make another run fail
      `apply_locked`. Runs side by side share the store (SQLite in WAL mode, decision 36).
    - **D4, isolation.** A pipeline whose next fire time cannot be computed (an unknown time
      zone, a date out of range; the loader refuses what it can, but `0 0 29 2 *` is valid
      and rare) logs an error naming the pipeline and the exception's type only, shows no
      `next_fire`, and retries after 1 min, doubling up to 1 h; the other pipelines and the
      runs in progress carry on.
    - **E, the process: `horae serve`.** The long-running entry point of a container (the
      name #37's HTTP API will share: one process). It runs `horae.scheduler.Scheduler`,
      whose `state()` (`SchedulerState`: `mode`, `active`, `started_at`, `max_concurrent`,
      and per pipeline `cron`, `timezone`, `next_fire`, `last_fire`, `last_outcome`,
      `last_run_id`, `last_ping` (`ok`, `failed`, `skipped`, `none`: no monitor), `running`,
      `waiting`, `skipped`, `last_skip`, `last_skip_reason`; `to_dict()` is JSON-ready, times
      as `...Z`) is what `/healthz` will report. The state lives in memory: the skip counts
      and `last_*` fields start afresh on restart (persisting them is a follow-up). Scheduled
      runs run in the process, exactly as `horae run` runs them; a crashed run is logged with
      its run id and `internal error (<type>)`, never the exception's message. The instance
      is **loaded once**: a change to `horae.yaml`, a pipeline or the lock needs a restart.
      `horae serve` has no `--json` (it is a process, not a report; `horae cache purge`
      has none either). The wall clock is re-read at least every 30 s while waiting, so a
      clock step or a suspend delays a fire time by at most that much.
    - **E2, shutdown.** The first SIGTERM or SIGINT stops every loop, drops the fire times
      waiting for a slot and cancels the runs in progress; a cancelled run stops as `horae
      run` does on SIGTERM (partial record saved `failed`; an apply in progress finished
      first and saved with its real status). The run is then **reported with the status it
      was saved with** (status and run id from the saved record, its monitor pinged
      accordingly: an apply that finished is `ok` and `up`); a run stopped before any record
      was saved is recorded `cancelled` (keeping its run id, if it began) and pings
      **nothing**: the monitor's own heartbeat window reports it. `horae run` follows the
      same rule for an interrupted run, and bounds that ping to 2 s. The command then exits
      **0** (a requested stop is the normal end of a server, unlike an interrupted `horae
      run`). A **second** signal stops waiting for the reports and pings (each up to 10 s):
      the runs are still awaited so that their records are saved (bounded by the runner's 5 s
      grace period, or for an apply by its sink calls' `call_timeout_s`), then the command
      exits 128 + that signal (130, 143), so a forced stop is visible. A container should be
      given **at least 20 s** to stop (compose `stop_grace_period: 20s`; Docker's default is
      10 s), more when applies are long.
    - **F, push monitors.** `scheduler.monitors.<pipeline>: {secret, kind}`: the push URL
      is a **secret** (it carries the monitor's token), resolved like any other (decision 25)
      and added to the secrets the instance names, so it is primed for redaction (decision
      43) and checked by `horae doctor`; a monitor for an unknown pipeline is a loader
      problem. `kind: uptime-kuma` (default) sets the `status` (`up` / `down`), `msg` (the
      outcome word) and `ping` (the run's duration in ms) query parameters; `kind:
      healthchecks` pings the URL for success and `<url>/fail` for a failure. `up` is a run
      `ok`, `noop` or `degraded` (a `degraded` run is up, as its exit code is 0); everything
      else is `down` (failed, timeout, lost, crashed, and a run that could not begin: plugins
      not matching the lock, a store, lock or I/O problem, the error kind as the message).
      Pinged after every run of the pipeline, scheduled or `horae run`, never for a dry run,
      a locked pipeline, or a run stopped before any record was saved (E2). `horae doctor`
      resolves the secret and checks it is a valid push URL with the ping's own validator,
      never echoing the value. The network rules are the
      LLM backends' (decisions 24, 40): the URL must be a `BackendUrl` (`https://` anywhere,
      `http://` only to loopback, private or single-label hosts, re-checked at connect time
      by `LanOnlyNetwork`), one `GET`, no retry, no redirect, `trust_env=False`, 10 s for the
      whole exchange, the body bounded at 64 KiB and unused. A failure (timeout, refused,
      non-2xx, unresolvable secret, invalid URL) is one warning in Horae's words, never the
      URL, response or exception message, and **never changes the run's outcome or exit
      code**; the `--json` document of `horae run` is unchanged.
    - **Not done (follow-ups)**: skipped fire times and the `last_*` state are not persisted
      (lost on restart; a store table would need a migration); no reload of a changed
      instance without a restart; no catch-up option; no `/start` ping
      (Healthchecks) to measure durations on the monitor side; the push URL with Uptime
      Kuma's default query (`?status=up&msg=OK&ping=`) is redacted as a whole, so the
      rewritten URL is never logged by design rather than by redaction.

### Provisional decisions (#37, pending maintainer review)

51. **The HTTP API: `/healthz` and the read-only `/runs` (#37).** Three new runtime
    dependencies (`fastapi`, `uvicorn` and an explicit `starlette>=1.9,<2`, pinned in
    `uv.lock`, all older than the 7-day minimum release age), new options of `horae serve`
    (`--host`, `--port`, `--allowed-host`), one optional argument of `Store.list_runs`
    (`before`, a page cursor) and one of `Store.open` (`immutable`, E); no contract change, no
    store migration.
    - **A, one process, one event loop.** The API is a FastAPI application served by
      uvicorn's programmatic `Server` inside `horae serve`, in the scheduler's event loop
      (`horae.api`): `/healthz` reads the live `Scheduler.state()`, and there is one process
      to supervise. Horae binds the socket itself before anything starts (a port in use or a
      bad host is a configuration error, exit 1, `bind_failed`, Horae's message), and
      uvicorn's own signal handlers, log configuration, access log, `Server` header,
      proxy-header rewriting and lifespan are off: the redacting log record factory
      (decision 43) stays in place, and request logging is the proxy's job. FastAPI is
      imported by `horae serve` only. `/runs` and `/runs/{id}` are synchronous and run in
      the shared worker thread pool, so a store read never blocks the scheduler's loop.
      `/healthz` is asynchronous: its store check runs in a small executor of its own (two
      threads), so a pool busy with slow `/runs` requests never fails the health check and
      SQLite never runs on the loop; it reads `Scheduler.state()` on the event loop, the
      scheduler's own, so the snapshot never interleaves with the scheduler changing it (no
      worker thread reads the scheduler).
    - **B, binding.** `--host` (`HORAE_HOST`, default `127.0.0.1`) and `--port`
      (`HORAE_PORT`, default 8080). The container image sets `HORAE_HOST=0.0.0.0` so that the
      reverse proxy (Coolify's Traefik) can reach it on the container network; the port is
      `EXPOSE`d, never meant to be published. `HORAE_HOST=::` listens on IPv6 only
      (`IPV6_V6ONLY`), so the image's IPv4 loopback `HEALTHCHECK` then fails: use `0.0.0.0`
      or override the check. **The API has no authentication of its own:** SSO is the
      proxy's middleware (PLAN §7, #41: only `/act/` is exempt; `/runs` and `/healthz`
      require SSO). It must never be exposed without that proxy. **The Docker network is
      trusted:** a neighbour container on it can reach port 8080 directly and send any `Host`
      it likes (the Host check of G2 is a defence against browsers, not against
      neighbours), so #41's template must put Horae on a network shared only with the proxy.
    - **C, `/healthz`.** `200` when the store opens read-only at this Horae's schema version
      **and** the scheduler is active (`SchedulerState.active`) or its mode is `external`;
      `503` otherwise (also while `horae serve` stops). Body: `schema_version` (1),
      `status` (`ok` / `unavailable`), `version`, `store` (`status` `ok` / `error`, `reason`
      `missing` / `schema_too_old` / `schema_too_new` / `unreadable` or `null`,
      `schema_version`) and `scheduler` (`SchedulerState.to_dict()`, decision 49 E). No path,
      no configuration value beyond the scheduler state, no exception text. `horae serve`
      creates and migrates the store once at start, so a new instance is healthy before its
      first run (a failure there is a warning; `/healthz` then says `error`). The body stays
      this detailed (pipelines, cron expressions, fire times, outcomes): it is behind SSO
      (#41), and keeping neighbours off the network is #41's job (B), so a reduced
      unauthenticated body is not needed. Behind the SSO proxy an external monitor needs its
      own way in (a proxy rule for the monitor, or Uptime Kuma on the same network): an
      operator choice, not Horae's (#41).
    - **D, `/runs`.** `GET /runs` lists runs newest first (start time, then id) as `horae
      runs --json` shows them (`horae.run_view.run_json`, shared with the CLI: id, pipeline,
      status, `stale`, dry run, abandoned, times, duration, cost, error; never the run
      record, `pid` or `host`). **Keyset pagination**: `limit` (1 to 100, default 20) and an
      opaque `cursor` (`next_cursor` of the previous page, `null` on the last), stable while
      runs are added (a new run never shifts a page, unlike an offset); runs that share a
      start time stay in id order across pages, and a cursor is a position, so it still
      works when its run has been pruned. Only the exact cursor shape the API writes is
      accepted (canonical base64url of `[<UTC isoformat time>, <run id>]`); anything else,
      a time that would overflow included, is `400`, never `500`. Filters `pipeline` and
      `status`, which takes the same values as `horae runs --status`
      (`run_view.RunStatusFilter`: `running`, `ok`, `noop`, `degraded`, `failed`, `undone`;
      `stale` is a flag computed from the locks after the query, not a status, in either);
      live skill evals (`eval:<skill>`) only with `all=true` or their own `pipeline`, as in
      the CLI. `GET /runs/{id}` is one run, the same object (`404` when unknown); it stays
      (a link to one run is what the dashboard and #38's messages need). Bad parameters are
      `400 invalid_request` naming the parameter, never echoing its value. Only `GET` routes
      exist (`405` otherwise); no slash redirect (`/runs/` is `404`).
    - **E, read-only store access.** Each request opens the store with
      `Store.open(read_only=True, immutable=False)` (decision 36): no migration, no WAL
      switch, `query_only`, and always plain `mode=ro`, a WAL reader with SQLite's locks and
      the shared `-shm`. The CLI's one-shot reads (`horae runs`) keep `immutable=True`, which
      opens the database `immutable` when there is no `-wal` file and so creates nothing. The
      API cannot: it reads in the scheduler's process, where a run's store writes and, on
      close, checkpoints into the main file at any moment, and an `immutable` connection
      takes no lock and trusts the file not to change, so such a read could be torn (the
      size of the WAL is no bound: a closing writer checkpoints whatever it holds). With
      plain `mode=ro` a reader never blocks the writes and sees only committed data, tested
      with a write transaction open and with a writer thread that checkpoints on every close
      while the API reads. The cost: a read with no writer active may leave an empty `-wal`
      and a 32 KiB `-shm` (the database file's permissions, no data); the next writer's
      close removes them (tested).
    - **F, no secrets in responses.** Every body, errors included, is redacted as data
      (`scrub_data`: strings, keys, numbers) and then as text with the process registry
      (decision 43), which `horae serve` primes with every secret the instance names; the
      store already redacts what it writes, so this is the second line. Error bodies are
      Horae's (`{schema_version, error: {kind, message}}`): an internal error shows its
      exception type only, a validation error the parameter's name only.
    - **G, headers and surface.** Every response from the app: `Cache-Control: no-store`,
      `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
      frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`,
      `Content-Type: application/json` (uvicorn's own `400` for a request it cannot parse
      never reaches the app and has none). No CORS (no `Access-Control-*` header; a
      preflight is `405`). The OpenAPI document and the docs pages are **disabled**, on
      every interface (not only off loopback): the API is three routes, documented here and
      in the README, and a schema endpoint would be one more thing to keep behind SSO.
    - **G2, Host check (DNS rebinding; added after the security review of this branch).**
      With the default loopback bind, a web page could point its own name at 127.0.0.1 and
      read `/runs` and `/healthz` from the user's browser (same origin for the page). So a
      request whose `Host` is not an allowed host is refused before any route runs: `400`,
      `{kind: invalid_host, message: "host not allowed (see HORAE_ALLOWED_HOSTS)"}`, the host
      never echoed, with the security headers. Always allowed: `localhost`, `127.0.0.1`,
      `::1` (any port; the container's `HEALTHCHECK` and a local client). Also allowed: each
      `--allowed-host` (`HORAE_ALLOWED_HOSTS`, space-separated): the proxy's public hostname,
      which Traefik forwards as `Host`; and the bind address itself when it is not a
      wildcard. An allowed host is a DNS name (`_` allowed too: Docker service names) or an
      IP address, compared exactly after lower-casing and dropping a trailing dot: no
      wildcard, no port, no sub-domain matching (a bad value is a usage error, exit 2). A
      missing, malformed or repeated `Host` is refused. **A wrong `Host` is visible, not
      refused at start**: the loopback `HEALTHCHECK` stays green while every proxied request
      is refused, so `horae serve` logs a warning at start when it listens on a wildcard
      (`0.0.0.0`, `::`) with no allowed host, naming `HORAE_ALLOWED_HOSTS`, and logs each
      refused `Host` value once (warning, `horae.api`; `ascii()`-escaped so no control or
      bidi character reaches the log, truncated to 64 characters, at most 32 distinct values
      then one line saying it stops, through the redacting record factory). It warns rather
      than refuses to start: a wildcard bind with loopback clients only is a valid setup.
      Allowed hosts are set on the command line (and the environment) only, not in
      `horae.yaml`: it is a property of the deployment, like `--host`, and needs no contract
      change; an `api.allowed_hosts` key is a follow-up if instance repos want it.
    - **H, shutdown.** The first SIGTERM / SIGINT stops the scheduler (decision 49 E2) and
      asks the server to stop: it closes its socket and waits up to 5 s for requests in
      progress. The command exits 0 once both have stopped; a second signal also forces the
      server. A server that ends on its own stops the scheduler and exits 70.
    - **I, container.** The image's default command is `horae serve` (it was `--help`; it
      stays `serve`), and its `HEALTHCHECK` asks `/healthz` over loopback with the standard
      library (`python -c`, `urllib`, no proxy): no curl or wget in the image. The check
      follows `HORAE_PORT`, not a `--port` flag: change the port with `HORAE_PORT` (a `serve
      --port N` command leaves the check asking 8080 and the container unhealthy). A
      container running another command (`init`, `doctor`, `run`) reports unhealthy, which
      is harmless for a one-off; one with `HORAE_HOST` set to `::` or to a non-loopback
      address other than `0.0.0.0` must override the check. `scripts/smoke-image.sh` checks
      that `horae serve` becomes healthy with the image's own check, serves `/runs`, warns
      about `HORAE_ALLOWED_HOSTS` and exits 0 on `docker stop`; under emulation (QEMU) it
      waits longer (`SMOKE_SERVE_WAIT_S`, `SMOKE_PROBE_TIMEOUT_S`, `SMOKE_STOP_S`).
    - **J, dependencies.** `starlette` is a direct dependency with its own range
      (`>=1.9,<2`): the API relies on Starlette's handler behaviour (exception handlers,
      middleware order, `redirect_slashes`), so a change of major version must be a
      deliberate bump, not a side effect of a FastAPI update.
    - **Not done (follow-ups)**: `/act` (#38), the SSO-exemption test against the rendered
      template and the network that only the proxy shares (#41), `api.allowed_hosts` in
      `horae.yaml`; no ETag or conditional requests; no metrics endpoint.
