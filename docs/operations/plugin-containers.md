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

## Installing third-party plugins

A plugin that is not **first-party** (a `community` plugin, a plugin of a marketplace you added
to `marketplaces`, even if that index calls it `first-party`, or a direct install from a git
URL) installs and runs **only with `runtime: container`**
([decision 79](../../schemas/README.md#provisional-decisions-191-pending-maintainer-review)).
First-party plugins of the Horae marketplace may use either runtime.

1. Meet the prerequisites above (Docker, the pulled image, a non-root Horae).
2. Set the plugin's runtime **before** installing it, by its name (`horae plugin add` accepts a
   plugin configured in `horae.yaml` that is not in `horae.lock` yet; listing it also enables
   it):

   ```yaml
   plugins:
     weather:
       runtime: container
   ```

   or `plugin_defaults.runtime: container` for every plugin (then keep the built-in ones on
   `process`, as above).
3. Install it: `horae plugin add weather`, or `horae plugin add
   https://git.example.org/someone/plugin-weather@<full commit SHA>`. The approval prompt names
   the source, the tier and index, and says it runs in a container. `horae.lock` records
   `tier` and `index` (or `source: direct`) for it. Its project is **not built**: only the
   dependencies of its `uv.lock` are installed (hashed wheels), and its sources are imported
   from its tree inside the container. It must start with `[python, -m, <module>]` and
   declare static `[project]` metadata ([writing a plugin](../writing-a-plugin.md)).

Without step 2 the install is refused (`tier_refused`, exit 3) with what to set. A direct
install is refused before anything is fetched when no plugin runs in a container, and once its
`plugin.yaml` names it otherwise.

**At run time** the plugin host reads the tier from `horae.lock` (never from the plugin's
manifest): if `horae.yaml` is later changed so that such a plugin would run as a process, it is
not started (`runtime_refused`), and `horae plugin audit` reports it (`runtime_not_container`,
exit 4). `horae plugin upgrade NAME` applies the same rule to the new version (a plugin whose
index stops calling it `first-party` needs a container, and the change is approved again); a
direct install moves to another commit with `horae plugin upgrade <the same URL>@<new SHA>`,
which must be reachable from the repository's own branches or tags.

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
line, one byte at a time so nothing of the MCP stream is consumed, and runs the plugin as its
child with exactly that environment (it passes `SIGTERM` and the other stop signals on, and
drops its own copy of the environment). The values cross the CLI's connection to the Docker daemon (its local
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
stopped (containers are no longer `--rm`): before each start of a plugin Horae removes its
stopped containers (`docker rm`, never a running one). List leftovers with `docker ps --all
--filter label=dev.horae.plugin`.

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
   kept elsewhere, e.g. `/srv/vault`). `/`, Horae's home directory or one of its parents
   are refused as `mount_roots`: list the vault itself;
5. neither be, contain nor lie inside a **sensitive path**: the instance's `horae.yaml`,
   `horae.lock`, `plugins/`, `pipelines/`, `skills/`, `.git/`, state directory and secrets
   directory (`secrets.dir`, `/run/secrets` by default); the Docker socket (the one the CLI uses,
   `/var/run/docker.sock`, `/run/docker.sock`, `/run/containerd`); in Horae's home `.ssh`,
   `.gnupg`, `.docker`, `.aws`, `.azure`, `.kube`, `.config`, `.local`, `.netrc`,
   `.git-credentials`, `.pypirc`, the shell start-up files (`.bashrc`, `.bash_profile`,
   `.profile`, `.zshrc`, `.zprofile`), `Library/LaunchAgents` and `Library/Keychains`; macOS's
   `/System`, `/Library` and `/Applications`; and the system directories (`/etc`, `/usr`, `/proc`, `/sys`, `/dev`, `/run`,
   `/root`, `/boot`, `/bin`, `/sbin`, `/lib*`, `/var/lib/docker`, ...). So the instance
   directory or your home directory as a whole are refused: declare the vault itself;
6. not be `/tmp` itself (the container's own scratch space);
7. not lie strictly inside a writable path of **another plugin that runs in a container**
   (it could swap a directory of yours for a link while yours starts); and a **writable**
   path must not be or hold another container plugin's writable path (two container plugins
   never write the same tree). Reading the same path, or a path holding the other's writable
   one, is fine: that is how plugins share a vault. Plugins on `runtime: process` are not
   counted (they are unconfined anyway). These comparisons also use the files' identities,
   so another letter case (macOS volumes ignore case) or a link to the same directory is
   recognised;
