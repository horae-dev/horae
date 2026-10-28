# Design note: plugin host v1 (#24)

- **Issue:** [#24](https://github.com/horae-dev/horae/issues/24) (Human-led, sensitive path
  `src/horae/plugins/host/`), epic [#2](https://github.com/horae-dev/horae/issues/2), M1
- **Status:** Draft, waiting for maintainer feedback before implementation
- **Builds on:** [ADR 0003](../adr/0003-mcp-as-the-plugin-protocol.md) (MCP, two enforcement
  tiers), [ADR 0002](../adr/0002-intents-applied-by-the-core.md) (intents),
  [ADR 0001](../adr/0001-pipelines-not-agent-loops.md) (no LLM tool selection),
  [PLAN.md §3.3](../PLAN.md#33-plugin-manifest-and-permissions), [§8 M1/M5](../PLAN.md#8-roadmap),
  [schemas decisions](../../schemas/README.md#decisions) 8, 11, 21

## Goals (v1, "declared" tier) and non-goals

Goals, from the acceptance criteria and PLAN.md M1:

1. Start an `mcp-stdio` plugin from its `runtime.command` argument list, **no shell**.
2. Its environment is **built from scratch**: a fixed minimal base plus **only its declared
   `permissions.secrets`**. Proven by a probe plugin (exact equality, not "subset").
3. The engine calls named tools explicitly (no LLM selection); every call has a timeout; a
   timeout, crash or protocol error becomes a typed step error and **never crashes the run**.
4. Plugin stderr is captured, **redacted of secret values**, capped, and stored in
   `StepRecord.stderr` (64 KiB, `schemas/run-record/v1.json`).

Non-goals for v1 (later stories): `mcp-http` runtimes (rejected with `runtime_unsupported`);
intent validation itself (#20, the host only hands intents over); per-plugin venvs (M4); and
everything in [Not enforced until M5](#not-enforced-until-m5).

## Process model

- **One subprocess per plugin per run**, started lazily on the first call to one of its tools and
  reused for every later call in the same run (e.g. a plugin used in `collect` and `act`). All
  processes are stopped in the run's `finally`. No long-lived plugin daemons: each run gets a fresh
  environment and no state carried between runs, which keeps replays honest. Cost: start-up time
  per run, acceptable for cron-scale pipelines (ADR 0003 "Negative").
- **Startup:** spawn, then MCP `initialize` and `tools/list` under a **startup timeout**
  (default 10 s). Every tool the pipeline references for this plugin must be in the manifest
  `provides` *and* in the server's `tools/list`, else `tool_missing` before any call.
- **Client capabilities: none.** The host advertises no `sampling`, `roots` or `elicitation`, and
  refuses such requests if a server sends them anyway: a plugin asking the core's LLM would be an
  undeclared LLM call (ADR 0001, AGENTS.md). Server `notifications/message` log lines go to the
  same redacted capture as stderr.
- **Shutdown** (mirrors the SDK and the MCP lifecycle spec): close stdin, wait 2 s, `SIGTERM` the
  process group, wait 2 s, `SIGKILL`. The child is spawned with `start_new_session=True` so the
  whole tree is killed.
- **Concurrency:** v1 is **sequential**: one call in flight per run, steps and the calls inside a
  step in declaration order. Deterministic and easy to record; parallel `collect` is a question
  below. Plugin processes of one run are independent, so parallelism can be added without changing
  this design.
- **cwd** is a fresh empty per-run directory under the state dir (deleted after the run), never the
  instance root. **argv[0]** is resolved by the core with `shutil.which` against the child `PATH`
  and passed as an absolute path; not found is `spawn_failed`.

## MCP client library

The official `mcp` SDK (PLAN.md §3.6, ADR 0003). Latest on PyPI is **2.3.0** (2026-10-02), older
than the repo's 7-day `exclude-newer` cooldown, so pin `mcp>=2.3,<2.4` and let Renovate move it.
We use its `ClientSession` (handshake, `call_tool(..., read_timeout_seconds=...)`, typed
`CallToolResult` with `content`, `structured_content`, `is_error`).

We **do not use `mcp.client.stdio.stdio_client`**: in 2.3.0 it builds the child environment as
`get_default_environment() | server.env`, which always inherits `HOME`, `LOGNAME`, `PATH`,
`SHELL`, `TERM` and `USER` from the host, and it writes stderr to a `TextIO` we would have to
back with a real fd. Instead `transport.py` spawns the process itself (`anyio.open_process`,
exact env, `stderr=PIPE`) and feeds the SDK's message streams (newline-delimited JSON-RPC), about
100 lines. This also lets us cap the size of one stdout message (default 8 MiB,
`output_too_large`). Monkeypatching the SDK's env helper was rejected (global, fragile on
upgrades). The transport is behind an internal interface, as ADR 0003 asks for SDK churn.

## Environment construction

The child env is exactly `base ∪ declared secrets`; nothing from `os.environ` is copied.

| Variable | Value | Why |
|---|---|---|
| `PATH` | `/usr/local/bin:/usr/bin:/bin` (M4: the plugin venv's `bin` first) | resolve interpreters and tools the plugin itself spawns; never the host `PATH` |
| `HOME` | per-run empty dir | many runtimes fail without `HOME`; must not be the user's real home (dotfile credentials) |
| `TMPDIR` | per-run empty dir | keeps temp files inside the run's cleanup scope |
| `LANG` | `C.UTF-8` | UTF-8 stdio for non-Python plugins (Python already coerces, PEP 538/540) |

Not included: `USER`, `LOGNAME`, `SHELL`, `TERM`, proxies, `TZ` (the run context passes time zone
as data). **Secrets** come from the #16 loader through a small `SecretResolver` protocol
(`resolve(name) -> str`), resolved just before spawn and never logged. A declared but unresolvable
secret fails the step with `secret_missing` before spawn.

Per schemas decision 8, the builder owns the **deny list**: a declared secret may not collide with
a base key or match `HORAE_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `PATH`, `HOME`, `TMPDIR`, `LANG`,
`LC_*`, `TZ`, `NODE_OPTIONS`, `NODE_PATH`, `BASH_ENV`, `ENV`, `PERL5OPT`, `RUBYOPT`,
`*_PROXY`/`*_proxy`, `SSL_CERT_*`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_*`, `IFS`. It is
re-checked at spawn (`env_unsafe`) and surfaced by `horae doctor`. Values with NUL are rejected.

## Timeouts and crash handling

- **Startup** 10 s and **per call** 30 s by default, set in `horae.yaml` (instance default, and
  per plugin); the pipeline contract has no per-call timeout today. The run timeout is #19's.
- **Call timeout:** the SDK raises; the host sends no retry, **kills the process** (it may be
  wedged mid-write) and marks the plugin dead for the rest of the run. Later calls to it fail fast
  with `plugin_unavailable`. No automatic restart in v1 (predictable, no double side effects).
- **Crash** (EOF or exit while a call is in flight, or at startup): `plugin_crashed`, with exit
  code or signal in the message; same "dead for the run" rule.
- All host exceptions are caught at the `PluginHost.call` boundary and returned as a
  `PluginCallError(kind, message)`; unexpected bugs become `host_error`. The run continues.

## Stderr capture and redaction

A reader task drains stderr continuously (a full pipe would block the plugin) into a bounded
**head + tail** capture: the first 16 KiB and a ring of the last 48 KiB, per plugin per step, plus
a count of elided bytes. Memory stays bounded however much the plugin writes.

Redaction, on bytes, before decoding:

1. Patterns = UTF-8 bytes of **every secret value the instance resolved** (not only the plugin's:
   a plugin can echo data from other steps), each line of multi-line values, plus their base64
   and percent-encoded forms; longest first. Each match becomes `[REDACTED:<NAME>]`.
2. Each window keeps a margin of `max_len - 1` extra bytes at its cut edge; after replacement the
   margin is dropped, so a secret straddling a cut can never leave a partial prefix or suffix.
3. Decode with `errors="replace"`, join head, `…[N bytes elided]…`, tail; truncate to 65 536
   characters (the `Stderr` contract) and store in `StepRecord.stderr`, concatenating the plugins
   the step called, each prefixed `--- <plugin> ---`.

The same redactor runs on every `StepError.message` built from plugin data.

## Tool results to step outputs and intents

- `is_error: true` gives `tool_error` (message from the text content, redacted).
- **Data** is `structured_content` (any JSON value). Fallback for plain MCP servers (M6): exactly
  one text block that parses as JSON. Anything else is `output_invalid`. The value lands in
  `StepRecord.output` and, for `collect`, in `InputSnapshot.data`.
- **Intents** proposed by a source or transform travel in the result's `_meta` under
  `dev.horae/intents` (a reserved key that plain servers never set). The host only parses them
  into `Intent` models and returns `ToolOutput(data, intents)`; #20 checks them against
  `permissions.intents` (`intent_undeclared`).
- For `act`, the host passes the validated intent as tool arguments and returns the sink's
  structured result to the engine, which journals it.

## Error taxonomy

`ErrorKind` values the host emits: `runtime_unsupported`, `secret_missing`, `env_unsafe`,
`spawn_failed`, `startup_timeout`, `handshake_failed`, `tool_missing`, `timeout`,
`plugin_crashed`, `plugin_unavailable`, `protocol_error` (JSON-RPC error response, malformed
message), `output_too_large`, `output_invalid`, `tool_error`, `host_error`. Each makes the
**step** `failed`; the step policy of #17 turns that into run `degraded` or `failed`. The host
never sets run status.

## Probe-plugin test design

`tests/fixtures/plugins/probe/`: a `plugin.yaml` and a small stdio MCP server built with the SDK,
run as `[sys.executable, "<abs path>/server.py"]` (the venv interpreter finds its site-packages
without `PYTHONPATH`). Tools: `probe.env` (returns `dict(os.environ)`), `probe.sleep`,
`probe.crash` (`os._exit(3)`), `probe.stderr` (writes a given payload and size).

- Host env seeded with sentinels: an undeclared instance secret, `AWS_SECRET_ACCESS_KEY`,
  `HORAE_X`, `PYTHONPATH`, `LD_PRELOAD`. Assert `probe.env` keys **equal** base keys plus the one
  declared secret, with its correct value, and that `PATH`/`HOME` are the constructed ones.
- Declaring `NODE_OPTIONS` or `LANG` as a secret is refused (`env_unsafe`).
- `probe.sleep` past the timeout: step `failed`/`timeout`, the next step still runs, the process
  is gone. `probe.crash`: `plugin_crashed`, then `plugin_unavailable`.
- `probe.stderr` with the secret value, its base64 form, a secret straddling the head/tail cut, and
  1 MiB of output: the record never contains the value, is at most 64 KiB, shows the marker.
- Redactor and env builder also get pure unit tests (property tests for the cut margin).

## Module layout

```
src/horae/plugins/__init__.py
src/horae/plugins/host/__init__.py   # PluginHost, ToolOutput, PluginCallError
src/horae/plugins/host/env.py        # base env, deny list, build_env()
src/horae/plugins/host/transport.py  # spawn (argv, exact env, new session), JSON-RPC pipes, shutdown
src/horae/plugins/host/capture.py    # head/tail stderr capture
src/horae/plugins/host/redact.py     # Redactor
src/horae/plugins/host/session.py    # one plugin process: ClientSession, handshake, call_tool
src/horae/plugins/host/host.py       # per-run registry, lazy start, timeouts, error mapping
src/horae/plugins/host/errors.py     # error kinds
```

## Not enforced until M5

Honest limits of the "declared" tier (ADR 0003): the plugin runs as the same user as Horae, so it
**can reach any host** (no proxy, #50), **read and write any path** the user can (cwd and `HOME`
are hygiene, not isolation, #51), use unbounded CPU and memory (#51), and on Linux read
`/proc/<horae-pid>/environ`, i.e. **every secret Horae got through its own environment**.
Secrets read from `/run/secrets` files are not exposed that way. The `undeclared_secret`,
`undeclared_path` and `egress_blocked` violations are not detected in v1. Acceptable for
first-party plugins only.

## Questions for the maintainer

1. **SDK footprint.** `mcp` 2.3.0 pulls starlette, uvicorn, httpx2, opentelemetry-api and pyjwt.
   Accept it as a core dependency (FastAPI needs starlette anyway), or keep it in an extra?
2. **Own stdio transport** instead of `stdio_client` (to get an exact env): agreed?
3. **Base env**: is `PATH`, `HOME`, `TMPDIR`, `LANG` the right minimum? Should `TZ` be set from the
   run context?
4. **Redaction scope**: redact all instance secret values (proposed) or only the plugin's? And
   should a secret value found in a tool's *output* fail the step (`secret_in_output`) rather than
   be stored?
5. **Intent channel**: `_meta["dev.horae/intents"]` (proposed) or a `{data, intents}` envelope in
   `structured_content`?
6. **After a timeout or crash**: dead for the rest of the run (proposed) or one restart?
7. **Concurrency**: sequential calls in v1, parallel `collect` across plugins later?
8. **Timeouts**: defaults 10 s / 30 s from `horae.yaml`, or a per-call `timeout_s` in the pipeline
   contract (a `schemas/` change)?
9. **Text-JSON fallback** for servers without `structured_content`: allow in v1 or wait for M6?
10. **`/proc/<pid>/environ` exposure**: should M1 docs recommend `/run/secrets` files over env for
    secrets, given the clean env does not stop a hostile plugin?
