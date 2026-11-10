# SPDX-License-Identifier: Apache-2.0
"""The container runtime without a container engine (#49, schemas decision 74).

The generated `docker run` command (every hardening flag, no host network, read-only mounts,
no secret), the venv and command checks, the network check, the launcher's protocol, and the
fail-closed paths through a real `PluginHost`, with a fake Docker CLI where an engine would be
needed. `tests/test_plugin_container_live.py` runs the same against a real engine.
"""

import asyncio
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.contracts import InstanceConfig
from horae.contracts.instance import (
    DEFAULT_CONTAINER_IMAGE,
    ContainerConfig,
    ContainerLimits,
    PluginDefaults,
)
from horae.plugins.host import PluginCallError, PluginHost
from horae.plugins.host.container import (
    ENGINE_ENV_KEYS,
    LAUNCHER,
    NETWORK_OPTIONS,
    TMPFS,
    ContainerRuntime,
    check_venv,
    container_command,
    engine_env,
    launcher_source,
    mount_source,
    network_name,
    network_problem,
    preamble,
    run_argv,
    tmpfs_option,
)
from horae.plugins.host.mounts import MountPolicy
from horae.secrets import Secret, SecretResolver
from plugin_fixtures import COMMIT, RAW_SERVER, lock_for, secrets_dir

REPO = Path(__file__).resolve().parent.parent
TOKEN = "sentinel-value-for-jail"  # noqa: S105 (synthetic)
GOOD_CFG = "home = /usr/local/bin\nimplementation = CPython\nversion_info = 3.12.16\n"
_PLATFORM_ADDED = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()


def _plugin_dir(root: Path, cfg: str = GOOD_CFG, scripts: tuple[str, ...] = ("python",)) -> Path:
    """A plugin directory with a venv as the Horae image builds it (`bin/python` a symlink to
    the image's interpreter, which need not exist here)."""
    plugin = root / "plugin"
    bin_dir = plugin / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (plugin / ".venv" / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    for script in scripts:
        (bin_dir / script).symlink_to("/usr/local/bin/python3.12")
    (plugin / "server.py").write_text("print()\n", encoding="utf-8")
    return plugin


def _argv(**changes: Any) -> list[str]:
    values: dict[str, Any] = {
        "plugin": "jail",
        "name": "horae-jail-0123456789ab",
        "image": DEFAULT_CONTAINER_IMAGE,
        "user": "10001:10001",
        "source": "/srv/instance/plugins/jail",
        "command": ["/plugin/.venv/bin/python", "-m", "jail_server"],
    } | changes
    return run_argv("/usr/bin/docker", **values)


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(argv))


# --- the command line -------------------------------------------------------------------------


def test_every_hardening_flag_is_present() -> None:
    argv = _argv()
    image_at = argv.index(DEFAULT_CONTAINER_IMAGE)
    options, after = argv[:image_at], argv[image_at + 1 :]
    assert options[:2] == ["/usr/bin/docker", "run"]
    for flag in ("--interactive", "--init", "--read-only"):
        assert flag in options
    assert "--rm" not in options  # inspected for OOMKilled, then removed (decision 78 E)
    pairs = _pairs(options)
    for pair in [
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--user", "10001:10001"),
        ("--pull", "never"),
        ("--log-driver", "none"),
        ("--tmpfs", TMPFS),
        ("--network", "horae-plugin-jail"),
        ("--label", "dev.horae.plugin=jail"),
        ("--name", "horae-jail-0123456789ab"),
        ("--entrypoint", "/usr/local/bin/python3"),
    ]:
        assert pair in pairs
    assert "noexec" in TMPFS
    assert "nosuid" in TMPFS
    assert "nodev" in TMPFS
    assert after[:4] == ["-I", "-S", "-c", LAUNCHER]
    assert after[4:] == ["/plugin/.venv/bin/python", "-m", "jail_server"]


def test_nothing_widens_the_container() -> None:
    options = _argv()[: _argv().index(DEFAULT_CONTAINER_IMAGE)]
    joined = " ".join(options)
    for forbidden in (
        "--privileged",
        "--cap-add",
        "--device",
        "--volume",
        "-v",
        "--env",
        "-e",
        "--env-file",
        "--pid",
        "--ipc",
        "--uts",
        "--userns",
        "--group-add",
        "--publish",
        "-p",
        "-t",
        "--tty",
    ):
        assert forbidden not in options, forbidden
    assert "seccomp" not in joined
    assert "apparmor" not in joined
    assert "unconfined" not in joined
    network = options[options.index("--network") + 1]
    assert network not in ("host", "bridge", "default")
    assert not network.startswith("container:")
    assert options.count("--network") == 1
    assert options.count("--security-opt") == 1


def test_without_declared_paths_the_only_mount_is_the_plugin_directory_read_only() -> None:
    options = _argv()[: _argv().index(DEFAULT_CONTAINER_IMAGE)]
    mounts = [value for flag, value in _pairs(options) if flag == "--mount"]
    assert mounts == ["type=bind,source=/srv/instance/plugins/jail,target=/plugin,readonly"]
    assert [value for flag, value in _pairs(options) if flag == "--tmpfs"] == [TMPFS]


