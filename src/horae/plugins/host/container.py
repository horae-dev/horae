# SPDX-License-Identifier: Apache-2.0
"""The container runtime: one Docker container per plugin, MCP over its stdio (#49).

Schemas decision 74 (provisional) is the contract; `docs/operations/plugin-containers.md` the
operator guide. A plugin whose effective `runtime` (`horae.yaml`: `plugins.<name>.runtime`, else
`plugin_defaults.runtime`) is `container` is started by `ContainerRuntime.spawn`, which returns
the same `Spawn` the process runtime builds, only for the Docker CLI:

- `docker run --interactive --init` (`run_argv`), as Horae's own non-root uid and gid, with
  a read-only root filesystem, every capability dropped, `no-new-privileges`, Docker's default
  seccomp profile (never overridden), a `noexec` tmpfs on `/tmp`, no log kept by the daemon,
  nothing pulled, the plugin's directory (its tree and its `.venv`) bind-mounted read-only at
  `/plugin`, and **only its declared paths** besides (`horae.plugins.host.mounts`, #51,
  decision 78): each locked `permissions.paths` entry at the same path, read-only unless
  declared writable, after checks that refuse the plugin when one cannot be mounted safely;
- within its CPU, memory (no swap), PID, `/tmp` and open-file limits (`plugin_defaults.limits`,
  `plugins.<name>.limits`); a plugin the memory limit kills fails its call with `oom_killed`
  (the launcher's `OOM_EXIT`, from the cgroup's `memory.events`, or `docker inspect`'s
  `.State.OOMKilled`), so the container is not `--rm`: it is removed once inspected;
- on the plugin's own network, `horae-plugin-<name>` (`network_name`): `--internal` and no host
  address on the bridge (`inhibit_ipv4`), so the plugin reaches nothing, the host's services
  included, but **its own egress proxy** (`horae.plugins.host.egress`, #50, decision 76), the
  only other container allowed there; checked on each run's first start of the plugin, and
  refused (never repaired) when it is not exactly that;
- egress only through that proxy: one per plugin start, with the plugin's locked network
  permissions as its policy, on the plugin's network and `horae-egress`; the plugin gets its
  address in `HTTPS_PROXY` / `HTTP_PROXY` / `HORAE_EGRESS_PROXY`. A proxy that does not start
  means the plugin does not either;
- **secrets** never on a command line nor in the container's configuration: the plugin's
  environment (`horae.plugins.host.env.build_env`, with the container's paths) is the first line
  of the container's stdin (`preamble`), which `LAUNCHER`, run by the image's interpreter, reads
  byte by byte before it runs the plugin command (its child) with exactly that environment;
- the command runs from the plugin's venv only (`container_command`), and the venv must be one
  the image's interpreter can run (`check_venv`);
- once the CLI is gone, `docker rm --force` removes the container whatever the plugin did; then
  `mounts.sweep` undoes what it left in its writable paths that the host could trip over.

**Fail closed**: whatever is missing (the CLI, the daemon, the image, the network, the venv) is a
`runtime_unavailable` error; the host never falls back to the process runtime.
"""

import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio

from horae.contracts.instance import DEFAULT_TMP_MB, ContainerConfig, ContainerLimits
from horae.plugins.host import egress, mounts
from horae.plugins.host.egress_proxy import LISTEN_PORT
from horae.plugins.host.env import BASE_PATH, VENV_DIR, build_env
from horae.plugins.host.errors import HostErrorKind, PluginCallError
from horae.plugins.host.mounts import UNSAFE_MOUNT, Mount, MountPolicy
from horae.plugins.host.session import Spawn
from horae.secrets import Secret

PLUGIN_MOUNT = "/plugin"
"""Where the plugin's directory is mounted, read-only."""
IMAGE_PYTHON = "/usr/local/bin/python3"
"""The image's interpreter, which runs `LAUNCHER`."""
IMAGE_PYTHON_HOME = "/usr/local/bin"
"""The `home` a plugin venv's `pyvenv.cfg` must name (the image's interpreter directory)."""
IMAGE_PYTHON_VERSION = "3.12"
"""The Python minor version of the image (and of the Horae image)."""
# Paths inside the container, on its own tmpfs (not the host's /tmp).
CONTAINER_TMP = "/tmp"  # noqa: S108
CONTAINER_HOME = f"{CONTAINER_TMP}/home"
CONTAINER_TMPDIR = f"{CONTAINER_TMP}/tmp"  # noqa: S108
CONTAINER_WORKDIR = f"{CONTAINER_TMP}/work"


def tmpfs_option(size_mb: int) -> str:
    """`--tmpfs`'s value: the container's own scratch space, `size_mb` MiB."""
    return f"{CONTAINER_TMP}:rw,noexec,nosuid,nodev,size={size_mb}m,mode=1777"


