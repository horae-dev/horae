# Plugin containers (`runtime: container`)

[#49](https://github.com/horae-dev/horae/issues/49), schemas
[decision 74](../../schemas/README.md#provisional-decisions-49-pending-maintainer-review)
(provisional), and its egress proxy, [#50](https://github.com/horae-dev/horae/issues/50),
[decision 76](../../schemas/README.md#provisional-decisions-50-pending-maintainer-review)
(provisional), its declared mounts and resource limits,
[#51](https://github.com/horae-dev/horae/issues/51),
[decision 78](../../schemas/README.md#provisional-decisions-51-pending-maintainer-review)
(provisional). Part of M5 hard isolation: the malicious-plugin suite (#52) builds on it.

By default (`runtime: process`) a plugin is a child process of Horae: same user, same network,
same filesystem (the "declared" tier, `docs/security.md` T04 and T05). With `runtime: container`
each plugin runs in its own Docker container, within CPU, memory and process limits, and sees
only its own directory, read-only, and the paths `horae.lock` approves for it. Its only way out
is its own allow-list proxy, which lets through only the hosts `horae.lock` approves for it. The MCP session, timeouts, restarts, results and redaction are the same in
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
  mount_roots: [/srv/vault]   # default: none (only the instance directory), see below
```

Resource limits and declared paths are set in the same file:
[Declared paths](#declared-paths) and [Resource limits](#resource-limits).

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
3. **Docker Engine 25 or later**: the egress proxy is started on two networks in one
   `docker run`.
4. Horae running as a non-root user: the container runs as Horae's own uid and gid (so it can
   read the plugin tree Horae installed), and Horae refuses to start one as root.
5. The plugin installed by `horae plugin add` with its own venv (`plugins/<name>/.venv`,
   decision 69) **built by `/usr/local/bin/python3.12`**, which is what the Horae image does.
   A venv is linked to the interpreter that built it, and the plugin image has that interpreter
   at that path; a venv built elsewhere (a developer's machine, a distribution's
   `/usr/bin/python3.12`) is refused with a message naming its interpreter. The built-in plugins
   (`file-sink`, `fixture-source`) run from Horae's own interpreter and have no venv: keep them
   on `runtime: process`.
6. The plugin's command must run from its venv: `[python, -m, <module>]`, a console script of
   the venv, or a path inside the plugin directory. An absolute command (a host path) is refused.

## What a plugin container is

`docker run --interactive --init` with:

| Setting | Why |
|---|---|
| `--user <horae uid>:<gid>`, never 0 | non-root; reads only what Horae's user can |
| `--read-only` | the image cannot be changed |
| `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=<tmp_mb>m,mode=1777` | its scratch space (`HOME=/tmp/home`, `TMPDIR=/tmp/tmp`, working directory `/tmp/work`), nothing executable there |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | no capability, no setuid escalation |
| Docker's default seccomp profile | never overridden (no `seccomp=unconfined`, no `--privileged`, `--cap-add`, `--device`, host namespace) |
| `--mount type=bind,source=<plugins/name>,target=/plugin,readonly` | the plugin's tree and its venv |
| `--mount type=bind,source=<path>,target=<path>,bind-recursive=disabled[,readonly]` | each **declared path** approved in `horae.lock`, at the same path; read-only unless declared writable ([below](#declared-paths)) |
| `--cpus`, `--memory` = `--memory-swap`, `--pids-limit`, `--ulimit nofile` | its [resource limits](#resource-limits); no swap |
| `--network horae-plugin-<name>` | its own network, see below |
| `--init` | Docker's init is PID 1: children are reaped and signalled with the plugin |
| `--log-driver none` | the daemon keeps no copy of the plugin's output (stderr may echo a secret) |
| `--pull never` | nothing is downloaded at run time |
| `--label dev.horae.plugin=<name>`, `--name horae-<name>-<random>` | to find and remove it |

Not `--rm`: when a plugin dies, Horae asks the engine whether its memory limit killed it
before removing the container itself (`docker rm --force`, always).

The plugin sees neither `horae.yaml`, the lock, the store, `/run/secrets`, other plugins,
Horae's processes and environment, nor any path of the vault it did not declare. `tests/test_plugin_container_live.py` checks all
of this from inside a container (uid, capabilities, `NoNewPrivs`, seccomp mode, read-only
filesystem and mount, `noexec` tmpfs, visible processes) and from `docker inspect`.

### The network

Each plugin gets its own bridge network, `horae-plugin-<name>`, which Horae creates on first use
and checks on each run:

- `--internal`: no route out of the host;
- `com.docker.network.bridge.enable_icc=true`: the plugin can reach its egress proxy (below),
  the only other container allowed there. #49 created these networks with `enable_icc=false`;
  such a network is now refused with "remove it": `docker network rm horae-plugin-<name>`
  and the next run recreates it;
- `com.docker.network.bridge.inhibit_ipv4=true`: **the host has no address on it.** On a plain
  internal network the host's bridge address is the containers' gateway, and every service the
  host exposes on `0.0.0.0` (Horae's own API on a bare-metal install, sshd, a database, a
  container publishing a port) answers there: this was measured on Docker 29 before choosing
  the option, and the live test checks a host listener on `0.0.0.0` is not reachable.

It must also carry the label `dev.horae.network=plugin`, must not be a configuration-only
network or take its configuration from another one, and every container already attached to it
must be labelled `dev.horae.plugin=<name>` (the same plugin in a concurrent run) or
`dev.horae.egress-proxy=<name>` (its own proxy; another plugin's proxy is refused). A network
of that name that does not pass all of this (IPv6 on, another driver, made by hand, a foreign
container attached) is refused, never changed or deleted: `docker network rm
horae-plugin-<name>` and the next run recreates it. Apart from its proxy, a container has **no
network access at all**: no DNS, no Internet, no LAN, not the host.

### Egress: the allow-list proxy

Each time a plugin container starts, Horae first starts **that plugin's own proxy**, in a
container of its own, and starts the plugin only once the proxy listens. The proxy's only
policy is the plugin's lock entry: `permissions.network` (public names) and
`permissions.network_local` (`host:port` on your network), including the grants you added
with `--allow-host` / `--allow-local`. It is the plugin's only way out:

| The plugin asks for | Allowed when |
|---|---|
| `CONNECT name:443` (HTTPS) | `name` is in `network`, and every address it resolves to is public |
| `CONNECT name:port` on another port | never for a `network` host (the lock has no port for them: 443 only) |
| `CONNECT host:port` or plain `http://host:port/…` | `host:port` is exactly a `network_local` entry, and a name resolves only to private addresses (link-local 169.254.x.y only through a literal grant of that address) |
| an IP address | only when it is exactly a `network_local` entry |
| anything else | refused (`403`), and recorded |

The proxy resolves names itself and connects to the address it checked, never to the name
again (no DNS rebinding). It refuses cloud metadata addresses (`169.254.169.254`…) and loopback
whatever the grant, and never decrypts TLS. The plugin finds it in `HTTPS_PROXY`, `HTTP_PROXY`
(and lowercase) and `HORAE_EGRESS_PROXY`, `http://<address>:3128`. Standard clients use these
on their own; a plugin whose HTTP client ignores the environment must pass the proxy itself
(horae-sdk 0.8: `egress_proxy()`), or its connections fail.

**What a refusal looks like.** The plugin gets `403 Forbidden` with a reason code. The run
records an `egress_blocked` violation per attempt, for example `github: egress blocked to
evil.example.net:443 (host_not_allowed)`, and Horae logs it at WARNING. Only the host, the port
and the reason are recorded, never the URL, its query or headers. Reasons are
`host_not_allowed` (not in the lock: add it with `horae plugin add --allow-host` if you trust
it), `port_not_allowed`, `plain_http_not_allowed`, `ip_literal`, `private_address`,
`embedded_address` (an IPv6 answer carrying an IPv4 address), `link_local_address`,
`metadata_address`, `loopback_address`, `not_local_address` (a local name resolved
elsewhere), `malformed_target` (a target it cannot parse, such as `127.1` or user info:
recorded without the host), and `dns_failed` / `no_ipv4_address` (`502`). A server that does
not answer is `502` and not recorded. At most 200 attempts are listed per minute; beyond that
the run records `N more blocked egress attempts`.

**The proxy container.** `docker run --interactive --rm --init` from the same pinned image,
running `python3 -I -S -c <the proxy's source> <policy>`, as `65534:65534` (nobody), with
`--read-only`, `--cap-drop ALL`, `no-new-privileges`, Docker's seccomp profile, `--pids-limit
64`, `--memory 128m`, `--log-driver none`, `--pull never`, no mount, no environment, named
`horae-egress-<plugin>-<random>` and labelled `dev.horae.egress-proxy=<plugin>`. It is attached
to the plugin's network and to **`horae-egress`**, a plain bridge Horae creates on first use
(labelled `dev.horae.network=egress`, `enable_icc=false` so proxies do not reach each other,
no IPv6) and checks on each run like the plugin networks. It also runs with
`--sysctl net.ipv4.ip_forward=0`. The proxy listens only on its address on the plugin's
network, and serves **only its plugin's container**: once the plugin has started, Horae tells
the proxy that container's address, and every other client (a concurrent run of the same
plugin on the same network included) is dropped without an answer. `enable_icc=false` is not
enforced by every engine (OrbStack ignores it), so this client check, not the bridge option,
is what keeps containers off each other's proxies. Its `docker inspect` shows the policy (host
names, not secrets).

**Fail closed.** A proxy that cannot start, has no address on the plugin's network, or cannot
be bound to the plugin's container means the plugin is not started (`runtime_unavailable`). When the plugin stops, its proxy is
stopped and removed with it (`docker rm --force`). If Horae is killed, a leftover proxy exits
when its standard input closes; list them with `docker ps --filter label=dev.horae.egress-proxy`.

**Local services.** The proxy reaches your LAN through the Docker host (`horae-egress` is NATed
like any bridge), so `192.168.1.10:8123` or `homeassistant.local:8123` work when granted. A
grant on **loopback** (`127.0.0.1:1143`, e.g. Proton Mail Bridge on the Docker host) is
refused: loopback is the proxy's own container. Grant the host's LAN address and port instead,
with the service listening there, or keep that plugin on `runtime: process`. A service on the
Docker host itself is reachable at the `horae-egress` gateway address if you grant that
`address:port` (it is private, so it is never reachable without such a grant).

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
ignores end-of-file and signals, or started background processes, is removed with them, and
then sweeps its writable paths ([below](#declared-paths)). If Horae itself is killed with
`SIGKILL`, a container may be left running until its plugin exits on end-of-file, or left
stopped (containers are no longer `--rm`); list such leftovers with `docker ps --all --filter
label=dev.horae.plugin` and remove them with `docker rm` (Horae does not sweep them: it could
remove the container of a concurrent run).

## Declared paths

A plugin that declares `permissions.paths` (a vault to write notes to, files to read) gets
exactly the paths **approved in `horae.lock`** (what you accepted at `horae plugin add`, never
the manifest's own list), each **mounted at the same path** in its container:

- a `read` path is mounted read-only; a `write` path read-write (a path in both is writable);
- nested paths behave as declared: a writable `vault/inbox` under a read-only `vault` is
  writable and the rest of the vault is not; a read-only `vault/archive` under a writable
  `vault` stays read-only;
- a filesystem mounted below a declared path on the host (a USB disk, another bind mount) is
  not carried in (`bind-recursive=disabled`);
- **nothing else** of the host is there: an undeclared path does not exist in the container.

**How the plugin finds them.** Through the configuration it already receives: every request
carries the approved paths in `_meta["dev.horae/paths"]` (`{"read": [...], "write": [...]}`).
Since they are the same paths inside the container, that list, a file path a pipeline passes as
a parameter, and a path the plugin returns all mean the same file on both sides. Nothing is
added to the plugin's environment, and a plugin written for the process runtime works
unchanged.

**What is refused.** Before anything starts (its egress proxy included), every declared path
is checked; if one fails, **the plugin is not started** (`runtime_unavailable`, naming the path
and why). A path must:

1. be what `--mount` can carry (no `,`, `"` or control character);
2. be **its own real path**: no symbolic link anywhere in it (declare the real path: `realpath
   <path>` prints it);
3. exist, and be a directory or a regular file (never a socket, device or FIFO);
4. lie in the instance directory, or in a directory you list in `container.mount_roots` (a vault
   kept elsewhere, e.g. `/srv/vault`);
5. neither be, contain nor lie inside a **sensitive path**: the instance's `horae.yaml`,
   `horae.lock`, `plugins/`, `pipelines/`, `skills/`, `.git/`, state directory and secrets
   directory (`secrets.dir`, `/run/secrets` by default); the Docker socket (the one the CLI uses,
   `/var/run/docker.sock`, `/run/docker.sock`, `/run/containerd`); in Horae's home `.ssh`,
   `.gnupg`, `.docker`, `.aws`, `.azure`, `.kube`, `.config`, `.netrc`, `.git-credentials`,
   `.pypirc`; and the system directories (`/etc`, `/usr`, `/proc`, `/sys`, `/dev`, `/run`,
   `/root`, `/boot`, `/bin`, `/sbin`, `/lib*`, `/var/lib/docker`, ...). So the instance
   directory or your home directory as a whole are refused: declare the vault itself;
6. not be `/tmp` itself (the container's own scratch space).

`horae doctor` does not check these yet: the first run with the plugin reports a refusal.

**What a bind mount cannot do.** Docker's bind mounts take no `nosuid`, `nodev` or `noexec`
option: a declared path keeps the host filesystem's own flags. Inside the container nothing is
gained from that (no capability: no device node can be made; `no-new-privileges`: a set-user-ID
file grants nothing; a hard link cannot cross mounts). But in a **writable** path a plugin can
leave things for the host to trip over: a symbolic link to `~/.ssh/authorized_keys` that Horae
or Obsidian would follow, a FIFO that blocks a reader, a set-user-ID program that runs as your
user. So when the container is gone, Horae **sweeps** each writable path: for what was created
or changed during the plugin's run (never your older files), it removes a symbolic link to a
place the plugin could not write itself, removes a FIFO, socket or device, and clears a
set-user-ID bit, logging each at WARNING. It also warns when a declared path was replaced
(renamed away by a writable parent). The sweep looks at 200 000 entries at most and says so
past that. While the plugin runs, what it wrote is on the host as it wrote it.

**Example.** A plugin declaring `paths: {read: [/srv/vault], write: [/srv/vault/Inbox]}`,
approved in the lock, with the vault outside the instance:

```yaml
container:
  mount_roots: [/srv/vault]
plugins:
  notes-digest:
    runtime: container
```

## Resource limits

Each plugin container runs within limits you set in `horae.yaml`, for all plugins
(`plugin_defaults.limits`) or one (`plugins.<name>.limits`, each field set there wins). The
plugin cannot change them: they are not in its manifest or the lock.

```yaml
plugin_defaults:
  runtime: container
  limits:              # the defaults, written out
    cpus: 1.0          # --cpus: at most one core's worth of CPU time
    memory_mb: 512     # --memory and --memory-swap: 512 MiB, no swap
    pids: 128          # --pids-limit: processes and threads
    tmp_mb: 64         # the /tmp tmpfs, counted in memory_mb
    nofile: 1024       # --ulimit nofile: open files per process
plugins:
  big-parser:
    limits:
      memory_mb: 2048  # the other fields keep plugin_defaults.limits
```

| Field | Default | Bounds | What happens at the limit |
|---|---|---|---|
| `cpus` | 1.0 | 0.1 to 64 (and at most the host's CPU count: the engine refuses more) | the plugin is slowed, never stopped; a call may then hit its `call_timeout_s` |
| `memory_mb` | 512 | 64 to 65 536 | the kernel kills it: the call fails with **`oom_killed`** (`plugin <name> (runtime container) was killed by its memory limit (512 MiB, ...)`), not a generic crash; it may restart once in the run like any failed plugin |
| `pids` | 128 | 16 to 4096 | `fork` and new threads fail (`EAGAIN`); a fork loop stops there |
| `tmp_mb` | 64 | 1 to 1024, less than `memory_mb` | writes to `/tmp` fail (`ENOSPC`) |
| `nofile` | 1024 | 64 to 65 536 | `open` fails (`EMFILE`) |

The defaults suit a **4-core / 16 GB home server**: one plugin never takes more than a core,
and ten plugin containers at their memory limit (five plugins, each started at most twice in a
run) still leave over 10 GB; each egress proxy adds at most 128 MiB (decision 76). A Python
plugin with a thread pool and a few children fits in 128 PIDs. Raise `memory_mb` for a plugin
that parses large files, and lower the defaults on a smaller machine (a Raspberry Pi 4 with
4 GB: `memory_mb: 256`, `cpus: 0.5`).

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
| With the egress proxy (#50), median of 7 | | start and first call 770 ms, stop 344 ms: the proxy adds about 0.45 s per start (its container, one `docker inspect`, before the plugin's) and 0.15 s per stop, same machine |
| Memory of an idle container | | about 9 MiB (`docker stats`), plus the plugin's own |

A call costs the same in both runtimes (about 50 ms of each is the host's stderr quiet window,
`STDERR_QUIET_S`); a container with its egress proxy adds about 0.7 s per plugin start and
0.35 s per stop. A plugin starts at most twice per run (one restart) and is stopped after its
last planned call, so a pipeline using five plugins pays about 5 s more per run and holds at
most ten small containers at a time (each plugin and its proxy). That fits a 4-core / 16 GB home server with a wide margin: the cost is the containers'
start, not their footprint. Not measured on such a server itself; re-run the benchmark there
(the live tests are a quick check: `uv run pytest tests/test_plugin_container_live.py`).

## Limits and follow-ups

- **Declared paths** (decision 78 C): a writable path is the host's own filesystem while the
  plugin runs; the sweep runs only once it stops. Between Horae's check and the engine's
  mount, a process able to write a parent directory could swap a component for a symbolic
  link (the next start's check, and the sweep, report it). Nesting a read-only path under a
  writable one lets the plugin rename the directories in between (the read-only contents move
  along, unchanged; the sweep warns). `horae doctor` does not check paths yet.
- **Podman** is not supported; **the Horae image** cannot start containers (above).
- **Egress limits** (decision 76 I): public hosts on port 443 only; IPv4 only out of
  `horae-egress`; loopback grants are not reachable from a container; a direct connection a
  plugin tries without the proxy fails but is not recorded (only refusals by the proxy are).
- **The tier gate stays**: `horae plugin add` still refuses non-first-party and direct installs
  before M5 (`horae.marketplace.approval`, `DIRECT_INSTALLS`). Lifting it needs #50, #51 and #52,
  and should require `runtime: container` for such plugins.
- `horae doctor` does not check the container prerequisites yet; the first run reports them.