def test_no_secret_reaches_the_command_line(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    spawn = _spawn(tmp_path, plugin, secrets={"JAIL_TOKEN": Secret("JAIL_TOKEN", TOKEN)})
    assert spawn.preamble is not None
    assert TOKEN not in " ".join(spawn.argv)
    assert TOKEN not in json.dumps(spawn.env)
    assert TOKEN.encode() in spawn.preamble
    env = json.loads(spawn.preamble)
    assert env["JAIL_TOKEN"] == TOKEN
    assert env["PATH"] == "/plugin/.venv/bin:/usr/local/bin:/usr/bin:/bin"
    assert env["VIRTUAL_ENV"] == "/plugin/.venv"
    assert (env["HOME"], env["TMPDIR"]) == ("/tmp/home", "/tmp/tmp")  # noqa: S108
    assert env["UV_OFFLINE"] == "1"
    assert env["PIP_NO_INDEX"] == "1"


def test_the_preamble_is_one_ascii_line() -> None:
    line = preamble({"A_SECRET": "line one\nline two \u00e9 \u2028"})
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert line.isascii()
    assert json.loads(line) == {"A_SECRET": "line one\nline two \u00e9 \u2028"}
    with pytest.raises(PluginCallError) as info:
        preamble({"A_SECRET": "x" * (1024 * 1024)})
    assert info.value.kind == "env_unsafe"


def test_the_engine_gets_its_connection_settings_only() -> None:
    environ = {
        "HOME": "/home/someone",
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "AWS_SECRET_ACCESS_KEY": "sentinel-ambient",
        "HTTPS_PROXY": "http://proxy.example.test:3128",
        "PATH": "/opt/somewhere/bin",
        "LD_PRELOAD": "/tmp/x.so",  # noqa: S108
    }
    env = engine_env(environ)
    assert env == {
        "HOME": "/home/someone",
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    assert set(env) - {"PATH", "LANG"} <= set(ENGINE_ENV_KEYS)


# --- the venv and the command -----------------------------------------------------------------


def test_a_venv_of_the_image_interpreter_is_accepted(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    assert check_venv("jail", plugin) == plugin / ".venv"
    stdlib_venv = "home = /usr/local/bin\nversion = 3.12.16\ninclude-system-site-packages = false\n"
    (plugin / ".venv" / "pyvenv.cfg").write_text(stdlib_venv, encoding="utf-8")
    assert check_venv("jail", plugin) == plugin / ".venv"


@pytest.mark.parametrize(
    ("cfg", "needle"),
    [
        ("home = /opt/homebrew/bin\nversion_info = 3.12.16\n", "/opt/homebrew/bin"),
        ("home = /usr/local/bin\nversion_info = 3.13.1\n", "3.13.1"),
        ("home = /usr/local/bin\n", "Python ?"),
        (GOOD_CFG + "include-system-site-packages = true\n", "system site-packages"),
    ],
)
def test_a_venv_the_image_cannot_run_is_refused(tmp_path: Path, cfg: str, needle: str) -> None:
    plugin = _plugin_dir(tmp_path, cfg)
    with pytest.raises(PluginCallError) as info:
        check_venv("jail", plugin)
    assert info.value.kind == "runtime_unavailable"
    assert needle in info.value.message


def test_a_plugin_without_its_own_venv_is_refused(tmp_path: Path) -> None:
    plugin = tmp_path / "builtin"
    plugin.mkdir()
    with pytest.raises(PluginCallError) as info:
        check_venv("file-sink", plugin)
    assert "runtime: process" in info.value.message
    (plugin / ".venv").symlink_to(_plugin_dir(tmp_path) / ".venv")
    with pytest.raises(PluginCallError, match="not a directory"):
        check_venv("file-sink", plugin)


def test_a_venv_without_python_or_with_a_strange_cfg_is_refused(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path, scripts=())
    with pytest.raises(PluginCallError, match="bin/python is missing"):
        check_venv("jail", plugin)
    cfg = plugin / ".venv" / "pyvenv.cfg"
    cfg.unlink()
    cfg.mkdir()
    with pytest.raises(PluginCallError, match="small regular file"):
        check_venv("jail", plugin)
    cfg.rmdir()
    cfg.write_bytes(b"\xff\xfe")
    with pytest.raises(PluginCallError, match="cannot be read"):
        check_venv("jail", plugin)


def test_commands_map_into_the_container(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path, scripts=("python", "jail-server"))
    assert container_command("jail", ["python", "-m", "jail_server"], plugin) == [
        "/plugin/.venv/bin/python",
        "-m",
        "jail_server",
    ]
    assert container_command("jail", ["jail-server"], plugin) == ["/plugin/.venv/bin/jail-server"]
    assert container_command("jail", ["./server.py", "x"], plugin) == ["/plugin/server.py", "x"]


@pytest.mark.parametrize(
    "first",
    [sys.executable, "/usr/local/bin/python3", "sh", "..", "../outside.py", ".venv/bin/python"],
)
def test_commands_outside_the_plugin_venv_are_refused(tmp_path: Path, first: str) -> None:
    plugin = _plugin_dir(tmp_path)
    (tmp_path / "outside.py").write_text("print()\n", encoding="utf-8")
    with pytest.raises(PluginCallError) as info:
        container_command("jail", [first], plugin)
    assert info.value.kind == "runtime_unavailable"


def test_a_path_mount_cannot_carry_is_refused(tmp_path: Path) -> None:
    odd = tmp_path / "a,readonly=false"
    odd.mkdir()
    with pytest.raises(PluginCallError, match="cannot be mounted"):
        mount_source("jail", odd)
    with pytest.raises(PluginCallError, match="cannot be mounted"):
        mount_source("jail", tmp_path / "missing")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert mount_source("jail", link) == str(real.resolve())


# --- the network ------------------------------------------------------------------------------


def _egress(**changes: Any) -> dict[str, Any]:
    return {
        "Name": "horae-egress",
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "ConfigOnly": False,
        "ConfigFrom": {"Network": ""},
        "Labels": {"dev.horae.network": "egress"},
        "Options": {"com.docker.network.bridge.enable_icc": "false"},
        "Containers": {},
    } | changes


def _network(**changes: Any) -> dict[str, Any]:
    return {
        "Name": "horae-plugin-jail",
        "Driver": "bridge",
        "Internal": True,
        "EnableIPv6": False,
        "ConfigOnly": False,
        "ConfigFrom": {"Network": ""},
        "Labels": {"dev.horae.network": "plugin"},
        "Options": dict(NETWORK_OPTIONS),
        "IPAM": {"Config": [{"Subnet": "192.168.155.0/24"}]},
        "Containers": {},
    } | changes


def test_only_an_isolated_network_is_accepted() -> None:
    assert network_name("jail") == "horae-plugin-jail"
    assert network_problem(_network()) is None
    assert network_problem(_network(Internal=False)) == "is not internal"
    assert network_problem(_network(Driver="macvlan")) == "is not a bridge network"
    assert network_problem(_network(EnableIPv6=True)) == "has IPv6 enabled"
    assert network_problem(_network(Options=None)) == "has no driver options"
    assert network_problem(_network(Labels={})) == "has no dev.horae.network=plugin label"
    assert network_problem(_network(Labels=None)) == "has no dev.horae.network=plugin label"
    assert network_problem(_network(ConfigOnly=True)) == "is a configuration-only network"
    assert (
        network_problem(_network(ConfigFrom={"Network": "elsewhere"}))
        == "takes its configuration from another network"
    )
    for key in NETWORK_OPTIONS:
        options = dict(NETWORK_OPTIONS)
        del options[key]
        assert network_problem(_network(Options=options)) is not None
    # #49's networks (no inter-container traffic) cannot reach their proxy: refused, not changed
    icc = dict(NETWORK_OPTIONS) | {"com.docker.network.bridge.enable_icc": "false"}
    assert network_problem(_network(Options=icc)) is not None


# --- the launcher -----------------------------------------------------------------------------


def _launch(tmp_path: Path, stdin: bytes, *command: str) -> subprocess.CompletedProcess[bytes]:
    source = launcher_source(
        home=str(tmp_path / "home"), tmpdir=str(tmp_path / "tmp"), workdir=str(tmp_path / "work")
    )
    return subprocess.run(  # noqa: S603 (this interpreter, fixed arguments)
        [sys.executable, "-I", "-S", "-c", source, *command],
        input=stdin,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_the_launcher_reads_one_line_and_execs_with_exactly_that_environment(
    tmp_path: Path,
) -> None:
    env = {"JAIL_TOKEN": TOKEN, "LANG": "C.UTF-8", "HOME": str(tmp_path / "home")}
    report = (
        "import json, os, sys; print(json.dumps([dict(os.environ), os.getcwd(), sys.stdin.read()]))"
    )
    done = _launch(
        tmp_path, bytes(preamble(env)) + b'{"jsonrpc":"2.0"}\nrest', sys.executable, "-c", report
    )
    assert done.returncode == 0, done.stderr
    seen, cwd, rest = json.loads(done.stdout)
    assert {key: value for key, value in seen.items() if key not in _PLATFORM_ADDED} == env
    assert Path(cwd).resolve() == (tmp_path / "work").resolve()
    assert rest == '{"jsonrpc":"2.0"}\nrest'  # nothing of the MCP stream was consumed
    for directory in ("home", "tmp", "work"):
        assert (tmp_path / directory).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        (b"", "stdin closed"),
        (b'{"A": "B"', "stdin closed"),
        (b"not json\n", "not JSON"),
        (b'["a"]\n', "object of strings"),
        (b'{"A": 1}\n', "object of strings"),
    ],
)
def test_the_launcher_refuses_a_bad_environment_line(
    tmp_path: Path, stdin: bytes, message: str
) -> None:
    done = _launch(tmp_path, stdin, sys.executable, "-c", "print('ran')")
    assert done.returncode == 70
    assert message in done.stderr.decode()
    assert b"ran" not in done.stdout


def test_the_launcher_refuses_an_overlong_line_and_a_missing_command(tmp_path: Path) -> None:
    done = _launch(tmp_path, b"x" * (1024 * 1024 + 10), sys.executable, "-c", "print('ran')")
    assert done.returncode == 70
    assert b"too long" in done.stderr
    done = _launch(tmp_path, b"{}\n")
    assert done.returncode == 70
    assert b"no command" in done.stderr
    done = _launch(tmp_path, b"{}\n", str(tmp_path / "absent"))
    assert done.returncode == 70
    assert b"cannot run" in done.stderr


# --- configuration ----------------------------------------------------------------------------


def test_the_runtime_is_chosen_per_plugin_with_an_instance_default() -> None:
    config = InstanceConfig.model_validate(
        {
            "plugin_defaults": {"runtime": "container"},
            "plugins": {"file-sink": {"runtime": "process"}, "jail": {"call_timeout_s": 5.0}},
        }
    )
    assert config.plugin_timeouts("file-sink").runtime == "process"
    assert config.plugin_timeouts("jail").runtime == "container"
    assert config.plugin_timeouts("jail").call_timeout_s == 5.0
    assert InstanceConfig().plugin_timeouts("jail").runtime == "process"
    with pytest.raises(ValidationError):
        InstanceConfig.model_validate({"plugin_defaults": {"runtime": "vm"}})


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim-bookworm",
        "python@sha256:abc",
        "python:3.12@sha256:" + "A" * 64,
        "Python@sha256:" + "a" * 64,
    ],
)
def test_the_image_must_be_pinned_by_digest(image: str) -> None:
    with pytest.raises(ValidationError):
        ContainerConfig.model_validate({"image": image})