TMPFS = tmpfs_option(DEFAULT_TMP_MB)
"""The container's scratch space with the default size (its only writable place but the
declared writable paths)."""
NETWORK_PREFIX = "horae-plugin-"
PLUGIN_LABEL = "dev.horae.plugin"
"""Label of every plugin container (value: the plugin name)."""
NETWORK_LABEL = "dev.horae.network"
"""Label of every plugin network (value `plugin`); a network without it is refused."""
EGRESS_LABEL = "dev.horae.egress-proxy"
"""Label of an egress proxy (value: the plugin name): it may join that plugin's network only."""
NETWORK_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.inhibit_ipv4": "true",
}
"""Driver options a plugin network must have (decision 74 D, as amended by decision 76 C:
inter-container traffic is on, so the plugin reaches its proxy; only its own containers and its
own proxy may be attached)."""
ENGINE_TIMEOUT_S = 10.0
"""Each Docker CLI call but `run` (inspect, create, rm) may take this long."""
OOM_POLL_S = 1.0
"""How long a `SIGKILL` exit (137) with `OOMKilled` still false is re-inspected (#188)."""
OOM_POLL_STEP_S = 0.05
SIGKILLED = 137
OOM_EXIT = 247
"""The launcher's exit status for a plugin its cgroup's memory limit killed (#188)."""
CGROUP_ROOT = "/sys/fs/cgroup"
PROC_CGROUP = "/proc/self/cgroup"
MAX_PREAMBLE = 1024 * 1024
"""The longest environment line the launcher reads."""
ENGINE_ENV_KEYS = (
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
)
"""What the Docker CLI inherits from Horae's environment: how to reach the engine, nothing else."""
_MAX_CFG_BYTES = 64 * 1024
_MAX_ENGINE_OUTPUT = 64 * 1024
_SHOWN = 300
_log = logging.getLogger("horae.plugins.host.container")


def launcher_source(
    home: str = CONTAINER_HOME,
    tmpdir: str = CONTAINER_TMPDIR,
    workdir: str = CONTAINER_WORKDIR,
    cgroup_root: str = CGROUP_ROOT,
    proc_cgroup: str = PROC_CGROUP,
) -> str:
    """The launcher's source, creating `home`, `tmpdir` and `workdir` (tests use other paths,
    and another cgroup tree: `cgroup_root`, `proc_cgroup`).

    It runs the plugin as its child (not `execve`), so that it is still there, in the
    container's cgroup, when the plugin dies: a plugin killed by `SIGKILL` while the cgroup's
    `memory.events` `oom_kill` count went up makes it exit `OOM_EXIT` (#188). Any other end is
    passed on as the plugin's own status (`128 + signal` for a signal), but an exit with
    `OOM_EXIT` itself, which becomes 1: only the launcher reports a memory kill. It makes
    itself non-dumpable first (`prctl(PR_SET_DUMPABLE, 0)`), so that the plugin, its child with
    the same uid, can neither ptrace it (and forge that status) nor read its memory; the
    plugin's own `execve` makes it dumpable again, as before."""
    return f"""\
import json, os, signal, sys
try:  # PR_SET_DUMPABLE 0: the plugin (same uid) cannot ptrace or read this process (#193)
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)
except (ImportError, OSError, AttributeError):
    pass  # best effort: a start never fails for it (its execve makes the plugin dumpable)
def fail(message):
    os.write(2, b"horae-launch: " + message.encode() + b"\\n")
    os._exit(70)
def oom_kills():
    try:
        with open({proc_cgroup!r}, "rb") as handle:
            lines = handle.read(65536).decode("utf-8", "replace").splitlines()
        group = next(line[3:] for line in lines if line.startswith("0::"))
        path = {cgroup_root!r} + group.rstrip("/") + "/memory.events"
        with open(path, "rb") as handle:
            for line in handle.read(65536).decode("ascii", "replace").splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
    except (OSError, StopIteration, ValueError):
        pass
    return None
line = bytearray()
while not line.endswith(b"\\n"):
    if len(line) > {MAX_PREAMBLE}:
        fail("the environment line is too long")
    byte = os.read(0, 1)
    if not byte:
        fail("stdin closed before the environment line")
    line += byte
try:
    env = json.loads(line)
except ValueError:
    fail("the environment line is not JSON")
line[:] = bytes(len(line))
if not isinstance(env, dict) or not all(
    isinstance(k, str) and isinstance(v, str) for k, v in env.items()
):
    fail("the environment line is not an object of strings")
if len(sys.argv) < 2:
    fail("no command")
os.umask(0o077)
for path in ({home!r}, {tmpdir!r}, {workdir!r}):
    os.makedirs(path, mode=0o700, exist_ok=True)
os.chdir({workdir!r})
before = oom_kills()
forwarded = (
    signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2
)
blocked = signal.pthread_sigmask(signal.SIG_BLOCK, forwarded)
try:
    child = os.fork()
except OSError as error:
    fail("cannot fork: " + str(error.strerror))
if child == 0:
    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    try:
        os.execve(sys.argv[1], sys.argv[1:], env)
    except OSError as error:
        fail("cannot run " + sys.argv[1] + ": " + str(error.strerror))
env.clear()
def forward(number, frame):
    try:
        os.kill(child, number)
    except OSError:
        pass
for number in forwarded:
    signal.signal(number, forward)
signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
null = os.open("/dev/null", os.O_RDWR)
os.dup2(null, 0)
os.dup2(null, 1)
_, status = os.waitpid(child, 0)
if os.WIFSIGNALED(status):
    number = os.WTERMSIG(status)
    after = oom_kills() if number == signal.SIGKILL else None
    if before is not None and after is not None and after > before:
        os._exit({OOM_EXIT})
    os._exit(128 + number)
code = os.WEXITSTATUS(status)
os._exit(1 if code == {OOM_EXIT} else code)
"""


