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
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.contracts import InstanceConfig
from horae.contracts.instance import DEFAULT_CONTAINER_IMAGE, ContainerConfig, PluginDefaults
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
)
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
    for flag in ("--interactive", "--rm", "--init", "--read-only"):
        assert flag in options
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


def test_the_only_mount_is_the_plugin_directory_read_only() -> None:
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


def _network(**changes: Any) -> dict[str, Any]:
    return {
        "Name": "horae-plugin-jail",
        "Driver": "bridge",
        "Internal": True,
        "EnableIPv6": False,
        "Options": dict(NETWORK_OPTIONS),
    } | changes


def test_only_an_isolated_network_is_accepted() -> None:
    assert network_name("jail") == "horae-plugin-jail"
    assert network_problem(_network()) is None
    assert network_problem(_network(Internal=False)) == "is not internal"
    assert network_problem(_network(Driver="macvlan")) == "is not a bridge network"
    assert network_problem(_network(EnableIPv6=True)) == "has IPv6 enabled"
    assert network_problem(_network(Options=None)) == "has no driver options"
    for key in NETWORK_OPTIONS:
        options = dict(NETWORK_OPTIONS)
        del options[key]
        assert network_problem(_network(Options=options)) is not None
    icc = dict(NETWORK_OPTIONS) | {"com.docker.network.bridge.enable_icc": "true"}
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
    state = os.path.join(config_dir, "network.json")
    if os.path.exists(state):
        print(open(state).read())
        sys.exit(0)
    print("Error: network not found", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["network", "create"]:
    with open(os.path.join(config_dir, "network.json"), "w") as handle:
        handle.write(json.dumps(fake["created"]))
    sys.exit(0)
if args[:1] == ["rm"]:
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
    os.execv(sys.executable, [sys.executable, fake["server"], "normal"])
sys.exit(2)
"""


def _fake_docker(tmp_path: Path, **fake: Any) -> tuple[Path, Path]:
    """A fake Docker CLI and its state directory (passed as `DOCKER_CONFIG`)."""
    state = tmp_path / "docker-state"
    state.mkdir()
    settings = {"image": True, "created": _network(), "server": str(RAW_SERVER)} | fake
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


def _manifest(command: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": "raw",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": ["raw.ok"]},
        "permissions": {"secrets": ["JAIL_TOKEN"]},
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
) -> PluginHost:
    from horae.contracts import PluginManifest
    from horae.plugins.host import HostRegistry

    manifest = _manifest()
    lock = lock_for(manifest)
    registry = HostRegistry(lock, {"raw": PluginManifest.model_validate(manifest)}, {"raw": plugin})
    resolver = SecretResolver({}, secrets_dir(tmp_path, JAIL_TOKEN=TOKEN))
    config = ContainerConfig(docker=None if docker is None else str(docker))
    h = PluginHost(
        registry,
        secrets=resolver,
        work_dir=tmp_path / "work",
        timeouts=lambda _name: PluginDefaults(runtime=runtime, startup_timeout_s=30.0),  # type: ignore[arg-type]
        container=config,
    )
    environ = {} if state is None else {"DOCKER_CONFIG": str(state)}
    h._containers = ContainerRuntime(config, uid=uid, environ=environ)
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
        return await runtime.spawn(
            "jail",
            root=plugin,
            command=["python", "-m", "jail_server"],
            secrets=secrets or {},
            timezone="UTC",
            cwd=tmp_path,
            startup_timeout_s=10.0,
        )

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
    assert kinds[:4] == [
        ["image", "inspect"],
        ["network", "inspect"],
        ["network", "create"],
        ["network", "inspect"],
    ]
    run = next(call for call in calls if call[0] == "run")
    assert TOKEN not in json.dumps(calls)
    name = run[run.index("--name") + 1]
    assert ["rm", "--force", name] in calls  # removed once stopped, whatever the plugin did
    create = next(call for call in calls if call[:2] == ["network", "create"])
    assert "--internal" in create
    assert "--opt=com.docker.network.bridge.inhibit_ipv4=true" in create
    assert "--opt=com.docker.network.bridge.enable_icc=false" in create


def test_an_existing_network_that_is_not_isolated_is_refused_and_kept(tmp_path: Path) -> None:
    plugin = _plugin_dir(tmp_path)
    cli, state = _fake_docker(tmp_path)
    (state / "network.json").write_text(json.dumps(_network(Internal=False)), encoding="utf-8")
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