def test_the_image_and_cli_settings() -> None:
    mirror = "registry.example.test/mirror/python:3.12@sha256:" + "0" * 64
    config = ContainerConfig.model_validate({"image": mirror, "docker": "/usr/bin/docker"})
    assert config.image == mirror
    with pytest.raises(ValidationError):
        ContainerConfig.model_validate({"docker": "docker"})


def test_the_default_image_is_the_horae_image_base() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", dockerfile, re.MULTILINE)
    assert match is not None
    assert match.group(1) == DEFAULT_CONTAINER_IMAGE


def test_renovate_bumps_the_default_image() -> None:
    renovate = json.loads((REPO / "renovate.json").read_text(encoding="utf-8"))
    source = (REPO / "src" / "horae" / "contracts" / "instance.py").read_text(encoding="utf-8")
    managers = [
        manager
        for manager in renovate["customManagers"]
        if "src/horae/contracts/instance" in " ".join(manager["managerFilePatterns"])
    ]
    assert len(managers) == 1
    pattern = managers[0]["matchStrings"][0].replace("(?<", "(?P<")
    found = re.search(pattern, source)
    assert found is not None
    assert found.group("depName") == "python"
    assert (
        f"{found.group('currentValue')}@{found.group('currentDigest')}" in DEFAULT_CONTAINER_IMAGE
    )


# --- through the host, with a fake Docker CLI --------------------------------------------------

FAKE_DOCKER = """\
import json, os, sys
config_dir = os.environ["DOCKER_CONFIG"]
with open(os.path.join(config_dir, "fake.json")) as handle:
    fake = json.load(handle)
with open(os.path.join(config_dir, "calls.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    if fake["image"]:
        print("sha256:" + "0" * 64)
        sys.exit(0)
    print("Error: No such image: " + args[-1], file=sys.stderr)
    sys.exit(1)
if args[:2] == ["network", "inspect"]:
    state = os.path.join(config_dir, "network-" + args[-1] + ".json")
    if os.path.exists(state):
        print(open(state).read())
        sys.exit(0)
    print("Error: network not found", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["network", "create"]:
    created = fake["egress_created"] if args[-1] == "horae-egress" else fake["created"]
    with open(os.path.join(config_dir, "network-" + args[-1] + ".json"), "w") as handle:
        handle.write(json.dumps(created))
    sys.exit(0)
if args[:2] == ["context", "inspect"]:
    print(fake["endpoint"])
    sys.exit(0)
if args[:2] == ["container", "inspect"] and ".Mounts" in args[3]:
    if fake["mounts"] is not None:
        print(json.dumps(fake["mounts"]))
        sys.exit(0)
    run = json.load(open(os.path.join(config_dir, "run.json")))
    mounts = []
    for flag, value in zip(run, run[1:]):
        if flag == "--mount":
            fields = dict(part.partition("=")[::2] for part in value.split(","))
            mounts.append(
                {
                    "Type": "bind",
                    "Source": fields["source"],
                    "Destination": fields["target"],
                    "RW": "readonly" not in fields,
                }
            )
    print(json.dumps(mounts))
    sys.exit(0)
if args[:2] == ["container", "inspect"] and "OOMKilled" in args[3]:
    counter = os.path.join(config_dir, "oom-inspects")
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(seen + 1))
    print(("true" if fake["oom"] and seen >= fake["oom_after"] else "false"), fake["exit_code"])
    sys.exit(0)
if args[:2] == ["container", "inspect"] and "NetworkSettings" in args[3]:
    print(json.dumps(fake["proxy_networks"]))
    sys.exit(0)
if args[:2] == ["container", "inspect"]:
    for container in args[4:]:
        print(json.dumps(fake["labels"][container]))
    sys.exit(0)
if args[:1] == ["ps"]:
    status = next(a for a in args if a.startswith("status=")).split("=", 1)[1]
    print("\\n".join(fake["leftovers"].get(status, [])))
    sys.exit(0)
if args[:1] == ["rm"]:
    sys.exit(0)
if args[:1] == ["run"] and any(a.startswith("dev.horae.egress-proxy=") for a in args):
    if fake["proxy"] != "ok":
        print("docker: Error response from daemon: " + fake["proxy"], file=sys.stderr)
        sys.exit(125)
    print(json.dumps({"event": "ready", "port": 3128}), flush=True)
    for event in fake["blocked"]:
        print(json.dumps(event), flush=True)
    for line in sys.stdin.buffer:
        with open(os.path.join(config_dir, "proxy-stdin"), "ab") as handle:
            handle.write(line)
        if fake["admit"] and json.loads(line).get("client"):
            print(json.dumps({"event": "admitted"}), flush=True)
    with open(os.path.join(config_dir, "proxy-stopped"), "a") as handle:
        handle.write("stdin closed\\n")
    sys.exit(0)
if args[:1] == ["run"]:
    line = bytearray()
    while not line.endswith(b"\\n"):
        byte = os.read(0, 1)
        if not byte:
            sys.exit(70)
        line += byte
    with open(os.path.join(config_dir, "preamble.json"), "wb") as handle:
        handle.write(bytes(line))
    with open(os.path.join(config_dir, "run.json"), "w") as handle:
        handle.write(json.dumps(args))
    os.execv(sys.executable, [sys.executable, fake["server"], fake["mode"]])
sys.exit(2)
"""