LAUNCHER = launcher_source()
"""Run as `python3 -I -S -c LAUNCHER <command>` in the container (decision 74 E). Its source is
visible in `docker inspect`; it holds no secret (it drops its copy of the environment once the
plugin runs)."""


def _unavailable(plugin: str, message: str) -> PluginCallError:
    return PluginCallError("runtime_unavailable", f"plugin {plugin} (runtime container): {message}")


def network_name(plugin: str) -> str:
    """The plugin's own network."""
    return NETWORK_PREFIX + plugin


def check_venv(plugin: str, root: Path) -> Path:
    """`<root>/.venv`, if the image's interpreter can run it; else `runtime_unavailable`.

    The venv must be a real directory whose `pyvenv.cfg` names `home = /usr/local/bin` and a
    3.12 version, without the system site-packages, and which has a `bin/python` (a symlink to
    the interpreter, which need not exist on the host)."""
    venv = root / VENV_DIR
    try:
        if not stat.S_ISDIR(os.lstat(venv).st_mode):
            raise _unavailable(plugin, f"{VENV_DIR} is not a directory")
        cfg = venv / "pyvenv.cfg"
        status = os.lstat(cfg)
        if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_CFG_BYTES:
            raise _unavailable(plugin, f"{VENV_DIR}/pyvenv.cfg is not a small regular file")
        text = cfg.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _unavailable(
            plugin, "it has no venv of its own (a built-in plugin runs with `runtime: process`)"
        ) from None
    except (OSError, UnicodeError):
        raise _unavailable(plugin, f"{VENV_DIR}/pyvenv.cfg cannot be read") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    home = values.get("home")
    version = values.get("version_info") or values.get("version") or ""
    if home != IMAGE_PYTHON_HOME or not version.startswith(IMAGE_PYTHON_VERSION + "."):
        raise _unavailable(
            plugin,
            f"its venv was built by Python {version or '?'} in {home or '?'}, the image runs "
            f"{IMAGE_PYTHON_VERSION} in {IMAGE_PYTHON_HOME} (install it with the Horae image)",
        )
    if values.get("include-system-site-packages", "false").lower() != "false":
        raise _unavailable(plugin, "its venv includes the system site-packages")
    if not os.path.lexists(venv / "bin" / "python"):
        raise _unavailable(plugin, f"{VENV_DIR}/bin/python is missing")
    return venv


def container_command(plugin: str, command: list[str], root: Path) -> list[str]:
    """`command` as the container runs it (decision 74 F): a bare `argv[0]` from the plugin's
    venv (`/plugin/.venv/bin/<name>`), a relative path inside the plugin directory
    (`/plugin/<path>`); an absolute path (a host path) is refused."""
    first = command[0]
    if os.path.isabs(first):
        raise _unavailable(plugin, f"command {first!r} is a host path")
    real_root = root.resolve()
    if "/" in first:
        real = (real_root / first).resolve()
        if not real.is_relative_to(real_root) or not real.is_file():
            raise _unavailable(plugin, f"command {first!r} is not a file inside the plugin")
        inside = f"{PLUGIN_MOUNT}/{real.relative_to(real_root).as_posix()}"
    else:
        if first in (".", "..") or not os.path.lexists(real_root / VENV_DIR / "bin" / first):
            raise _unavailable(plugin, f"command {first!r} is not in the plugin's venv")
        inside = f"{PLUGIN_MOUNT}/{VENV_DIR}/bin/{first}"
    return [inside, *command[1:]]


def mount_source(plugin: str, root: Path) -> str:
    """The plugin directory's real path, as `--mount` can carry it (no `,`, `"` or control
    character: its value is CSV)."""
    real = root.resolve()
    text = str(real)
    if not real.is_dir() or UNSAFE_MOUNT.search(text):
        raise _unavailable(
            plugin,
            'its directory cannot be mounted (not a directory, or a path holding a "," or a quote)',
        )
    return text


