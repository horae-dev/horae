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
- on the plugin's own network, `horae-plugin-<name>` (`network_name`): `--internal`, no
  inter-container traffic, and no host address on the bridge (`inhibit_ipv4`), so the plugin
  reaches nothing, the host's services included; checked on each run's first start of the plugin,
  and refused (never repaired) when it is not exactly that. #50 attaches its egress proxy there;
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
from collections.abc import Mapping
from pathlib import Path

import anyio

from horae.contracts.instance import ContainerConfig
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
NETWORK_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "false",
    "com.docker.network.bridge.inhibit_ipv4": "true",
}
"""Driver options a plugin network must have (decision 74 D)."""
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
        self._networks: set[str] = set()

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
    ) -> Spawn:
        """The `Spawn` of `plugin`'s container; `runtime_unavailable` if it cannot run in one
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
        env = build_env(
            plugin,
            home=Path(CONTAINER_HOME),
            tmpdir=Path(CONTAINER_TMPDIR),
            timezone=timezone,
            secrets=secrets,
            venv=Path(PLUGIN_MOUNT) / venv.name,
        )
        line = preamble(env)
        env.clear()
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
            await self._run(docker, "rm", "--force", name)

        return Spawn(
            plugin,
            tuple(argv),
            dict(self._env),
            cwd,
            startup_timeout_s,
            preamble=line,
            cleanup=remove,
        )

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
        problem = network_problem(found)
        if problem is not None:
            raise _unavailable(
                plugin, f"network {name} exists but {problem}: remove it (`docker network rm`)"
            )
        self._networks.add(name)

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
    options = network.get("Options")
    if not isinstance(options, dict):
        return "has no driver options"
    for key, value in NETWORK_OPTIONS.items():
        if options.get(key) != value:
            return f"does not set {key}={value}"
    return None


def secrets_token() -> str:
    """The random part of a container name."""
    return secrets.token_hex(6)


__all__ = [
    "ENGINE_ENV_KEYS",
    "IMAGE_PYTHON",
    "LAUNCHER",
    "NETWORK_OPTIONS",
    "PLUGIN_LABEL",
    "PLUGIN_MOUNT",
    "TMPFS",
    "ContainerRuntime",
    "check_venv",
    "container_command",
    "engine_env",
    "mount_source",
    "network_name",
    "network_problem",
    "preamble",
    "run_argv",
]
