# SPDX-License-Identifier: Apache-2.0
"""The container runtime: one Docker container per plugin, MCP over its stdio (#49).

Schemas decision 74 (provisional) is the contract; `docs/operations/plugin-containers.md` the
operator guide. A plugin whose effective `runtime` (`horae.yaml`: `plugins.<name>.runtime`, else
`plugin_defaults.runtime`) is `container` is started by `ContainerRuntime.spawn`, which returns
the same `Spawn` the process runtime builds, only for the Docker CLI:

- `docker run --interactive --rm --init` (`run_argv`), as Horae's own non-root uid and gid, with
  a read-only root filesystem, every capability dropped, `no-new-privileges`, Docker's default
  seccomp profile (never overridden), a `noexec` tmpfs on `/tmp`, no log kept by the daemon,
  nothing pulled, and **only the plugin's directory** (its tree and its `.venv`) bind-mounted
  read-only at `/plugin`;
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
  byte by byte before it `execve`s the plugin command with exactly that environment;
- the command runs from the plugin's venv only (`container_command`), and the venv must be one
  the image's interpreter can run (`check_venv`);
- once the CLI is gone, `docker rm --force` removes the container whatever the plugin did.

**Fail closed**: whatever is missing (the CLI, the daemon, the image, the network, the venv) is a
`runtime_unavailable` error; the host never falls back to the process runtime.
"""

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio

from horae.contracts.instance import ContainerConfig
from horae.plugins.host import egress
from horae.plugins.host.egress_proxy import LISTEN_PORT
from horae.plugins.host.env import BASE_PATH, VENV_DIR, build_env
from horae.plugins.host.errors import PluginCallError
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
TMPFS = f"{CONTAINER_TMP}:rw,noexec,nosuid,nodev,size=64m,mode=1777"
"""The only writable place in a container."""
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
_UNSAFE_MOUNT = re.compile(r'[,"\x00-\x1f\x7f]')


def launcher_source(
    home: str = CONTAINER_HOME, tmpdir: str = CONTAINER_TMPDIR, workdir: str = CONTAINER_WORKDIR
) -> str:
    """The launcher's source, creating `home`, `tmpdir` and `workdir` (tests use other paths)."""
    return f"""\
import json, os, sys
def fail(message):
    os.write(2, b"horae-launch: " + message.encode() + b"\\n")
    os._exit(70)
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
try:
    os.execve(sys.argv[1], sys.argv[1:], env)
except OSError as error:
    fail("cannot run " + sys.argv[1] + ": " + str(error.strerror))
"""


LAUNCHER = launcher_source()
"""Run as `python3 -I -S -c LAUNCHER <command>` in the container (decision 74 E). Its source is
visible in `docker inspect`; it holds no secret."""


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
    if not real.is_dir() or _UNSAFE_MOUNT.search(text):
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


def run_argv(
    docker: str,
    *,
    plugin: str,
    name: str,
    image: str,
    user: str,
    source: str,
    command: list[str],
) -> list[str]:
    """The `docker run` command line of decision 74 C (no secret: they go in the preamble)."""
    return [
        docker,
        "run",
        "--interactive",
        "--rm",
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
        "--tmpfs",
        TMPFS,
        "--network",
        network_name(plugin),
        "--mount",
        f"type=bind,source={source},target={PLUGIN_MOUNT},readonly",
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
        uid: int | None = None,
        gid: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or ContainerConfig()
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
    ) -> Spawn:
        """The `Spawn` of `plugin`'s container, its egress proxy started with the locked
        `network` and `network_local` as policy; `runtime_unavailable` if either cannot run
        (`env_unsafe` for a denied secret name, as in the process runtime)."""
        if self._uid == 0:
            raise _unavailable(plugin, "Horae runs as root; plugin containers never do")
        if root is None:
            raise _unavailable(plugin, "it has no installed directory")
        venv = check_venv(plugin, root)
        inside = container_command(plugin, command, root)
        source = mount_source(plugin, root)
        docker = await self._engine(plugin)
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
        )

        async def remove() -> None:
            try:
                await self._run(docker, "rm", "--force", name)
            finally:
                await proxy.stop()

        return Spawn(
            plugin,
            tuple(argv),
            dict(self._env),
            cwd,
            startup_timeout_s,
            preamble=line,
            cleanup=remove,
            egress=proxy,
        )

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
    "PLUGIN_LABEL",
    "PLUGIN_MOUNT",
    "TMPFS",
    "ContainerRuntime",
    "check_venv",
    "container_belongs",
    "container_command",
    "engine_env",
    "is_local_endpoint",
    "mount_source",
    "network_name",
    "network_problem",
    "preamble",
    "run_argv",
]
