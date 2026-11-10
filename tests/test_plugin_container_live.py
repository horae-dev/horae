# SPDX-License-Identifier: Apache-2.0
"""The container runtime against a real Docker engine (#49, schemas decision 74).

Marked `container`; skipped when no Docker CLI on the base `PATH` reaches a daemon, or when the
plugin image cannot be pulled (set `HORAE_SKIP_CONTAINER_TESTS=1` to skip them anyway). The
plugin is `tests/fixtures/plugins/jail/jail_server.py`, a stdlib-only MCP server installed in a
venv that the plugin image's own interpreter builds, which reports what it can do from inside.
Names are synthetic; the only addresses are a documentation one (192.0.2.1) and the plugin
network's own subnet.
"""

import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import PluginManifest
from horae.contracts.instance import DEFAULT_CONTAINER_IMAGE, ContainerConfig, PluginDefaults
from horae.plugins.host import HostRegistry, PluginCallError, PluginHost
from horae.plugins.host.env import BASE_PATH, PLUGIN_KEYS
from horae.secrets import SecretResolver
from plugin_fixtures import lock_for, secrets_dir

pytestmark = pytest.mark.container

JAIL_SERVER = Path(__file__).resolve().parent / "fixtures" / "plugins" / "jail" / "jail_server.py"
TOKEN = "sentinel-value-for-jail-live"  # noqa: S105 (synthetic)
TOOLS = ["env", "probe", "connect", "resolve"]
PLUGINS = ("jail", "jail-stubborn", "jail-open")


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker", path=BASE_PATH)
    assert docker is not None
    return subprocess.run(  # noqa: S603 (the Docker CLI, fixed arguments)
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[None]:
    if os.environ.get("HORAE_SKIP_CONTAINER_TESTS"):
        pytest.skip("HORAE_SKIP_CONTAINER_TESTS is set")
    if shutil.which("docker", path=BASE_PATH) is None:
        pytest.skip("no Docker CLI on the base PATH")
    if _docker("info", "--format", "{{.ServerVersion}}", timeout=30).returncode != 0:
        pytest.skip("the Docker daemon is not reachable")
    if _docker("image", "inspect", DEFAULT_CONTAINER_IMAGE).returncode != 0:
        pulled = _docker("pull", "--quiet", DEFAULT_CONTAINER_IMAGE, timeout=600)
        if pulled.returncode != 0:
            pytest.skip("the plugin image cannot be pulled")
    yield
    for name in PLUGINS:
        _docker("network", "rm", f"horae-plugin-{name}")


@pytest.fixture(scope="module")
def plugin_dir(engine: None, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A plugin directory whose venv the image's interpreter built, holding `jail_server`."""
    root = tmp_path_factory.mktemp("jail-plugin")
    built = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={root.resolve()},target=/plugin",
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-m",
        "venv",
        "--without-pip",
        "/plugin/.venv",
    )
    assert built.returncode == 0, built.stderr
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    shutil.copy(JAIL_SERVER, site / "jail_server.py")
    return root


def _manifest(name: str, mode: str = "normal") -> dict[str, Any]:
    command = ["python", "-m", "jail_server", mode, name]
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": [f"{name}.{tool}" for tool in TOOLS]},
        "permissions": {"secrets": ["JAIL_TOKEN"]},
        "runtime": {"protocol": "mcp-stdio", "command": command},
    }


def _host(
    tmp_path: Path,
    plugin_dir: Path,
    name: str = "jail",
    mode: str = "normal",
    image: str | None = None,
) -> PluginHost:
    manifest = _manifest(name, mode)
    registry = HostRegistry(
        lock_for(manifest), {name: PluginManifest.model_validate(manifest)}, {name: plugin_dir}
    )
    resolver = SecretResolver({}, secrets_dir(tmp_path, JAIL_TOKEN=TOKEN))
    defaults = PluginDefaults(runtime="container", startup_timeout_s=120.0, call_timeout_s=60.0)
    return PluginHost(
        registry,
        secrets=resolver,
        work_dir=tmp_path / "work",
        timeouts=lambda _name: defaults,
        container=ContainerConfig(image=image or DEFAULT_CONTAINER_IMAGE),
    )


async def _call(h: PluginHost, ref: str, **params: Any) -> Any:
    return (await h.call(ref, params, None)).data


def test_a_plugin_runs_in_its_container_with_exactly_its_environment(
    tmp_path: Path, plugin_dir: Path
) -> None:
    async def go() -> Any:
        async with _host(tmp_path, plugin_dir) as h:
            return await _call(h, "jail.env")

    env = asyncio.run(go())["env"]
    # Delivered through stdin, not the command line; the host redacts it on the way back.
    assert env["JAIL_TOKEN"] == "[REDACTED:JAIL_TOKEN]"  # noqa: S105 (a redaction marker)
    assert set(env) == PLUGIN_KEYS | {"VIRTUAL_ENV", "JAIL_TOKEN"}
    assert env["VIRTUAL_ENV"] == "/plugin/.venv"
    assert env["PATH"].startswith("/plugin/.venv/bin:")


