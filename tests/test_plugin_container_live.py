# SPDX-License-Identifier: Apache-2.0
"""The container runtime against a real Docker engine (#49, schemas decision 74).

Marked `container`; skipped when no Docker CLI on the base `PATH` reaches a daemon, or when the
plugin image cannot be pulled (set `HORAE_SKIP_CONTAINER_TESTS=1` to skip them anyway). The
plugin is `tests/fixtures/plugins/jail/jail_server.py`, a stdlib-only MCP server installed in a
venv that the plugin image's own interpreter builds, which reports what it can do from inside.
Names are synthetic; the only addresses are a documentation one (192.0.2.1), the plugin
network's own subnet and the egress network's gateway, where a local test server is published
(the egress proxy tests, #50). Nothing reaches the public internet: the only declared public
host is under `.test`, which never resolves.
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
from horae.plugins.host.env import BASE_PATH, PLUGIN_KEYS, PROXY_KEYS
from horae.secrets import SecretResolver
from plugin_fixtures import lock_for, secrets_dir

pytestmark = pytest.mark.container

JAIL_SERVER = Path(__file__).resolve().parent / "fixtures" / "plugins" / "jail" / "jail_server.py"
TOKEN = "sentinel-value-for-jail-live"  # noqa: S105 (synthetic)
TOOLS = ["env", "probe", "connect", "resolve", "tunnel", "fetch"]
PLUGINS = ("jail", "jail-stubborn", "jail-open", "jail-egress")


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
    _docker("network", "rm", "horae-egress")  # fails harmlessly while another run uses it


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


def _manifest(
    name: str, mode: str = "normal", network: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    command = ["python", "-m", "jail_server", mode, name]
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": [f"{name}.{tool}" for tool in TOOLS]},
        "permissions": {"secrets": ["JAIL_TOKEN"]} | (network or {}),
        "runtime": {"protocol": "mcp-stdio", "command": command},
    }


def _host(
    tmp_path: Path,
    plugin_dir: Path,
    name: str = "jail",
    mode: str = "normal",
    image: str | None = None,
    network: dict[str, list[str]] | None = None,
) -> PluginHost:
    manifest = _manifest(name, mode, network)
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
    assert set(env) == PLUGIN_KEYS | {"VIRTUAL_ENV", "JAIL_TOKEN", *PROXY_KEYS}
    assert env["HTTPS_PROXY"].startswith("http://")
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


# --- the egress proxy (#50, schemas decision 76) -------------------------------------------------


@pytest.fixture(scope="module")
def lan_server(engine: None) -> Iterator[tuple[str, int]]:
    """A local HTTP server on the Docker host's network, and the address the egress proxy
    reaches it at: `horae-egress`'s gateway (a private address, the host's own there) and its
    port. The egress network is created here as Horae would, so its gateway is known for the
    grant."""
    if _docker("network", "inspect", "horae-egress").returncode != 0:
        created = _docker(
            "network",
            "create",
            "--driver=bridge",
            "--opt=com.docker.network.bridge.enable_icc=false",
            "--label=dev.horae.network=egress",
            "horae-egress",
        )
        assert created.returncode == 0, created.stderr
    inspected = json.loads(_docker("network", "inspect", "horae-egress").stdout)[0]
    gateway = inspected["IPAM"]["Config"][0]["Gateway"]
    name = f"horae-test-lan-{secrets.token_hex(4)}"
    port = 20000 + secrets.randbelow(20000)
    # On the host's network: the proxy reaches it at the egress gateway (the host's own address
    # there), so no traffic crosses from one bridge to another.
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--network",
        "host",
        "--name",
        name,
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-m",
        "http.server",
        str(port),
        "--bind",
        "0.0.0.0",  # noqa: S104 (the egress gateway is one of the host's addresses)
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 30
        probe = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=1)"
        while _docker("exec", name, "python", "-c", probe).returncode != 0:
            assert time.monotonic() < deadline, "the LAN server did not start"
            time.sleep(0.2)
        yield gateway, port
    finally:
        _docker("rm", "--force", name)


def test_egress_goes_through_the_proxy_and_only_where_the_lock_allows(
    tmp_path: Path, plugin_dir: Path, lan_server: tuple[str, int]
) -> None:
    gateway, port = lan_server
    network = {"network": ["api.example.test"], "network_local": [f"{gateway}:{port}"]}
    name = "jail-egress"

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, name, network=network) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return await h.call(f"{name}.{tool}", params, None)

            seen: dict[str, Any] = {
                "granted": await tool("tunnel", host=gateway, port=port),
                "plain_http": await tool("fetch", url=f"http://{gateway}:{port}/"),
                "undeclared": await tool("tunnel", host="undeclared.example.net", port=443),
                "ungranted_port": await tool("tunnel", host=gateway, port=22),
                "ip_literal": await tool("tunnel", host="192.0.2.1", port=443),
                "declared": await tool("tunnel", host="api.example.test", port=443),
                "direct": await tool("connect", host=gateway, port=port),
            }
            listed = _docker("ps", "--quiet", "--filter", f"label=dev.horae.egress-proxy={name}")
            ids = listed.stdout.split()
            assert len(ids) == 1, listed
            seen["inspect"] = json.loads(_docker("inspect", ids[0]).stdout)[0]
            return seen

    seen = asyncio.run(go())
    granted = seen["granted"]
    assert granted.data["status"] == "HTTP/1.1 200 Connection established", granted.data
    assert granted.data["answer"].startswith("HTTP/1.0 200")
    assert granted.violations == ()
    assert seen["plain_http"].data["status"] == 200, seen["plain_http"].data
    undeclared = seen["undeclared"]
    assert undeclared.data["status"] == "HTTP/1.1 403 Forbidden"
    assert [v.detail for v in undeclared.violations] == [
        "jail-egress: egress blocked to undeclared.example.net:443 (host_not_allowed)"
    ]
    assert seen["ungranted_port"].data["status"] == "HTTP/1.1 403 Forbidden"
    assert [v.detail for v in seen["ungranted_port"].violations] == [
        f"jail-egress: egress blocked to {gateway}:22 (ip_literal)"
    ]
    assert [v.detail for v in seen["ip_literal"].violations] == [
        "jail-egress: egress blocked to 192.0.2.1:443 (ip_literal)"
    ]
    # Declared: past the allow-list, then refused by DNS (`.test` never resolves), recorded.
    assert seen["declared"].data["status"] == "HTTP/1.1 502 Bad Gateway"
    assert [v.detail for v in seen["declared"].violations] == [
        "jail-egress: egress blocked to api.example.test:443 (dns_failed)"
    ]
    assert seen["direct"].data["connected"] is False  # no route but the proxy

    data = seen["inspect"]
    config, host = data["Config"], data["HostConfig"]
    assert config["User"] == "65534:65534"
    assert config["Labels"]["dev.horae.egress-proxy"] == name
    assert "dev.horae.plugin" not in config["Labels"]
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert host["Privileged"] is False
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["LogConfig"]["Type"] == "none"
    assert host["PidsLimit"] == 64
    assert host["Sysctls"] == {"net.ipv4.ip_forward": "0"}
    assert data["Mounts"] == []
    assert TOKEN not in json.dumps(data)
    assert set(data["NetworkSettings"]["Networks"]) == {f"horae-plugin-{name}", "horae-egress"}

    deadline = time.monotonic() + 30
    while _docker(
        "ps", "--all", "--quiet", "--filter", f"label=dev.horae.egress-proxy={name}"
    ).stdout.strip():
        assert time.monotonic() < deadline, "the proxy outlived the run"
        time.sleep(0.2)


def test_a_sibling_run_cannot_use_another_runs_proxy(
    tmp_path: Path, plugin_dir: Path, lan_server: tuple[str, int]
) -> None:
    """Two concurrent runs of one plugin share its network: each proxy serves its own plugin
    container only (decision 76 C), so a sibling's tunnel through it gets no answer at all."""
    gateway, port = lan_server
    network = {"network_local": [f"{gateway}:{port}"]}
    name = "jail-egress"

    async def go() -> tuple[Any, Any, Any]:
        first = _host(tmp_path / "a", plugin_dir, name, network=network)
        second = _host(tmp_path / "b", plugin_dir, name, network=network)
        async with first as a, second as b:
            env_a = (await a.call(f"{name}.env", {}, None)).data["env"]
            own = await b.call(f"{name}.tunnel", {"host": gateway, "port": port}, None)
            stolen = await b.call(
                f"{name}.tunnel",
                {"host": gateway, "port": port, "via": env_a["HTTPS_PROXY"]},
                None,
            )
            mine = await a.call(f"{name}.tunnel", {"host": gateway, "port": port}, None)
            return own, stolen, mine

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    own, stolen, mine = asyncio.run(go())
    assert own.data["status"] == "HTTP/1.1 200 Connection established", own.data
    assert mine.data["status"] == "HTTP/1.1 200 Connection established", mine.data
    assert not stolen.data["status"], stolen.data  # dropped: no status line
    assert stolen.data["answer"] == ""