8. if writable, not be or hold a git repository: no `.git` (in any letter case) and no git
   directory layout under any name (a directory holding a `HEAD` with any of `objects`,
   `refs`, `commondir`, `gitdir`, `config`, `hooks`, `index`, `info`, `packed-refs`,
   `worktrees`) in the path or anywhere below it, and no git directory layout above it. A
   plugin could plant hooks that your `git` would run. **A git work tree around it is fine**
   as long as that repository's git directory stays out of the plugin's reach: the usual
   `horae init` layout, an instance kept in git with `vault/` gitignored, mounts its vault
   writable, because the repository's `.git` is the instance's, outside every path the plugin
   is given. If that git directory (the `.git` above, the directory a `.git` file points to,
   or a worktree's common directory) lies inside any path the plugin is given, read-only or
   writable, or cannot be found, the plugin is refused. A `.git` the plugin creates during
   its run quarantines it, and so does any git directory layout found in a writable path.
   Also set `git config --global safe.bareRepository explicit` for the user who runs `git` on
   the host, so git never uses a bare repository it merely finds in a directory.

The paths are checked again right before `docker run`, and once more after the container has
started and before its egress proxy lets it out: `docker inspect` must show exactly the
checked mounts, and each path must still be the same file. Otherwise the plugin is stopped.

`horae doctor` does not check these yet: the first run with the plugin reports a refusal.

**What a bind mount cannot do.** Docker's bind mounts take no `nosuid`, `nodev` or `noexec`
option: a declared path keeps the host filesystem's own flags. Inside the container nothing is
gained from that (no capability: no device node can be made; `no-new-privileges`: a set-user-ID
file grants nothing; a hard link cannot cross mounts). But in a **writable** path a plugin can
leave things for the host to trip over: a symbolic link to `~/.ssh/authorized_keys` that Horae
or Obsidian would follow, a FIFO that blocks a reader, a set-user-ID program that runs as your
user. So when the container is gone, Horae **sweeps** each writable path: for what was created
or changed during the plugin's run (never your older files), it removes a symbolic link whose
target is absolute or has a `..`, or that resolves (following every link, as the kernel does)
to a place the plugin could not write itself, removes a FIFO, socket or device, and clears a
set-user-ID bit, logging each at WARNING. It also warns when a declared path was replaced
(renamed away by a writable parent).

**One run at a time.** A plugin with a writable path holds a lock
(`<state_dir>/quarantine/<plugin>.lock`) from its start until its sweep is done: a second run
of the same plugin at the same time is refused (`runtime_unavailable`), so nothing in a
container writes there while the sweep works. The sweep itself never follows a link: it works
through open directory descriptors and refuses anything that changed under it.

**Quarantine.** The sweep fails closed. If it cannot finish its check (a directory it cannot
list, an entry on another device that is not a mount point, more than 100 000 new or
2 000 000 entries in all, a declared path replaced, a container that could not be removed),
Horae writes `<state_dir>/quarantine/<plugin>` with the reasons and **refuses to start that
plugin again** (`runtime_unavailable`, naming the file). The same happens when a run ended
before its sweep (Horae killed): `<state_dir>/quarantine/<plugin>.pending`, written before
each start and removed after a clean sweep, is still there. Look at the plugin's writable
paths, then remove the file to let it run again. While the plugin runs, what it wrote is on the host as it
wrote it.

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
| `memory_mb` | 512 | 64 to 65 536 | the kernel kills it: the call fails with **`oom_killed`** (`plugin <name> (runtime container) was killed by its memory limit (512 MiB, ...)`), not a generic crash; it may restart once in the run like any failed plugin. The launcher reads the container's cgroup `memory.events` (`oom_kill`) when the plugin dies of `SIGKILL` and exits with status 247; the engine's `OOMKilled` is a fallback only, since the engine can lose that event (#188) |
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
  plugin runs; the sweep runs only once it stops. A process that is not a container plugin
  (a process-runtime plugin, the same plugin in a concurrent run, you) and can write a parent
  directory could swap a component for a symbolic link and back in the instant between
  Horae's last check and the engine's mount; `docker inspect` shows paths, not files, so that
  is not seen. Nesting a read-only path under a
  writable one lets the plugin rename the directories in between (the read-only contents move
  along, unchanged; the sweep warns). `horae doctor` does not check paths yet.
- **Podman** is not supported; **the Horae image** cannot start containers (above).
- **Egress limits** (decision 76 I): public hosts on port 443 only; IPv4 only out of
  `horae-egress`; loopback grants are not reachable from a container; a direct connection a
  plugin tries without the proxy fails but is not recorded (only refusals by the proxy are).
- **Non-first-party plugins** install and run only in a container (#191, above); `horae doctor`
  reports one configured for the process runtime (#194), as `horae plugin audit` does
  (`runtime_not_container`).
- `horae doctor` checks that a local Docker engine is usable when a plugin runs in a container
  (the CLI, a local `unix://` endpoint, a daemon that answers; nothing is started or pulled, and
  the image is not checked). It does not check the image, the mounts or the egress network: the
  first run reports them.