def _fake_docker(tmp_path: Path, **fake: Any) -> tuple[Path, Path]:
    """A fake Docker CLI and its state directory (passed as `DOCKER_CONFIG`)."""
    state = tmp_path / "docker-state"
    state.mkdir()
    settings = {
        "image": True,
        "created": _network(),
        "server": str(RAW_SERVER),
        "endpoint": "unix:///var/run/docker.sock",
        "labels": {},
        "egress_created": _egress(),
        "proxy": "ok",
        "admit": True,
        "blocked": [],
        "oom": False,
        "oom_after": 0,
        "exit_code": 137,
        "mounts": None,
        "leftovers": {},
        "mode": "normal",
        "proxy_networks": {
            f"horae-plugin-{name}": {"IPAddress": "192.168.155.2"} for name in ("raw", "jail")
        }
        | {"horae-egress": {"IPAddress": "192.168.156.2"}},
    } | fake
    (state / "fake.json").write_text(json.dumps(settings), encoding="utf-8")
    cli = tmp_path / "bin" / "docker"
    cli.parent.mkdir()
    cli.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}", encoding="utf-8")
    cli.chmod(0o700)
    return cli, state


def _calls(state: Path) -> list[list[str]]:
    path = state / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _manifest(
    command: list[str] | None = None, paths: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    return {
        "name": "raw",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": ["raw.ok", "raw.rpc_error", "raw.close"]},
        "permissions": {"secrets": ["JAIL_TOKEN"]} | ({"paths": paths} if paths else {}),
        "runtime": {"protocol": "mcp-stdio", "command": command or ["python", "-m", "raw"]},
    }


def _host(
    tmp_path: Path,
    plugin: Path,
    *,
    docker: Path | None,
    state: Path | None = None,
    uid: int | None = None,
    runtime: str = "container",
    environ: dict[str, str] | None = None,
    paths: dict[str, list[str]] | None = None,
    mounts: MountPolicy | None = None,
    limits: ContainerLimits | None = None,
) -> PluginHost:
    from horae.contracts import PluginManifest
    from horae.plugins.host import HostRegistry

    manifest = _manifest(paths=paths)
    lock = lock_for(manifest)
    registry = HostRegistry(lock, {"raw": PluginManifest.model_validate(manifest)}, {"raw": plugin})
    resolver = SecretResolver({}, secrets_dir(tmp_path, JAIL_TOKEN=TOKEN))
    config = ContainerConfig(docker=None if docker is None else str(docker))
    h = PluginHost(
        registry,
        secrets=resolver,
        work_dir=tmp_path / "work",
        timeouts=lambda _name: PluginDefaults(
            runtime=runtime,  # type: ignore[arg-type]
            startup_timeout_s=30.0,
            limits=limits or ContainerLimits(),
        ),
        container=config,
    )
    engine = {} if state is None else {"DOCKER_CONFIG": str(state)}
    h._containers = ContainerRuntime(
        config, mounts=mounts, uid=uid, environ=engine | (environ or {})
    )
    return h


def _spawn(tmp_path: Path, plugin: Path, *, secrets: dict[str, Secret] | None = None) -> Any:
    cli, state = _fake_docker(tmp_path)
    runtime = ContainerRuntime(
        ContainerConfig(docker=str(cli)),
        uid=10001,
        gid=10001,
        environ={"DOCKER_CONFIG": str(state)},
    )

    async def go() -> Any:
        spawn = await runtime.spawn(
            "jail",
            root=plugin,
            command=["python", "-m", "jail_server"],
            secrets=secrets or {},
            timezone="UTC",
            cwd=tmp_path,
            startup_timeout_s=10.0,
        )
        assert spawn.egress is not None
        await spawn.egress.stop()  # the plugin is never run here: stop its (fake) proxy
        return spawn

    return asyncio.run(go())


def test_a_container_plugin_runs_through_the_cli_with_its_environment_on_stdin(
    tmp_path: Path,
) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            return await h.call("raw.ok", {}, None)

    result = asyncio.run(go())
    assert result.data == {"ok": True}
    env = json.loads((state / "preamble.json").read_bytes())
    assert env["JAIL_TOKEN"] == TOKEN
    calls = _calls(state)
    kinds = [call[:2] for call in calls]
    assert kinds[:8] == [
        ["context", "inspect"],
        ["image", "inspect"],
        ["network", "inspect"],
        ["network", "create"],
        ["network", "inspect"],
        ["network", "inspect"],  # horae-egress (decision 76)
        ["network", "create"],
        ["network", "inspect"],
    ]
    proxy, run = (call for call in calls if call[0] == "run")
    assert "dev.horae.egress-proxy=raw" in proxy  # the proxy first, then the plugin
    assert kinds[9] == ["container", "inspect"]  # the proxy's address
    assert TOKEN not in json.dumps(calls)
    name = run[run.index("--name") + 1]
    assert ["rm", "--force", name] in calls  # removed once stopped, whatever the plugin did
    proxy_name = proxy[proxy.index("--name") + 1]
    assert calls.index(["rm", "--force", name]) < calls.index(["rm", "--force", proxy_name])
    assert (state / "proxy-stopped").read_text() == "stdin closed\n"
    create = next(call for call in calls if call[:2] == ["network", "create"])
    assert "--internal" in create
    assert "--opt=com.docker.network.bridge.inhibit_ipv4=true" in create
    assert "--opt=com.docker.network.bridge.enable_icc=true" in create
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://192.168.155.2:3128"
    assert env["HORAE_EGRESS_PROXY"] == "http://192.168.155.2:3128"
    assert "NO_PROXY" not in env
    assert "no_proxy" not in env


def test_an_existing_network_that_is_not_isolated_is_refused_and_kept(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    (state / "network-horae-plugin-raw.json").write_text(
        json.dumps(_network(Internal=False)), encoding="utf-8"
    )
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "is not internal" in error.message
    assert not any(call[0] in ("run",) or call[:2] == ["network", "rm"] for call in _calls(state))


def test_a_missing_image_fails_closed(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, image=False)
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "docker pull" in error.message
    assert "No such image" in error.message
    assert not any(call[0] == "run" for call in _calls(state))


def test_a_network_that_cannot_be_created_fails_closed(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, created="not an object")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "cannot be created" in error.message


def _first_error(tmp_path: Path, plugin: Path, **kw: Any) -> PluginCallError:
    async def go() -> tuple[PluginCallError, PluginCallError]:
        async with _host(tmp_path, plugin, **kw) as h:
            with pytest.raises(PluginCallError) as first:
                await h.call("raw.ok", {}, None)
            with pytest.raises(PluginCallError) as second:
                await h.call("raw.ok", {}, None)
            return first.value, second.value

    first, second = asyncio.run(go())
    assert second.kind == "plugin_unavailable"  # a failed start is final for the run
    return first


def test_no_engine_means_no_plugin_never_a_process(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    error = _first_error(tmp_path, plugin, docker=tmp_path / "no-docker-here")
    assert error.kind == "runtime_unavailable"
    assert "Docker CLI is not installed" in error.message
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").rglob("home"))


def test_a_plugin_the_container_cannot_run_is_refused_before_the_engine(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path, "home = /usr/bin\nversion_info = 3.12.3\n")
    cli, state = _fake_docker(tmp_path)
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "/usr/bin" in error.message
    assert _calls(state) == []


def test_root_never_starts_a_container(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    error = _first_error(tmp_path, plugin, docker=cli, state=state, uid=0)
    assert error.kind == "runtime_unavailable"
    assert "root" in error.message
    assert _calls(state) == []


def test_the_process_runtime_is_unchanged(tmp_path: Path) -> None:
    from horae.contracts import PluginManifest
    from horae.plugins.host import HostRegistry

    manifest = _manifest([sys.executable, str(RAW_SERVER)])
    registry = HostRegistry(
        lock_for(manifest), {"raw": PluginManifest.model_validate(manifest)}, {"raw": tmp_path}
    )
    resolver = SecretResolver({}, secrets_dir(tmp_path, JAIL_TOKEN=TOKEN))

    async def go() -> Any:
        async with PluginHost(registry, secrets=resolver, work_dir=tmp_path / "work") as h:
            return await h.call("raw.ok", {}, None)

    assert asyncio.run(go()).data == {"ok": True}


def test_the_engine_check_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from horae.plugins.host import container

    slow = tmp_path / "slow-docker"
    slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    slow.chmod(0o700)
    monkeypatch.setattr(container, "ENGINE_TIMEOUT_S", 0.5)
    runtime = ContainerRuntime(ContainerConfig(docker=str(slow)), uid=10001, environ={})

    async def go() -> tuple[int, bytes, bytes]:
        return await runtime._run(str(slow), "image", "inspect")

    code, _, error = asyncio.run(go())
    assert code == -1
    assert b"did not answer" in error
    code, _, error = asyncio.run(_unrunnable(runtime, tmp_path))
    assert code == -1
    assert error


async def _unrunnable(runtime: ContainerRuntime, tmp_path: Path) -> tuple[int, bytes, bytes]:
    return await runtime._run(str(tmp_path / "absent"), "version")


def test_the_lock_entry_is_unchanged_by_the_runtime() -> None:
    lock = lock_for(_manifest())
    assert "runtime" not in json.dumps(lock.model_dump(mode="json")["plugins"]["raw"])
    assert lock.plugins["raw"].commit == COMMIT


def test_secrets_in_the_preamble_are_zeroed_once_sent(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    seen: list[bytearray | None] = []
    original = ContainerRuntime.spawn

    async def spying(self: ContainerRuntime, *args: Any, **kwargs: Any) -> Any:
        spawn = await original(self, *args, **kwargs)
        seen.append(spawn.preamble)
        return spawn

    async def go() -> None:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            await h.call("raw.ok", {}, None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ContainerRuntime, "spawn", spying)
        asyncio.run(go())
    assert seen
    assert seen[0] is not None
    assert all(byte == 0 for byte in seen[0])
    assert os.environ.get("JAIL_TOKEN") is None


@pytest.mark.parametrize(
    ("environ", "endpoint", "refused"),
    [
        ({"DOCKER_HOST": "tcp://192.0.2.10:2375"}, "tcp://192.0.2.10:2375", "tcp://"),
        ({"DOCKER_HOST": "ssh://someone@engine.example.test"}, "unix:///x.sock", "ssh://"),
        ({}, "tcp://192.0.2.10:2376", "tcp://"),
        ({}, "ssh://someone@engine.example.test", "ssh://"),
        ({}, "npipe:////./pipe/docker_engine", "npipe://"),
        ({}, "", "unknown endpoint"),
        (
            {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"},
            "unix:///run/user/1000/docker.sock",
            None,
        ),
        ({}, "unix:///var/run/docker.sock", None),
    ],
)
def test_only_a_local_engine_is_used(
    tmp_path: Path, environ: dict[str, str], endpoint: str, refused: str | None
) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, endpoint=endpoint)

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state, environ=environ) as h:
            return await h.call("raw.ok", {}, None)

    if refused is None:
        assert asyncio.run(go()).data == {"ok": True}
        return
    error = _first_error(tmp_path, plugin, docker=cli, state=state, environ=environ)
    assert error.kind == "runtime_unavailable"
    assert "local unix socket" in error.message
    assert refused in error.message
    assert "192.0.2.10" not in error.message  # the endpoint is not echoed
    assert not any(call[:2] == ["image", "inspect"] or call[0] == "run" for call in _calls(state))


@pytest.mark.parametrize(
    ("labels", "accepted"),
    [
        ({"dev.horae.plugin": "raw"}, True),
        ({"dev.horae.egress-proxy": "raw"}, True),
        ({"dev.horae.egress-proxy": "other"}, False),  # another plugin's proxy (decision 76 C)
        ({"dev.horae.egress-proxy": "1"}, False),
        ({"dev.horae.plugin": "other"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_a_foreign_container_on_the_network_is_refused(
    tmp_path: Path, labels: dict[str, str] | None, accepted: bool
) -> None:
    plugin = _plugin_dir(tmp_path)
    attached = {"c0ffee": {"Name": "somebody"}}
    cli, state = _fake_docker(tmp_path, labels={"c0ffee": labels})
    network = _network(Name="horae-plugin-raw", Containers=attached)
    (state / "network-horae-plugin-raw.json").write_text(json.dumps(network), encoding="utf-8")

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            return await h.call("raw.ok", {}, None)

    if accepted:
        assert asyncio.run(go()).data == {"ok": True}
        return
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "not this plugin's" in error.message
    assert not any(call[0] == "run" for call in _calls(state))


def test_a_stalled_preamble_write_is_a_startup_timeout(tmp_path: Path) -> None:
    from horae.plugins.host.capture import StderrCapture
    from horae.plugins.host.session import PluginProcess, Spawn

    # A process that never reads stdin: a preamble larger than the pipe buffer never drains.
    spawn = Spawn(
        "raw",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        {},
        tmp_path,
        1.0,
        preamble=bytearray(b"x" * (300 * 1024) + b"\n"),
    )
    process = PluginProcess(spawn, StderrCapture(1024))

    async def go() -> tuple[float, PluginCallError | None]:
        import anyio

        start = time.monotonic()
        async with anyio.create_task_group() as group:
            group.start_soon(process.serve)
            with anyio.fail_after(30):
                await process.ready.wait()
            await process.stop(forced=True)
        return time.monotonic() - start, process.error

    elapsed, error = asyncio.run(go())
    assert error is not None
    assert error.kind == "startup_timeout"
    assert elapsed < 15
    assert spawn.preamble is not None
    assert not any(spawn.preamble)
    assert process.state.returncode is not None  # killed, not left behind


# --- the egress proxy (#50, decision 76), with a fake proxy --------------------------------------


def _blocked(host: str, port: int = 443, reason: str = "host_not_allowed") -> dict[str, Any]:
    return {"event": "blocked", "host": host, "port": port, "reason": reason}


def test_blocked_attempts_become_violations_of_the_call(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(
        tmp_path,
        blocked=[
            _blocked("evil.example.net"),
            _blocked(f"{TOKEN}.evil.example.net"),  # a secret smuggled in a name
            {"event": "ready", "port": 3128},  # anything but a report is ignored
        ],
    )

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            first = await h.call("raw.ok", {}, None)
            second = await h.call("raw.ok", {}, None)
            return first, second

    first, second = asyncio.run(go())
    assert [v.kind for v in first.violations] == ["egress_blocked", "egress_blocked"]
    assert first.violations[0].detail == (
        "raw: egress blocked to evil.example.net:443 (host_not_allowed)"
    )
    assert TOKEN not in first.violations[1].detail
    assert "[REDACTED:JAIL_TOKEN]" in first.violations[1].detail
    assert second.violations == ()  # taken once


def test_blocked_attempts_are_kept_when_the_call_fails(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, blocked=[_blocked("evil.example.net", 80)])

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call("raw.rpc_error", {}, None)
            return info.value

    error = asyncio.run(go())
    assert error.kind == "protocol_error"
    assert [v.detail for v in error.violations] == [
        "raw: egress blocked to evil.example.net:80 (host_not_allowed)"
    ]


def test_blocked_attempts_reach_the_run_record(tmp_path: Path) -> None:
    from horae.engine.executor import _Run

    class Failing:
        async def call(self, *_: Any, **__: Any) -> Any:
            from horae.contracts.run import Violation

            raise PluginCallError(
                "tool_error",
                "raw.ok: refused",
                violations=(Violation(kind="egress_blocked", detail="raw: egress blocked"),),
            )

    run = _Run(Failing(), None, None, None, {}, (), time.monotonic)
    call = type("Call", (), {"ref": "raw.ok", "params": {}, "timeout_s": None})()
    outcome = asyncio.run(run.call(call, None))
    assert [v.kind for v in outcome.violations] == ["egress_blocked"]


def test_a_proxy_that_does_not_start_means_no_plugin(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, proxy="network horae-egress not found")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "egress proxy did not start" in error.message
    assert "network horae-egress not found" in error.message
    runs = [call for call in _calls(state) if call[0] == "run"]
    assert len(runs) == 1  # the proxy only: the plugin never ran
    name = runs[0][runs[0].index("--name") + 1]
    assert ["rm", "--force", name] in _calls(state)


def test_a_proxy_without_an_address_on_the_plugin_network_means_no_plugin(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, proxy_networks={"horae-egress": {"IPAddress": "10.9.0.2"}})
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "no address on horae-plugin-raw" in error.message
    runs = [call for call in _calls(state) if call[0] == "run"]
    assert len(runs) == 1
    assert (state / "proxy-stopped").exists()  # its stdin was closed
    name = runs[0][runs[0].index("--name") + 1]
    assert ["rm", "--force", name] in _calls(state)


@pytest.mark.parametrize(
    ("network", "labels", "problem"),
    [
        (_egress(Internal=True), {}, "is internal"),
        (_egress(Labels={}), {}, "dev.horae.network=egress"),
        (
            _egress(Containers={"c0ffee": {}}),
            {"c0ffee": {"dev.horae.plugin": "x"}},
            "not an egress",
        ),
    ],
)
def test_an_egress_network_that_is_not_ours_is_refused_and_kept(
    tmp_path: Path, network: dict[str, Any], labels: dict[str, Any], problem: str
) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, labels=labels)
    (state / "network-horae-egress.json").write_text(json.dumps(network), encoding="utf-8")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "horae-egress" in error.message
    assert problem in error.message
    calls = _calls(state)
    assert not any(call[0] == "run" or call[:2] == ["network", "rm"] for call in calls)


def test_the_proxy_policy_is_the_locked_one(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    runtime = ContainerRuntime(
        ContainerConfig(docker=str(cli)),
        uid=10001,
        gid=10001,
        environ={"DOCKER_CONFIG": str(state)},
    )

    async def go() -> Any:
        spawn = await runtime.spawn(
            "jail",
            root=plugin,
            command=["python", "-m", "jail_server"],
            secrets={},
            timezone="UTC",
            cwd=tmp_path,
            startup_timeout_s=10.0,
            network=["api.example.org"],
            network_local=["printer.lan:8080"],
        )
        assert spawn.cleanup is not None
        await spawn.cleanup()
        return spawn

    spawn = asyncio.run(go())
    proxy = next(call for call in _calls(state) if call[0] == "run")
    assert json.loads(proxy[-1]) == {
        "network": ["api.example.org"],
        "network_local": ["printer.lan:8080"],
        "clients": ["192.168.155.0/24"],  # it answers the plugin's network only
    }
    assert spawn.egress is not None
    assert spawn.egress.url == "http://192.168.155.2:3128"


def test_the_proxy_serves_the_plugin_container_only(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            return await h.call("raw.ok", {}, None)

    assert asyncio.run(go()).data == {"ok": True}
    assert json.loads((state / "proxy-stdin").read_bytes()) == {"client": "192.168.155.2"}
    run = [call for call in _calls(state) if call[0] == "run"][1]
    plugin_name = run[run.index("--name") + 1]
    assert [
        "container",
        "inspect",
        "--format",
        "{{json .NetworkSettings.Networks}}",
        plugin_name,
    ] in (_calls(state))


def test_a_proxy_that_cannot_be_bound_to_the_plugin_means_no_plugin(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, admit=False)
    from horae.plugins.host import egress

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(egress, "ADMIT_TIMEOUT_S", 0.5)
        error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "runtime_unavailable"
    assert "could not be bound" in error.message
    runs = [call for call in _calls(state) if call[0] == "run"]
    for started in runs:  # the plugin and its proxy are both removed
        assert ["rm", "--force", started[started.index("--name") + 1]] in _calls(state)


# --- declared mounts, limits and out-of-memory kills (#51, schemas decision 78) ----------------


def _instance(tmp_path: Path) -> tuple[Path, MountPolicy]:
    root = (tmp_path / "instance").resolve()
    for name in ("vault/inbox", "state", "secrets", "plugins"):
        (root / name).mkdir(parents=True)
    policy = MountPolicy.for_instance(
        root, state_dir=root / "state", secrets_dir=root / "secrets", home=tmp_path / "home"
    )
    return root, policy


def test_declared_paths_and_limits_reach_the_run_command(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    vault, inbox = str(root / "vault"), str(root / "vault" / "inbox")
    limits = ContainerLimits(cpus=0.5, memory_mb=256, pids=64, tmp_mb=16, nofile=512)

    async def go() -> Any:
        async with _host(
            tmp_path,
            plugin,
            docker=cli,
            state=state,
            paths={"read": [vault], "write": [inbox]},
            mounts=policy,
            limits=limits,
        ) as h:
            return await h.call("raw.ok", {}, None)

    assert asyncio.run(go()).data == {"ok": True}
    run = json.loads((state / "run.json").read_text(encoding="utf-8"))
    mounts = [value for flag, value in _pairs(run) if flag == "--mount"]
    assert mounts[1:] == [
        f"type=bind,source={vault},target={vault},bind-recursive=disabled,readonly",
        f"type=bind,source={inbox},target={inbox},bind-recursive=disabled",
    ]
    for flag in (
        "--cpus=0.5",
        "--memory=256m",
        "--memory-swap=256m",
        "--pids-limit=64",
        "--ulimit=nofile=512:512",
    ):
        assert flag in run
    assert ("--tmpfs", tmpfs_option(16)) in _pairs(run)
    assert "size=16m" in tmpfs_option(16)


def test_a_path_that_cannot_be_mounted_means_no_plugin_and_no_proxy(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    error = _first_error(
        tmp_path,
        plugin,
        docker=cli,
        state=state,
        paths={"read": [str(root / "state")]},
        mounts=policy,
    )
    assert error.kind == "runtime_unavailable"
    assert "cannot be mounted" in error.message
    assert not any(call[0] == "run" for call in _calls(state))


def test_without_an_instance_a_declared_path_is_refused(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, _ = _instance(tmp_path)
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"read": [str(root / "vault")]}
    )
    assert error.kind == "runtime_unavailable"
    assert "no instance" in error.message
    assert not any(call[0] == "run" for call in _calls(state))


def test_a_memory_kill_during_a_call_is_reported_as_oom_killed(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, oom=True)

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call("raw.close", {}, None)
            return info.value

    error = asyncio.run(go())
    assert error.kind == "oom_killed"
    assert "memory limit (512 MiB" in error.message
    calls = _calls(state)
    inspect = next(
        i
        for i, call in enumerate(calls)
        if "{{json .State.OOMKilled}} {{json .State.ExitCode}}" in call
    )
    run = next(call for call in calls if call[0] == "run" and "dev.horae.plugin=raw" in call)
    name = run[run.index("--name") + 1]
    assert calls.index(["rm", "--force", name]) > inspect  # inspected before it is removed


def test_a_crash_without_a_memory_kill_stays_a_crash(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call("raw.close", {}, None)
            return info.value

    assert asyncio.run(go()).kind == "plugin_crashed"


def test_a_memory_kill_during_the_start_is_reported_as_oom_killed(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, oom=True, mode="exit")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "oom_killed"


def _inspects(state: Path) -> int:
    counter = state / "oom-inspects"
    return int(counter.read_text(encoding="utf-8")) if counter.exists() else 0


def test_an_oom_flag_set_after_the_exit_is_still_reported_as_oom_killed(tmp_path: Path) -> None:
    """#188: the engine can set `OOMKilled` after the container's exit (137): asked again."""
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, oom=True, oom_after=4, mode="exit")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "oom_killed"
    assert _inspects(state) == 5


def test_a_sigkill_exit_without_an_oom_flag_is_asked_again_for_a_bounded_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.plugins.host import container

    monkeypatch.setattr(container, "OOM_POLL_S", 0.3)
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, mode="exit")  # exit 137, the flag never comes
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "handshake_failed"
    assert 2 <= _inspects(state) < 40


def test_an_exit_other_than_sigkill_is_asked_once(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, exit_code=1, mode="exit")
    error = _first_error(tmp_path, plugin, docker=cli, state=state)
    assert error.kind == "handshake_failed"
    assert _inspects(state) == 1


def test_a_container_horae_stopped_is_never_reported_as_oom_killed(tmp_path: Path) -> None:
    import anyio

    from horae.plugins.host import session
    from horae.plugins.host.capture import StderrCapture

    asked: list[bool] = []

    async def diagnose() -> tuple[Any, str] | None:
        asked.append(True)
        return ("oom_killed", "memory")

    async def go() -> list[Any]:
        spawn = session.Spawn("raw", ("true",), {}, tmp_path, 1.0, diagnose=diagnose)
        process = session.PluginProcess(spawn, StderrCapture(1024))
        crashed = await process._diagnose()  # it died by itself: asked
        process._stop.set()  # Horae stops it (a timeout or a stop)
        stopped = await process._diagnose()
        process._stop = anyio.Event()
        process._host_kill = True  # Horae ended a stalled start
        return [crashed, stopped, await process._diagnose()]

    assert asyncio.run(go()) == [("oom_killed", "memory"), None, None]
    assert asked == [True]


def test_a_container_whose_mounts_are_not_the_checked_ones_is_stopped(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    root, policy = _instance(tmp_path)
    vault = str(root / "vault")
    swapped = [
        {"Type": "bind", "Source": str(plugin), "Destination": "/plugin", "RW": False},
        {"Type": "bind", "Source": str(root / "state"), "Destination": vault, "RW": False},
    ]
    cli, state = _fake_docker(tmp_path, mounts=swapped)
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"read": [vault]}, mounts=policy
    )
    assert error.kind == "runtime_unavailable"
    assert "not mounted from itself" in error.message
    assert not (state / "proxy-stdin").exists()  # never admitted: no egress at all


def test_a_path_inside_another_container_plugins_writable_path_is_refused(
    tmp_path: Path,
) -> None:
    from horae.contracts import PluginManifest
    from horae.plugins.host import HostRegistry

    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    raw = _manifest(paths={"read": [str(root / "vault" / "inbox")]})
    writer = {
        "name": "writer",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": ["writer.ok"]},
        "permissions": {"paths": {"write": [str(root / "vault")]}},
        "runtime": {"protocol": "mcp-stdio", "command": ["python", "-m", "writer"]},
    }
    models = {
        name: PluginManifest.model_validate(m) for name, m in (("raw", raw), ("writer", writer))
    }
    registry = HostRegistry(lock_for(raw, writer), models, {"raw": plugin, "writer": plugin})

    def run(writer_runtime: str) -> Any:
        def timeouts(name: str) -> PluginDefaults:
            runtime = writer_runtime if name == "writer" else "container"
            return PluginDefaults(runtime=runtime, startup_timeout_s=30.0)  # type: ignore[arg-type]

        h = PluginHost(
            registry,
            secrets=SecretResolver({}, secrets_dir(tmp_path, JAIL_TOKEN=TOKEN)),
            work_dir=tmp_path / "work",
            timeouts=timeouts,
        )
        h._containers = ContainerRuntime(
            ContainerConfig(docker=str(cli)), mounts=policy, environ={"DOCKER_CONFIG": str(state)}
        )

        async def go() -> Any:
            async with h:
                try:
                    return await h.call("raw.ok", {}, None)
                except PluginCallError as error:
                    return error

        return asyncio.run(go())

    refused = run("container")
    assert isinstance(refused, PluginCallError)
    assert "which plugin writer may write" in refused.message
    # A process-runtime writer is unconfined anyway (T04): sharing the vault is allowed.
    assert run("process").data == {"ok": True}


def test_a_failed_sweep_quarantines_the_plugin_until_the_operator_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.plugins.host import mounts as mounts_module

    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    inbox = str(root / "vault" / "inbox")
    monkeypatch.setattr(mounts_module, "NEW_LIMIT", 0)
    monkeypatch.setattr(mounts_module, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps

    async def go() -> Any:
        async with _host(
            tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
        ) as h:
            first = await h.call("raw.ok", {}, None)
            (Path(inbox) / "new.md").write_text("x", encoding="utf-8")  # as if by the plugin
            return first

    assert asyncio.run(go()).data == {"ok": True}
    marker = root / "state" / "quarantine" / "raw"
    assert "more than 0 new entries" in marker.read_text(encoding="utf-8")
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    runs = len([c for c in _calls(state) if c[0] == "run"])
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
    )
    assert error.kind == "runtime_unavailable"
    assert "quarantined" in error.message
    assert len([c for c in _calls(state) if c[0] == "run"]) == runs  # nothing started
    marker.unlink()  # the operator checked the paths
    monkeypatch.setattr(mounts_module, "NEW_LIMIT", 100)

    async def again() -> Any:
        async with _host(
            tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
        ) as h:
            return await h.call("raw.ok", {}, None)

    assert asyncio.run(again()).data == {"ok": True}


def test_leftover_stopped_containers_of_the_plugin_are_removed_never_running_ones(
    tmp_path: Path,
) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path, leftovers={"exited": ["0123abcd"], "dead": ["4567ef01"]})

    async def go() -> Any:
        async with _host(tmp_path, plugin, docker=cli, state=state) as h:
            return await h.call("raw.ok", {}, None)

    assert asyncio.run(go()).data == {"ok": True}
    calls = _calls(state)
    listed = [call for call in calls if call[:1] == ["ps"]]
    assert {next(a for a in call if a.startswith("status=")) for call in listed} == {
        "status=exited",
        "status=dead",
    }
    assert all("label=dev.horae.plugin=raw" in call for call in listed)
    assert ["rm", "0123abcd", "4567ef01"] in calls  # no --force: a running one would refuse
    assert not any(call[:1] == ["ps"] and "status=running" in call for call in calls)


def test_a_run_whose_sweep_never_ran_quarantines_the_plugin(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    inbox = str(root / "vault" / "inbox")
    (root / "state" / "quarantine").mkdir()
    (root / "state" / "quarantine" / "raw.pending").touch()  # a Horae killed mid-run
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
    )
    assert error.kind == "runtime_unavailable"
    assert "ended before its writable paths were checked" in error.message
    assert not any(call[0] == "run" for call in _calls(state))


def test_a_clean_sweep_removes_the_pending_marker(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    inbox = str(root / "vault" / "inbox")
    pending = root / "state" / "quarantine" / "raw.pending"
    seen: list[bool] = []

    async def go() -> Any:
        async with _host(
            tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
        ) as h:
            result = await h.call("raw.ok", {}, None)
            seen.append(pending.exists())  # while it runs
            return result

    assert asyncio.run(go()).data == {"ok": True}
    assert seen == [True]
    assert not pending.exists()


def test_a_quarantine_that_cannot_be_written_keeps_the_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.plugins.host import mounts as mounts_module

    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    inbox = str(root / "vault" / "inbox")
    quarantine = root / "state" / "quarantine"
    monkeypatch.setattr(mounts_module, "NEW_LIMIT", -1)  # every sweep fails

    async def go() -> Any:
        async with _host(
            tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
        ) as h:
            first = await h.call("raw.ok", {}, None)
            quarantine.chmod(0o500)  # its quarantine file cannot be written now
            return first

    try:
        assert asyncio.run(go()).data == {"ok": True}
    finally:
        quarantine.chmod(0o700)
    assert not (quarantine / "raw").exists()
    assert (root / "state" / "quarantine" / "raw.pending").exists()
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
    )
    assert "ended before its writable paths were checked" in error.message


def test_a_quarantine_written_while_waiting_for_the_run_lock_is_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.plugins.host import mounts as mounts_module

    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    root, policy = _instance(tmp_path)
    inbox = str(root / "vault" / "inbox")
    real_lock = mounts_module.lock_plugin

    def lock_after_a_concurrent_quarantine(name: str, guard: MountPolicy | None) -> int:
        # Another run of the plugin quarantines it between the first check and the lock.
        assert mounts_module.quarantine(name, guard, ["a concurrent run's sweep failed"])
        return real_lock(name, guard)

    monkeypatch.setattr(mounts_module, "lock_plugin", lock_after_a_concurrent_quarantine)
    error = _first_error(
        tmp_path, plugin, docker=cli, state=state, paths={"write": [inbox]}, mounts=policy
    )
    assert error.kind == "runtime_unavailable"
    assert "quarantined" in error.message
    assert not any(call[0] == "run" for call in _calls(state))
