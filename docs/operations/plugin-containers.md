# Plugin containers (`runtime: container`)

[#49](https://github.com/horae-dev/horae/issues/49), schemas
[decision 74](../../schemas/README.md#provisional-decisions-49-pending-maintainer-review)
(provisional). Part of M5 hard isolation: the egress proxy (#50), declared mounts and resource
limits (#51) and the malicious-plugin suite (#52) build on it.

By default (`runtime: process`) a plugin is a child process of Horae: same user, same network,
same filesystem (the "declared" tier, `docs/security.md` T04 and T05). With `runtime: container`
each plugin runs in its own Docker container, and sees only its own directory, read-only, with
no network at all. The MCP session, timeouts, restarts, results and redaction are the same in
both runtimes: only how the plugin is started changes.

## Enabling it

In `horae.yaml`, for every plugin or per plugin (a plugin's own setting wins):

```yaml
plugin_defaults:
  runtime: container          # process (default) | container
plugins:
  file-sink:
    runtime: process          # built-in plugins have no venv of their own: see below
  fixture-source:
    runtime: process
  github:
    runtime: container        # explicit, or inherited from plugin_defaults
container:                    # optional
  image: python:3.12-slim-bookworm@sha256:<digest>   # default: the Horae image's base
  docker: /usr/bin/docker     # default: `docker` on /usr/local/bin:/usr/bin:/bin
```

The operator chooses, never the plugin: the runtime is not in `plugin.yaml` nor in
`horae.lock`, so a plugin cannot ask to run outside a container.

**Prerequisites**, checked before each run's first container start; anything missing **fails
the plugin** with `runtime_unavailable` (it is not started, and never run as a process instead):

1. A Docker CLI that reaches a **local** Docker Engine through a `unix://` socket (the
   `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` of Horae's environment are honoured;
   nothing else of it is passed to the CLI). A `tcp://` or `ssh://` engine, set by `DOCKER_HOST`
   or by the current context, is refused: it would receive the plugin's secrets over the network
   and mount paths of its own host.
2. The image, pulled once: `docker pull <image>` (runs use `--pull never`, nothing is fetched at
   run time).
3. Horae running as a non-root user: the container runs as Horae's own uid and gid (so it can
   read the plugin tree Horae installed), and Horae refuses to start one as root.
4. The plugin installed by `horae plugin add` with its own venv (`plugins/<name>/.venv`,
   decision 69) **built by `/usr/local/bin/python3.12`**, which is what the Horae image does.
   A venv is linked to the interpreter that built it, and the plugin image has that interpreter
   at that path; a venv built elsewhere (a developer's machine, a distribution's
   `/usr/bin/python3.12`) is refused with a message naming its interpreter. The built-in plugins
   (`file-sink`, `fixture-source`) run from Horae's own interpreter and have no venv: keep them
   on `runtime: process`.
5. The plugin's command must run from its venv: `[python, -m, <module>]`, a console script of
   the venv, or a path inside the plugin directory. An absolute command (a host path) is refused.

## What a plugin container is

`docker run --interactive --rm --init` with:

| Setting | Why |
|---|---|
| `--user <horae uid>:<gid>`, never 0 | non-root; reads only what Horae's user can |
| `--read-only` | the image cannot be changed |
| `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777` | the only writable place (`HOME=/tmp/home`, `TMPDIR=/tmp/tmp`, working directory `/tmp/work`), nothing executable there |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | no capability, no setuid escalation |
| Docker's default seccomp profile | never overridden (no `seccomp=unconfined`, no `--privileged`, `--cap-add`, `--device`, host namespace) |
| `--mount type=bind,source=<plugins/name>,target=/plugin,readonly` | the **only** mount: the plugin's tree and its venv |
| `--network horae-plugin-<name>` | its own network, see below |
| `--init` | Docker's init is PID 1: children are reaped and signalled with the plugin |
| `--log-driver none` | the daemon keeps no copy of the plugin's output (stderr may echo a secret) |
| `--pull never` | nothing is downloaded at run time |
| `--label dev.horae.plugin=<name>`, `--name horae-<name>-<random>` | to find and remove it |

The plugin sees neither `horae.yaml`, the lock, the store, the vault, `/run/secrets`, other
plugins nor Horae's processes and environment. `tests/test_plugin_container_live.py` checks all
of this from inside a container (uid, capabilities, `NoNewPrivs`, seccomp mode, read-only
filesystem and mount, `noexec` tmpfs, visible processes) and from `docker inspect`.

### The network

Each plugin gets its own bridge network, `horae-plugin-<name>`, which Horae creates on first use
and checks on each run:

- `--internal`: no route out of the host;
- `com.docker.network.bridge.enable_icc=false`: no traffic between containers on it;
- `com.docker.network.bridge.inhibit_ipv4=true`: **the host has no address on it.** On a plain
  internal network the host's bridge address is the containers' gateway, and every service the
  host exposes on `0.0.0.0` (Horae's own API on a bare-metal install, sshd, a database, a
  container publishing a port) answers there: this was measured on Docker 29 before choosing
  the option, and the live test checks a host listener on `0.0.0.0` is not reachable.

It must also carry the label `dev.horae.network=plugin`, must not be a configuration-only
network or take its configuration from another one, and every container already attached to it
must be labelled `dev.horae.plugin=<name>` (the same plugin in a concurrent run) or
`dev.horae.egress-proxy` (reserved for the #50 proxy). A network of that name that does not
pass all of this (IPv6 on, another driver, made by hand, a foreign container attached) is
refused, never changed or deleted: `docker network rm horae-plugin-<name>` and the
next run recreates it. Until #50 adds the egress proxy a container has **no network access at
all**: no DNS, no Internet, no LAN. A plugin that needs `permissions.network` or
`network_local` fails its calls under this runtime until then (the grants still travel in
`_meta["horae/network"]`).

### Secrets

Secrets are never on a command line (visible in `ps`) nor in the container's configuration
(`docker inspect`, which shows the image's own `ENV` only). Horae writes the plugin's
environment (the same one the process runtime builds: the base variables, `UV_OFFLINE`,
`PIP_NO_INDEX`, `VIRTUAL_ENV` and the declared secrets) as the **first line of the container's
stdin**, one JSON object. A launcher (`python3 -I -S -c <source>`, a constant in
`horae.plugins.host.container`, secret-free and visible in `docker inspect`) reads exactly that
line, one byte at a time so nothing of the MCP stream is consumed, and `execve`s the plugin with
exactly that environment. The values cross the CLI's connection to the Docker daemon (its local
socket) and live in the plugin's process, where root on the host can read them as it can any
process's environment. Horae zeroes the line's buffer once it is written, but that is **best
effort**: the values also passed through Python strings (the resolved secret, the environment
mapping, the JSON text) and the event loop's write buffer, so copies stay in Horae's memory
until the garbage collector reuses it. The write is bounded by the plugin's startup timeout: an
engine that does not take it fails the start with `startup_timeout`.

### Stopping

As for a process: stdin is closed, then the CLI is sent `SIGTERM` (it forwards it to the
container) and `SIGKILL`; then Horae always runs `docker rm --force <name>`, so a plugin that
ignores end-of-file and signals, or started background processes, is removed with them. If
Horae itself is killed with `SIGKILL`, a container may be left running until its plugin exits
on end-of-file; list such leftovers with `docker ps --filter label=dev.horae.plugin` (Horae does
not sweep them: it could remove the container of a concurrent run).

## The image

`container.image` defaults to `python:3.12-slim-bookworm@sha256:<digest>`, **the same base image
the Horae image is built from** (`PYTHON_IMAGE` in the [`Dockerfile`](../../Dockerfile)). No
image is built: plugin venvs need exactly that interpreter, at `/usr/local/bin/python3.12`, and
the base image already is the minimal image that has it. It is pinned by digest; `horae.yaml`
refuses an image reference without `@sha256:<64 hex>`. `tests/test_plugin_container.py` keeps
the default equal to the Dockerfile's `PYTHON_IMAGE`, and Renovate bumps both in one PR (a regex
manager on `src/horae/contracts/instance.py`, grouped with the Dockerfile in `renovate.json`).
After such a bump, `docker pull` the new digest before the next run (the runs refuse a missing
image rather than fetch it). Use another image (a registry mirror) only if it has the same
interpreter at the same path; it must also be pinned by digest.

## Horae inside Docker (Coolify, compose)

The published Horae image ships **no Docker CLI and mounts no socket**, so `runtime: container`
reports `runtime_unavailable` there. Giving a containerised Horae the power to start containers
is a deliberate choice with these options, none enabled by default:

| Option | What it costs |
|---|---|
| **Mount the Docker socket** (`/var/run/docker.sock`) into Horae | **A Horae compromise is root on the host**: whoever drives the socket can start a privileged container mounting `/`. This is accepted risk 1 of [`docs/security.md`](../security.md#7-accepted-risks) (T26). The bind-mount paths are then the host's, not Horae's (`/instance/plugins/...` must exist on the host at the same path, or the volume be mounted at an identical path). |
| **A socket proxy** (e.g. one filtering the API by endpoint) | Narrows which API calls Horae can make, but `POST /containers/create` is the one it needs, and its body decides privileges and mounts: a proxy that does not inspect that body does not lower the risk much. |
| **Rootless Docker (or Podman) on the host** | The engine runs as an unprivileged user: a compromise of Horae plus the engine is that user, not root. Podman is not supported by this runtime yet (its networking and user mapping differ and were not verified). |
| **A sidecar** (a small trusted service, the only one with the socket, that starts plugin containers on Horae's request) | Best separation, most work: a follow-up, not designed yet. |

Running Horae directly on the host (a non-root user, Python 3.12 at `/usr/local/bin`, the
host's Docker CLI) needs none of these, at the cost of not using the image.

## Latency and sizing

Measured with the jail fixture plugin (`tests/fixtures/plugins/jail/jail_server.py`, a
stdlib-only MCP server), 10 runs of 50 calls each through `PluginHost`, on an Apple M4 (10
cores, 16 GB) with OrbStack (Docker Engine 29.8, a Linux VM with 10 CPUs and 8 GB):

| | Process | Container |
|---|---|---|
| Start and first call (median) | 83 ms | 314 ms (first ever, with network creation: 701 ms) |
| One call (median / p95) | 51.8 / 52.6 ms | 52.3 / 53.5 ms |
| Stop (median) | 12 ms | 187 ms (`docker rm --force` included) |
| Memory of an idle container | | about 9 MiB (`docker stats`), plus the plugin's own |

A call costs the same in both runtimes (about 50 ms of each is the host's stderr quiet window,
`STDERR_QUIET_S`); a container adds about 0.25 s per plugin start and 0.2 s per stop. A plugin
starts at most twice per run (one restart) and is stopped after its last planned call, so a
pipeline using five plugins pays about 2 s more per run and holds at most five small containers
at a time. That fits a 4-core / 16 GB home server with a wide margin: the cost is the containers'
start, not their footprint. Not measured on such a server itself; re-run the benchmark there
(the live tests are a quick check: `uv run pytest tests/test_plugin_container_live.py`).

## Limits and follow-ups

- **No resource limits yet** (CPU, memory, PIDs): #51, which also adds declared mounts.
- **No egress**: #50 attaches its proxy to the plugin networks.
- **Podman** is not supported; **the Horae image** cannot start containers (above).
- **The tier gate stays**: `horae plugin add` still refuses non-first-party and direct installs
  before M5 (`horae.marketplace.approval`, `DIRECT_INSTALLS`). Lifting it needs #50, #51 and #52,
  and should require `runtime: container` for such plugins.
- `horae doctor` does not check the container prerequisites yet; the first run reports them.