def engine_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The Docker CLI's environment: `ENGINE_ENV_KEYS` from Horae's, the base `PATH`."""
    source = os.environ if environ is None else environ
    env = {key: source[key] for key in ENGINE_ENV_KEYS if key in source}
    env["PATH"] = BASE_PATH
    env["LANG"] = "C.UTF-8"
    return env


def engine_problem(config: ContainerConfig, environ: Mapping[str, str] | None = None) -> str | None:
    """Why no usable local Docker engine is available, or `None` (`horae doctor`, #194).

    The same conditions the runtime enforces before it starts anything: the Docker CLI is
    installed, `DOCKER_HOST` and the CLI's current context are a local `unix://` socket
    (decision 74 B), and the daemon answers. Starts nothing, pulls nothing; the image is not
    checked here."""
    env = engine_env(environ)
    docker = config.docker or shutil.which("docker", path=BASE_PATH)
    if docker is None or not os.path.isfile(docker) or not os.access(docker, os.X_OK):
        return "the Docker CLI is not installed"
    host = env.get("DOCKER_HOST")
    if host is not None and not is_local_endpoint(host):
        return f"DOCKER_HOST is not a local unix socket ({_scheme(host)})"

    def run(*args: str) -> tuple[int, str]:
        try:
            done = subprocess.run(  # noqa: S603 (the Docker CLI, fixed arguments)
                [docker, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=env,
                check=False,
                timeout=ENGINE_TIMEOUT_S,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return -1, "the container engine did not answer in time"
        except OSError as error:
            return -1, error.strerror or "cannot run the Docker CLI"
        out = done.stdout if done.returncode == 0 else done.stderr
        return done.returncode, _first_line(out[:_MAX_ENGINE_OUTPUT])

    code, out = run("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if code != 0:
        return f"the Docker context cannot be inspected: {out}"
    if not is_local_endpoint(out):
        return f"the Docker engine is not a local unix socket ({_scheme(out)})"
    code, out = run("version", "--format", "{{.Server.Version}}")
    if code != 0:
        return f"the Docker engine is unreachable: {out}"
    return None


def limit_options(limits: ContainerLimits) -> list[str]:
    """The resource limits' flags (decision 78 D): `--memory-swap` equal to `--memory`, so the
    container has no swap."""
    return [
        f"--cpus={limits.cpus:g}",
        f"--memory={limits.memory_mb}m",
        f"--memory-swap={limits.memory_mb}m",
        f"--pids-limit={limits.pids}",
        f"--ulimit=nofile={limits.nofile}:{limits.nofile}",
    ]


def run_argv(
    docker: str,
    *,
    plugin: str,
    name: str,
    image: str,
    user: str,
    source: str,
    command: list[str],
    mounts: Sequence[Mount] = (),
    limits: ContainerLimits | None = None,
) -> list[str]:
    """The `docker run` command line of decisions 74 C and 78 (no secret: they go in the
    preamble). Not `--rm`: the container is inspected (`OOMKilled`) before it is removed."""
    limits = limits or ContainerLimits()
    declared = [arg for mount in mounts for arg in ("--mount", mount.option())]
    return [
        docker,
        "run",
        "--interactive",
        "--init",
        "--name",
        name,
        "--label",
        f"{PLUGIN_LABEL}={plugin}",
        "--pull",
        "never",
        "--log-driver",
        "none",
        "--user",
        user,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *limit_options(limits),
        "--tmpfs",
        tmpfs_option(limits.tmp_mb),
        "--network",
        network_name(plugin),
        "--mount",
        f"type=bind,source={source},target={PLUGIN_MOUNT},readonly",
        *declared,
        "--workdir",
        CONTAINER_TMP,
        "--entrypoint",
        IMAGE_PYTHON,
        image,
        "-I",
        "-S",
        "-c",
        LAUNCHER,
        *command,
    ]


def preamble(env: Mapping[str, str]) -> bytearray:
    """The launcher's environment line: one JSON object (ASCII, so no raw newline), then `\\n`."""
    line = bytearray(json.dumps(dict(env), ensure_ascii=True, separators=(",", ":")), "ascii")
    line += b"\n"
    if len(line) > MAX_PREAMBLE:
        line[:] = bytes(len(line))
        raise PluginCallError("env_unsafe", "the plugin's environment is too large")
    return line


def _first_line(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip().splitlines()
    line = text[0] if text else "no message"
    return re.sub(r"[\x00-\x1f\x7f]", " ", line)[:_SHOWN]


class ContainerRuntime:
    """Starts plugins in containers for one `PluginHost` (one run): the engine and image are
    checked once, each plugin's network once (see the module docstring)."""

    def __init__(
        self,
        config: ContainerConfig | None = None,
        *,
        mounts: MountPolicy | None = None,
        uid: int | None = None,
        gid: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or ContainerConfig()
        self.mounts = mounts
        """Where declared paths may come from (decision 78 B); `None`: none may be mounted."""
        self._planned: dict[str, tuple[Mount, ...]] = {}
        """Each running container's checked mounts, by container name."""
        self._quarantined: set[str] = set()
        """Plugins quarantined in this run (also on disk when the policy has a directory)."""
        self._socket: str | None = None
        """The engine's socket path, sensitive like the policy's paths."""
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._env = engine_env(environ)
        self._lock = anyio.Lock()
        self._docker: str | None = None
        self._networks: dict[str, str] = {}
        """Each checked plugin network's IPv4 subnet."""
        self._egress_checked = False

    async def spawn(
        self,
        plugin: str,
        *,
        root: Path | None,
        command: list[str],
        secrets: Mapping[str, Secret],
        timezone: str,
        cwd: Path,
        startup_timeout_s: float,
        network: Sequence[str] = (),
        network_local: Sequence[str] = (),
        paths_read: Sequence[str] = (),
        paths_write: Sequence[str] = (),
        limits: ContainerLimits | None = None,
        shared: Sequence[tuple[str, str]] = (),
    ) -> Spawn:
        """The `Spawn` of `plugin`'s container, its egress proxy started with the locked
        `network` and `network_local` as policy, the locked `paths_read` and `paths_write`
        mounted (decision 78) within `limits`; `runtime_unavailable` if either cannot run or a
        path cannot be mounted safely (`env_unsafe` for a denied secret name, as in the process
        runtime)."""
        limits = limits or ContainerLimits()
        if self._uid == 0:
            raise _unavailable(plugin, "Horae runs as root; plugin containers never do")
        if plugin in self._quarantined:
            raise _unavailable(plugin, "it is quarantined: its writable paths could not be checked")
        mounts.check_quarantine(plugin, self.mounts)
        if root is None:
            raise _unavailable(plugin, "it has no installed directory")
        venv = check_venv(plugin, root)
        inside = container_command(plugin, command, root)
        source = mount_source(plugin, root)
        docker = await self._engine(plugin)
        policy = self.mounts
        if policy is not None and self._socket is not None:
            policy = policy.with_sensitive(self._socket)
        declared = mounts.plan_mounts(plugin, paths_read, paths_write, policy, shared)
        writes = any(mount.writable for mount in declared)
        lock = mounts.lock_plugin(plugin, self.mounts) if writes else None
        try:
            if writes:  # again, now that no other run of it can quarantine it meanwhile
                if plugin in self._quarantined:
                    raise _unavailable(
                        plugin, "it is quarantined: its writable paths could not be checked"
                    )
                mounts.check_quarantine(plugin, self.mounts)
                mounts.check_pending(plugin, self.mounts)
            return await self._prepare(
                plugin,
                docker,
                root,
                venv,
                inside,
                source,
                secrets,
                timezone,
                cwd,
                startup_timeout_s,
                network,
                network_local,
                limits,
                policy,
                declared,
                lock,
            )
        except BaseException:
            mounts.unlock_plugin(lock)
            raise

    async def _prepare(
        self,
        plugin: str,
        docker: str,
        root: Path,
        venv: Path,
        inside: list[str],
        source: str,
        secrets: Mapping[str, Secret],
        timezone: str,
        cwd: Path,
        startup_timeout_s: float,
        network: Sequence[str],
        network_local: Sequence[str],
        limits: ContainerLimits,
        policy: MountPolicy | None,
        declared: tuple[Mount, ...],
        lock: int | None,
    ) -> Spawn:
        """The rest of `spawn`, once the mounts are planned and the run lock (if any) held."""
        await self._network(plugin, docker)
        await self._egress_network(plugin, docker)
        proxy = await self._start_proxy(plugin, docker, network, network_local)
        try:
            env = build_env(
                plugin,
                home=Path(CONTAINER_HOME),
                tmpdir=Path(CONTAINER_TMPDIR),
                timezone=timezone,
                secrets=secrets,
                venv=Path(PLUGIN_MOUNT) / venv.name,
                proxy=proxy.url,
            )
            line = preamble(env)
            env.clear()
        except BaseException:
            await proxy.stop()
            raise
        name = f"horae-{plugin}-{secrets_token()}"
        argv = run_argv(
            docker,
            plugin=plugin,
            name=name,
            image=self.config.image,
            user=f"{self._uid}:{self._gid}",
            source=source,
            command=inside,
            mounts=declared,
            limits=limits,
        )

        proxy.client_container = name
        try:
            await self._remove_exited(docker, plugin)
            mounts.recheck(plugin, declared, policy)  # right before the engine mounts them
            if lock is not None:
                mounts.mark_pending(plugin, self.mounts)  # gone only after a clean sweep
        except BaseException:
            await proxy.stop()
            raise
        self._planned[name] = declared
        started = mounts.now()

        async def remove() -> None:
            try:
                removed, _, _ = await self._run(docker, "rm", "--force", name)
            finally:
                self._planned.pop(name, None)
                await proxy.stop()
            if lock is None:
                return
            try:
                await self._sweep(plugin, declared, started, removed == 0)
            finally:
                mounts.unlock_plugin(lock)

        async def diagnose() -> tuple[HostErrorKind, str] | None:
            return await self._oom_killed(docker, plugin, name, limits)

        return Spawn(
            plugin,
            tuple(argv),
            dict(self._env),
            cwd,
            startup_timeout_s,
            preamble=line,
            cleanup=remove,
            egress=proxy,
            diagnose=diagnose,
        )

    async def _sweep(
        self, plugin: str, declared: tuple[Mount, ...], started: float, removed: bool
    ) -> None:
        """Sweep `plugin`'s writable paths, its container removed (decision 78 C). The plugin
        is gone and its run lock keeps every other run of it away; another container plugin
        never writes the same tree (`plan_mounts`). Writers outside containers (a process
        plugin, the user) can still race it: the sweep works through directory descriptors
        and fails on any change it sees. A failure quarantines the plugin; the pending marker
        goes only after a clean sweep or a written quarantine."""
        if not removed:
            failures = ["its container could not be removed, so its paths were not swept"]
        else:
            swept = await anyio.to_thread.run_sync(mounts.sweep, plugin, declared, started)
            failures = swept.failures
        if not failures:
            mounts.clear_pending(plugin, self.mounts)
            return
        self._quarantined.add(plugin)  # fail closed for the rest of this run in any case
        if mounts.quarantine(plugin, self.mounts, failures):
            mounts.clear_pending(plugin, self.mounts)
        else:  # the pending marker stays: the next run refuses it too
            _log.error("plugin %s: its quarantine file could not be written", plugin)

    async def _remove_exited(self, docker: str, plugin: str) -> None:
        """Remove `plugin`'s stopped containers left by an earlier, killed Horae (decision 78
        E): exited or dead ones only, never a running one, without `--force`."""
        found: list[str] = []
        for status in ("exited", "dead"):
            code, out, _ = await self._run(
                docker,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={PLUGIN_LABEL}={plugin}",
                "--filter",
                f"status={status}",
            )
            if code == 0:
                found += [line for line in out.decode("ascii", "replace").split() if line.isalnum()]
        if found:
            await self._run(docker, "rm", *found)

    async def _oom_killed(
        self, docker: str, plugin: str, name: str, limits: ContainerLimits
    ) -> tuple[HostErrorKind, str] | None:
        """`oom_killed` if the kernel killed the plugin in `name` for its memory limit (decision
        78 E), else `None` (the plugin's failure stays what it was).

        Two sources, either is enough (#188). First, the launcher's exit status `OOM_EXIT`: it
        outlives the plugin in the container's cgroup and reads that cgroup's `memory.events`
        (`oom_kill`) once the plugin died of `SIGKILL`. Second, the engine's `.State.OOMKilled`,
        for when the launcher cannot read it (cgroup v1, or the launcher itself killed): the
        engine sets it from an OOM event its runtime can lose when the cgroup is removed before
        the event is read (systemd's cgroup driver), or deliver after the exit, so a `SIGKILL`
        exit (137) with the flag still unset is asked again for `OOM_POLL_S` at most. Other
        exits are asked once. The caller never asks for a container Horae stopped itself (a
        timeout or a stop kills with `SIGKILL` too)."""
        deadline = anyio.current_time() + OOM_POLL_S
        while True:
            code, out, _ = await self._run(
                docker,
                "container",
                "inspect",
                "--format",
                "{{json .State.OOMKilled}} {{json .State.ExitCode}}",
                name,
            )
            flag, _, exit_code = out.decode("ascii", "replace").strip().partition(" ")
            if code != 0 or flag not in ("true", "false"):
                return None
            if flag == "true" or exit_code == str(OOM_EXIT):
                break
            if exit_code != str(SIGKILLED) or anyio.current_time() >= deadline:
                return None
            await anyio.sleep(OOM_POLL_STEP_S)
        return (
            "oom_killed",
            f"plugin {plugin} (runtime container) was killed by its memory limit "
            f"({limits.memory_mb} MiB, `plugins.{plugin}.limits.memory_mb` in horae.yaml)",
        )

    async def admit(self, plugin: str, proxy: egress.ProxyHandle) -> None:
        """Once the plugin's container runs, make it the only client its proxy serves
        (decision 76 C): its address on the plugin network, sent to the proxy, confirmed.
        `runtime_unavailable` otherwise (the caller stops the plugin). First, its mounts must be
        exactly the planned ones, still the files that were checked (decision 78 B): it has
        had no egress so far."""
        docker = self._docker
        address = None
        planned = self._planned.get(proxy.client_container)
        if planned:
            await self._verify_mounts(plugin, proxy.client_container, planned)
        if docker is not None:
            code, out, _ = await self._run(
                docker,
                "container",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                proxy.client_container,
            )
            try:
                networks = json.loads(out) if code == 0 else None
            except ValueError:
                networks = None
            address = egress.plugin_address(networks, network_name(plugin))
        if address is None or not await proxy.admit(address):
            raise _unavailable(plugin, "its egress proxy could not be bound to its container")

    async def _verify_mounts(self, plugin: str, name: str, planned: Sequence[Mount]) -> None:
        """`runtime_unavailable` unless the started container `name` has exactly the planned
        mounts and each path is still the checked file (`mounts.recheck`)."""
        problem: str | None = "the engine could not be asked"
        if self._docker is not None:
            code, out, _ = await self._run(
                self._docker, "container", "inspect", "--format", "{{json .Mounts}}", name
            )
            try:
                inspected = json.loads(out) if code == 0 else None
            except ValueError:
                inspected = None
            problem = mounts.mounts_problem(planned, inspected, PLUGIN_MOUNT)
        if problem is not None:
            raise _unavailable(plugin, f"its mounts are not as checked: {problem}")
        policy = self.mounts
        if policy is not None and self._socket is not None:
            policy = policy.with_sensitive(self._socket)
        mounts.recheck(plugin, planned, policy)

    async def _start_proxy(
        self, plugin: str, docker: str, network: Sequence[str], network_local: Sequence[str]
    ) -> egress.ProxyHandle:
        """Start `plugin`'s egress proxy and wait until it listens (decision 76 B, H); else
        remove it and raise `runtime_unavailable`."""
        name = f"horae-egress-{plugin}-{secrets_token()}"
        argv = egress.proxy_argv(
            docker,
            plugin=plugin,
            name=name,
            image=self.config.image,
            plugin_network=network_name(plugin),
            policy=egress.proxy_policy(
                network, network_local, self._networks[network_name(plugin)]
            ),
            python=IMAGE_PYTHON,
            labels={EGRESS_LABEL: plugin},
        )

        async def remove() -> None:
            await self._run(docker, "rm", "--force", name)

        try:
            process = await egress.open_proxy(argv, self._env)
        except OSError as error:
            raise _unavailable(
                plugin, f"the egress proxy cannot be started: {error.strerror}"
            ) from None
        proxy = egress.ProxyHandle(plugin, name, process, remove)
        try:
            if not await proxy.ready():
                why = await proxy.error()
                raise _unavailable(plugin, f"the egress proxy did not start: {why}")
            code, out, _ = await self._run(
                docker,
                "container",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                name,
            )
            try:
                networks = json.loads(out) if code == 0 else None
            except ValueError:
                networks = None
            address = egress.plugin_address(networks, network_name(plugin))
            if address is None:
                raise _unavailable(
                    plugin, f"the egress proxy has no address on {network_name(plugin)}"
                )
        except BaseException:
            await proxy.stop()
            raise
        proxy.url = f"http://{address}:{LISTEN_PORT}"
        return proxy

    async def _egress_network(self, plugin: str, docker: str) -> None:
        """Create `horae-egress` if needed, and check it (decision 76 B): a bridge that is not
        internal, without IPv6, labelled, with only egress proxies attached."""
        async with self._lock:
            if self._egress_checked:
                return
            name = egress.EGRESS_NETWORK
            found = await self._inspect_network(docker, name)
            if found is None:
                options = [
                    f"--opt={key}={value}" for key, value in egress.EGRESS_NETWORK_OPTIONS.items()
                ]
                await self._run(
                    docker,
                    "network",
                    "create",
                    "--driver=bridge",
                    *options,
                    f"--label={NETWORK_LABEL}={egress.EGRESS_NETWORK_LABEL}",
                    name,
                )
                found = await self._inspect_network(docker, name)
            if found is None:
                raise _unavailable(plugin, f"network {name} cannot be created")
            problem = egress.egress_network_problem(found) or await self._intruders(
                docker, None, found
            )
            if problem is not None:
                raise _unavailable(
                    plugin, f"network {name} exists but {problem}: remove it (`docker network rm`)"
                )
            self._egress_checked = True

    async def _run(self, *args: str) -> tuple[int, bytes, bytes]:
        """Run a Docker CLI command, bounded by `ENGINE_TIMEOUT_S`; (-1, b"", message) when it
        cannot be run or times out."""
        try:
            with anyio.fail_after(ENGINE_TIMEOUT_S):
                done = await anyio.run_process(
                    list(args),
                    stdin=subprocess.DEVNULL,
                    env=self._env,
                    check=False,
                    start_new_session=True,
                )
        except TimeoutError:
            return -1, b"", b"the container engine did not answer in time"
        except OSError as error:
            return -1, b"", (error.strerror or "cannot run the Docker CLI").encode()
        return done.returncode, done.stdout[:_MAX_ENGINE_OUTPUT], done.stderr[:_MAX_ENGINE_OUTPUT]

    async def _engine(self, plugin: str) -> str:
        """The Docker CLI, once the image is known to be present (checked once)."""
        async with self._lock:
            if self._docker is not None:
                return self._docker
            docker = self.config.docker or shutil.which("docker", path=BASE_PATH)
            if docker is None or not os.path.isfile(docker) or not os.access(docker, os.X_OK):
                raise _unavailable(plugin, "the Docker CLI is not installed")
            await self._check_local(plugin, docker)
            image = self.config.image
            code, _, error = await self._run(
                docker, "image", "inspect", "--format", "{{.Id}}", image
            )
            if code != 0:
                raise _unavailable(
                    plugin,
                    f"image {image} is not present or the engine is unreachable "
                    f"(`docker pull {image}`): {_first_line(error)}",
                )
            self._docker = docker
            return docker

    async def _check_local(self, plugin: str, docker: str) -> None:
        """Refuse an engine that is not a local `unix://` socket (decision 74 B): a remote one
        would get the secrets over the network and bind its own host's paths. `DOCKER_HOST`
        first, then the endpoint of the context the CLI uses (which reflects `DOCKER_HOST`,
        `DOCKER_CONTEXT` and the configured current context)."""
        host = self._env.get("DOCKER_HOST")
        if host is not None and not is_local_endpoint(host):
            raise _unavailable(plugin, f"DOCKER_HOST is not a local unix socket ({_scheme(host)})")
        code, out, error = await self._run(
            docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
        )
        if code != 0:
            raise _unavailable(
                plugin, f"the Docker context cannot be inspected: {_first_line(error)}"
            )
        endpoint = out.decode("utf-8", errors="replace").strip()
        if not is_local_endpoint(endpoint):
            raise _unavailable(
                plugin, f"the Docker engine is not a local unix socket ({_scheme(endpoint)})"
            )
        self._socket = endpoint.removeprefix("unix://")

    async def _network(self, plugin: str, docker: str) -> None:
        """Create the plugin's network if needed, and check it (decision 74 D)."""
        name = network_name(plugin)
        if name in self._networks:
            return
        found = await self._inspect_network(docker, name)
        if found is None:
            options = [f"--opt={key}={value}" for key, value in NETWORK_OPTIONS.items()]
            await self._run(
                docker,
                "network",
                "create",
                "--driver=bridge",
                "--internal",
                *options,
                f"--label={NETWORK_LABEL}=plugin",
                name,
            )
            found = await self._inspect_network(docker, name)  # created here, or concurrently
        if found is None:
            raise _unavailable(plugin, f"network {name} cannot be created")
        problem = network_problem(found) or await self._intruders(docker, plugin, found)
        subnet = egress.network_subnet(found)
        if problem is None and subnet is None:
            problem = "has no IPv4 subnet"
        if problem is not None or subnet is None:
            raise _unavailable(
                plugin, f"network {name} exists but {problem}: remove it (`docker network rm`)"
            )
        self._networks[name] = subnet

    async def _intruders(
        self, docker: str, plugin: str | None, network: Mapping[str, object]
    ) -> str | None:
        """Why a container attached to the plugin's network does not belong there, or `None`:
        each must carry `dev.horae.plugin=<plugin>` (another container of this plugin, a
        concurrent run) or `EGRESS_LABEL=<plugin>` (its proxy). For the egress network
        (`plugin` is `None`), each must be an egress proxy."""
        attached = network.get("Containers")
        if not attached:
            return None
        if not isinstance(attached, dict):
            return "lists its containers in an unknown form"
        ids = sorted(str(key) for key in attached)
        code, out, _ = await self._run(
            docker, "container", "inspect", "--format", "{{json .Config.Labels}}", *ids
        )
        if code != 0:
            return "has containers that cannot be inspected"
        lines = out.decode("utf-8", errors="replace").splitlines()
        if len(lines) != len(ids):
            return "has containers that cannot be inspected"
        for line in lines:
            try:
                labels = json.loads(line)
            except ValueError:
                return "has containers that cannot be inspected"
            if not isinstance(labels, dict):
                labels = {}  # a container without any label
            if plugin is None and not isinstance(labels.get(EGRESS_LABEL), str):
                return "has a container attached that is not an egress proxy"
            if plugin is not None and not container_belongs(labels, plugin):
                return "has a container attached that is not this plugin's"
        return None

    async def _inspect_network(self, docker: str, name: str) -> dict[str, object] | None:
        code, out, _ = await self._run(docker, "network", "inspect", "--format", "{{json .}}", name)
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


def network_problem(network: Mapping[str, object]) -> str | None:
    """Why `network` (`docker network inspect`) is not a plugin network, or `None`."""
    if network.get("Driver") != "bridge":
        return "is not a bridge network"
    if network.get("Internal") is not True:
        return "is not internal"
    if network.get("EnableIPv6") is True:
        return "has IPv6 enabled"
    if network.get("ConfigOnly") is True:
        return "is a configuration-only network"
    config_from = network.get("ConfigFrom")
    if isinstance(config_from, dict) and config_from.get("Network"):
        return "takes its configuration from another network"
    labels = network.get("Labels")
    if not isinstance(labels, dict) or labels.get(NETWORK_LABEL) != "plugin":
        return f"has no {NETWORK_LABEL}=plugin label"
    options = network.get("Options")
    if not isinstance(options, dict):
        return "has no driver options"
    for key, value in NETWORK_OPTIONS.items():
        if options.get(key) != value:
            return f"does not set {key}={value}"
    return None


def container_belongs(labels: Mapping[str, object], plugin: str) -> bool:
    """Whether a container with `labels` may be on `plugin`'s network: one of its own
    containers, or its own egress proxy (`EGRESS_LABEL=<plugin>`, never another plugin's)."""
    return labels.get(PLUGIN_LABEL) == plugin or labels.get(EGRESS_LABEL) == plugin


def is_local_endpoint(endpoint: str) -> bool:
    """Whether a Docker endpoint is a local socket (`unix:///...`)."""
    return endpoint.startswith("unix:///")


def _scheme(endpoint: str) -> str:
    scheme, separator, _ = endpoint.partition("://")
    return f"{scheme}://" if separator and scheme.isalnum() else "unknown endpoint"


def secrets_token() -> str:
    """The random part of a container name."""
    return secrets.token_hex(6)


__all__ = [
    "EGRESS_LABEL",
    "ENGINE_ENV_KEYS",
    "IMAGE_PYTHON",
    "LAUNCHER",
    "NETWORK_OPTIONS",
    "OOM_EXIT",
    "PLUGIN_LABEL",
    "PLUGIN_MOUNT",
    "TMPFS",
    "ContainerRuntime",
    "check_venv",
    "container_belongs",
    "container_command",
    "engine_env",
    "engine_problem",
    "is_local_endpoint",
    "limit_options",
    "mount_source",
    "network_name",
    "network_problem",
    "preamble",
    "run_argv",
    "tmpfs_option",
]