def test_the_sandbox_seen_from_inside(tmp_path: Path, plugin_dir: Path) -> None:
    async def go() -> Any:
        async with _host(tmp_path, plugin_dir) as h:
            return await _call(h, "jail.probe")

    seen = asyncio.run(go())
    assert seen["uid"] == os.getuid() != 0
    assert seen["gid"] == os.getgid()
    assert int(seen["cap_eff"], 16) == 0
    assert int(seen["cap_bnd"], 16) == 0
    assert seen["no_new_privs"] == "1"
    assert seen["seccomp"] == "2"  # a filter: Docker's default profile
    assert seen["root_writable"] is False
    assert seen["plugin_writable"] is False
    assert seen["venv_writable"] is False
    assert seen["tmp_writable"] is True
    assert seen["tmp_executable"] is False
    assert seen["cwd"] == "/tmp/work"  # noqa: S108 (inside the container)
    assert len(seen["visible_pids"]) <= 3  # its init and itself: no host process


def test_the_container_has_no_egress_and_cannot_reach_the_host(
    tmp_path: Path, plugin_dir: Path
) -> None:
    port = 20000 + secrets.randbelow(20000)
    listener = f"horae-test-listener-{secrets.token_hex(4)}"
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--network",
        "host",
        "--name",
        listener,
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-m",
        "http.server",
        str(port),
        "--bind",
        "0.0.0.0",  # noqa: S104 (a listener on every host address: what the plugin must miss)
    )
    assert started.returncode == 0, started.stderr
    try:
        probe = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=1)"
        deadline = time.monotonic() + 30
        while _docker("exec", listener, "python", "-c", probe).returncode != 0:
            assert time.monotonic() < deadline, "the host listener did not start"
            time.sleep(0.2)

        async def go() -> tuple[Any, Any, Any]:
            async with _host(tmp_path, plugin_dir) as h:
                outside = await _call(h, "jail.connect", host="192.0.2.1", port=80)
                inspected = _docker("network", "inspect", "horae-plugin-jail")
                subnet = json.loads(inspected.stdout)[0]["IPAM"]["Config"][0]["Subnet"]
                gateway = str(next(ipaddress.ip_network(subnet).hosts()))
                host = await _call(h, "jail.connect", host=gateway, port=port)
                dns = await _call(h, "jail.resolve", name="example.com")
                return outside, host, dns

        outside, host, dns = asyncio.run(go())
    finally:
        _docker("rm", "--force", listener)
    assert outside["connected"] is False
    assert host["connected"] is False, host  # no host address on the bridge (inhibit_ipv4)
    assert dns["resolved"] is False


def test_docker_inspect_shows_the_hardening_and_no_secret(tmp_path: Path, plugin_dir: Path) -> None:
    async def go() -> str:
        async with _host(tmp_path, plugin_dir) as h:
            await _call(h, "jail.env")
            listed = _docker("ps", "--quiet", "--filter", "label=dev.horae.plugin=jail")
            ids = listed.stdout.split()
            assert len(ids) == 1, listed
            return _docker("inspect", ids[0]).stdout

    raw = asyncio.run(go())
    assert TOKEN not in raw
    data = json.loads(raw)[0]
    config, host = data["Config"], data["HostConfig"]
    assert "JAIL_TOKEN" not in json.dumps(config["Env"])
    assert config["User"] == f"{os.getuid()}:{os.getgid()}"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert host["Privileged"] is False
    assert "no-new-privileges" in host["SecurityOpt"]
    assert not any("seccomp" in option for option in host["SecurityOpt"])
    assert host["NetworkMode"] == "horae-plugin-jail"
    assert host["LogConfig"]["Type"] == "none"
    assert host["Init"] is True
    assert list(host["Tmpfs"]) == ["/tmp"]  # noqa: S108
    mounts = data["Mounts"]
    assert [(m["Type"], m["Destination"], m["RW"]) for m in mounts] == [("bind", "/plugin", False)]


def test_a_plugin_that_ignores_eof_and_sigterm_is_removed(tmp_path: Path, plugin_dir: Path) -> None:
    async def go() -> None:
        async with _host(tmp_path, plugin_dir, "jail-stubborn", "stubborn") as h:
            await _call(h, "jail-stubborn.probe")

    asyncio.run(go())
    deadline = time.monotonic() + 30
    while True:
        listed = _docker(
            "ps", "--all", "--quiet", "--filter", "label=dev.horae.plugin=jail-stubborn"
        )
        if not listed.stdout.strip():
            break
        assert time.monotonic() < deadline, "the container outlived the host"
        time.sleep(0.2)


def test_a_missing_image_fails_closed(tmp_path: Path, plugin_dir: Path) -> None:
    absent = "registry.example.test/horae/absent@sha256:" + "0" * 64

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin_dir, image=absent) as h:
            with pytest.raises(PluginCallError) as info:
                await _call(h, "jail.env")
            return info.value

    error = asyncio.run(go())
    assert error.kind == "runtime_unavailable"
    assert "docker pull" in error.message


def test_a_network_that_is_not_internal_is_refused(tmp_path: Path, plugin_dir: Path) -> None:
    created = _docker("network", "create", "horae-plugin-jail-open")
    assert created.returncode == 0, created.stderr

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin_dir, "jail-open") as h:
            with pytest.raises(PluginCallError) as info:
                await _call(h, "jail-open.env")
            return info.value

    try:
        error = asyncio.run(go())
    finally:
        _docker("network", "rm", "horae-plugin-jail-open")
    assert error.kind == "runtime_unavailable"
    assert "is not internal" in error.message
