# SPDX-License-Identifier: Apache-2.0
"""The malicious-plugin suite, container runtime (#52): what only `runtime: container` enforces.

Marked `malicious` and `container`; skipped when no Docker CLI on the base `PATH` reaches a
daemon or the plugin image cannot be pulled (`HORAE_SKIP_CONTAINER_TESTS=1` skips it anyway).
Each fixture is `tests/fixtures/malicious/jailbreak.py` under its own manifest, listing only the
attacks of its guarantee (`malicious_attacks.py`): `evil-network` (T05, T07, T42, T48),
`evil-secrets` (T03, T09, T43 from inside and from the host, §7.3), `evil-paths` (T04, T51,
T53), `evil-resources` (T08, T45, T52 with a CPU spin, #193: `ptrace` of the launcher and a
forged `OOM_EXIT`). Every test asserts *how* the attack failed: the proxy's status and the
recorded `egress_blocked` violation with its reason, the error the kernel gave, the redaction
marker and `secret_in_output`, the sweep's undoing, the quarantine, the cgroup's throttling, the
error kind.

Synthetic names only (`.test`, `.example`); addresses are a documentation one (192.0.2.1), the
cloud metadata one (never reached: refused before any connection), the egress network's
gateway and the default bridge network's gateway, where a local HTTP server (and on the latter
a DNS server) is published so that the direct probes target something an open network reaches,
and a container on the egress network whose alias stands for a public name that resolves to a
private address (DNS rebinding). Nothing reaches the public internet.
"""

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import PluginManifest
from horae.contracts.instance import (
    DEFAULT_CONTAINER_IMAGE,
    ContainerConfig,
    ContainerLimits,
    PluginDefaults,
)
from horae.plugins.host import HostRegistry, PluginCallError, PluginHost
from horae.plugins.host import egress as egress_module
from horae.plugins.host.container import OOM_EXIT
from horae.plugins.host.env import BASE_PATH, PLUGIN_KEYS, PROXY_KEYS
from horae.plugins.host.mounts import MountPolicy
from horae.secrets import SecretResolver
from plugin_fixtures import lock_for, secrets_dir

pytestmark = [pytest.mark.malicious, pytest.mark.container]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "malicious"
EVIL_TOKEN = "sentinel-value-for-evil-plugin"  # noqa: S105 (synthetic)
SIBLING_TOKEN = "sentinel-value-for-sibling-plugin"  # noqa: S105 (synthetic)
CORE_KEY = "sentinel-value-for-the-core-act-key"  # synthetic, in Horae's own environment
REBIND = "rebind.horae.test"
"""A "public" name the egress network's DNS answers with a private address."""
SUFFIX = secrets.token_hex(3)
"""Every name this module gives Docker carries it, so concurrent runs never collide."""
NETWORK, SIBLING, SECRETS, PATHS, RESOURCES = (
    f"evil-{kind}-{SUFFIX}" for kind in ("network", "sibling", "secrets", "paths", "resources")
)
PLUGINS = (NETWORK, SIBLING, SECRETS, PATHS, RESOURCES)
EGRESS = f"horae-egress-test-{SUFFIX}"
"""This module's own egress network (`horae.plugins.host.egress.EGRESS_NETWORK` is set to it
for the module): the real `horae-egress` is never touched, nor joined by a test container."""


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker", path=BASE_PATH)
    assert docker is not None
    return subprocess.run(  # noqa: S603 (the Docker CLI, fixed arguments)
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _gone(label: str) -> None:
    """Wait until no container (running or not) has `label`."""
    deadline = time.monotonic() + 30
    while _docker("ps", "--all", "--quiet", "--filter", f"label={label}").stdout.strip():
        assert time.monotonic() < deadline, f"a container with {label} outlived its run"
        time.sleep(0.2)


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
    created = _docker(
        "network",
        "create",
        "--driver=bridge",
        "--opt=com.docker.network.bridge.enable_icc=false",
        "--label=dev.horae.network=egress",
        EGRESS,
    )
    assert created.returncode == 0, created.stderr
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(egress_module, "EGRESS_NETWORK", EGRESS)
        try:
            yield
        finally:
            for name in PLUGINS:
                _docker("network", "rm", f"horae-plugin-{name}")
            _docker("network", "rm", EGRESS)


@pytest.fixture(scope="module")
def plugin_dir(engine: None, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A plugin directory whose venv the image's interpreter built, holding the fixture."""
    root = tmp_path_factory.mktemp("jailbreak-plugin")
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
    for name in ("jailbreak.py", "malicious_attacks.py"):
        shutil.copy(FIXTURES / name, site / name)
    return root


def _manifest(
    name: str,
    tools: tuple[str, ...],
    secret: str | None = None,
    network: dict[str, list[str]] | None = None,
    paths: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    permissions: dict[str, Any] = {"secrets": [secret] if secret else []}
    permissions |= network or {}
    if paths:
        permissions["paths"] = paths
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0",
        "provides": {"sources": [f"{name}.{tool}" for tool in tools]},
        "permissions": permissions,
        "runtime": {"protocol": "mcp-stdio", "command": ["python", "-m", "jailbreak", name]},
    }


def _host(
    tmp_path: Path,
    plugin_dir: Path,
    *manifests: dict[str, Any],
    mounts: MountPolicy | None = None,
    limits: ContainerLimits | None = None,
) -> PluginHost:
    registry = HostRegistry(
        lock_for(*manifests),
        {m["name"]: PluginManifest.model_validate(m) for m in manifests},
        {m["name"]: plugin_dir for m in manifests},
    )
    values = {"EVIL_TOKEN": EVIL_TOKEN, "SIBLING_TOKEN": SIBLING_TOKEN}
    resolver = SecretResolver({}, secrets_dir(tmp_path, **values))
    defaults = PluginDefaults(
        runtime="container",
        startup_timeout_s=120.0,
        call_timeout_s=60.0,
        limits=limits or ContainerLimits(),
    )
    return PluginHost(
        registry,
        secrets=resolver,
        work_dir=tmp_path / "work",
        timeouts=lambda _name: defaults,
        container=ContainerConfig(image=DEFAULT_CONTAINER_IMAGE),
        mounts=mounts,
    )


def _details(result: Any) -> list[str]:
    return [violation.detail for violation in result.violations]


# --- network: T05, T07, T42, T48 ------------------------------------------------------------------


@contextlib.contextmanager
def _rebinding() -> Iterator[None]:
    """While it lasts, Docker's DNS on the egress network answers `REBIND` with a private
    address: a container joins the network under that alias. Started only once the plugin and
    its proxy run, since Horae refuses an egress network holding anything but its proxies
    (and removed before the next plugin starts)."""
    name = f"horae-test-evil-rebind-{secrets.token_hex(4)}"
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--network",
        EGRESS,
        "--network-alias",
        REBIND,
        "--name",
        name,
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-c",
        "import time; time.sleep(600)",
    )
    assert started.returncode == 0, started.stderr
    try:
        yield
    finally:
        _docker("rm", "--force", name)


LAN_SERVER = """
import http.server, socket, sys, threading
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://undeclared.example.net/")
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *args):
        pass
def dns(address, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((address, port))
    while True:
        query, peer = sock.recvfrom(512)
        head = query[:2] + bytes([0x81, 0x80]) + query[4:6] + bytes([0, 1, 0, 0, 0, 0])
        record = bytes([0xC0, 12, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4]) + socket.inet_aton(sys.argv[4])
        sock.sendto(head + query[12:] + record, peer)
gateway, port, outside = sys.argv[1], int(sys.argv[2]), sys.argv[3]
threading.Thread(target=dns, args=(outside, port), daemon=True).start()
serve = http.server.HTTPServer((outside, port), Handler).serve_forever
threading.Thread(target=serve, daemon=True).start()
http.server.HTTPServer((gateway, port), Handler).serve_forever()
"""
"""A local server: `200` at `/`, a redirect to an undeclared host at `/redirect`; on the
egress network's gateway (the grant) and on the default bridge's (`outside`: an address no
grant names), where a DNS server on the same port (UDP) answers every name with `ANSWER`."""
ANSWER = "192.0.2.10"


@dataclass(frozen=True)
class Lan:
    gateway: str
    """The egress network's gateway, where the granted endpoint listens."""
    port: int
    outside: str
    """The default bridge network's gateway: the same server and a DNS server, reachable from
    any container on an open network, granted to none."""


@pytest.fixture(scope="module")
def egress(engine: None) -> Iterator[Lan]:
    """A local HTTP server on the Docker host, bound to this module's egress network's
    gateway (the host's own address there), where the proxy reaches it (`gateway:port`), and
    to the default bridge's gateway with a DNS server (`outside:port`)."""
    inspected = json.loads(_docker("network", "inspect", EGRESS).stdout)[0]
    gateway = inspected["IPAM"]["Config"][0]["Gateway"]
    bridge = json.loads(_docker("network", "inspect", "bridge").stdout)[0]
    outside = bridge["IPAM"]["Config"][0]["Gateway"]
    lan = f"horae-test-evil-lan-{SUFFIX}"
    port = 20000 + secrets.randbelow(20000)
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--network",
        "host",
        "--name",
        lan,
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-c",
        LAN_SERVER,
        gateway,
        str(port),
        outside,
        ANSWER,
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 30
        probe = (
            "import socket\n"
            f"for host in ('{gateway}', '{outside}'):\n"
            f"    socket.create_connection((host, {port}), timeout=1)"
        )
        while _docker("exec", lan, "python", "-c", probe).returncode != 0:
            assert time.monotonic() < deadline, "the LAN server did not start"
            time.sleep(0.2)
        yield Lan(gateway, port, outside)
    finally:
        _docker("rm", "--force", lan)


def _from_an_open_network(lan: Lan) -> dict[str, Any]:
    """The positive control of the direct probes: the same attacks from a container on the
    default bridge network (open, as a plugin's network would be without `--internal`)."""
    code = (
        "import json, sys\n"
        "sys.path.insert(0, '/attacks')\n"
        "import malicious_attacks as a\n"
        f"print(json.dumps({{'connect': a.connect('{lan.outside}', {lan.port}), "
        f"'dns': a.dns('{lan.outside}', {lan.port}, 'api.example.test')}}))"
    )
    done = _docker(
        "run",
        "--rm",
        "--network",
        "bridge",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={FIXTURES},target=/attacks,readonly",
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-B",
        "-c",
        code,
    )
    assert done.returncode == 0, done.stderr
    seen: dict[str, Any] = json.loads(done.stdout)
    return seen


NETWORK_TOOLS = ("env", "connect", "resolve", "dns", "raw", "tunnel", "http", "fetch")
ENETUNREACH = 101
EAI_AGAIN = -3
"""Linux's `errno` for no route and glibc's `getaddrinfo` code for a temporary failure, as the
image reports them (the host's own constants may differ: macOS)."""


def test_evil_network_reaches_nothing_but_its_grants_and_every_attempt_is_recorded(
    tmp_path: Path, plugin_dir: Path, egress: Lan
) -> None:
    gateway, port, outside = egress.gateway, egress.port, egress.outside
    local = f"{gateway}:{port}"
    network = {"network": ["api.example.test", REBIND], "network_local": [local]}
    name = NETWORK
    manifest = _manifest(name, NETWORK_TOOLS, network=network)

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return await h.call(f"{name}.{tool}", params, None)

            seen = {
                # Positive control: the one grant works, through the proxy.
                "granted": await tool("tunnel", host=gateway, port=port),
                # Around the proxy, to servers an open network reaches (the control below):
                # no route out of the internal network, no DNS but Docker's, which does not
                # forward from an internal network.
                "direct_grant": await tool("connect", host=gateway, port=port),
                "direct_out": await tool("connect", host=outside, port=port),
                "own_dns": await tool("dns", server=outside, port=port, name="api.example.test"),
                "resolve": await tool("resolve", name="api.example.test"),
                # Through the proxy, outside the grants.
                "undeclared": await tool("tunnel", host="undeclared.example.net", port=443),
                "other_port": await tool("tunnel", host="api.example.test", port=8443),
                "plain_http": await tool("http", url="http://api.example.test/"),
                "ip_literal": await tool("tunnel", host="192.0.2.1", port=443),
                "grant_other_port": await tool("tunnel", host=gateway, port=port + 1),
                "metadata": await tool("tunnel", host="169.254.169.254", port=80),
                "metadata_http": await tool("http", url="http://169.254.169.254/latest/meta-data/"),
                "loopback": await tool("tunnel", host="127.0.0.1", port=3128),
                # Smuggling a second host in one request.
                "host_header": await tool(
                    "http", url="http://undeclared.example.net/", host_header=local
                ),
                "userinfo_connect": await tool(
                    "raw",
                    request=(
                        "CONNECT api.example.test@undeclared.example.net:443 HTTP/1.1\n"
                        "Host: api.example.test:443\n\n"
                    ),
                ),
                "userinfo_http": await tool(
                    "http", url=f"http://{local}@undeclared.example.net/", host_header=local
                ),
            }
            # A redirect from the granted endpoint to an undeclared host: a standard client
            # follows it with a new request, which the proxy checks again (T07).
            seen["redirect"] = await tool("fetch", url=f"http://{local}/redirect")
            with _rebinding():
                seen["rebinding"] = await tool("tunnel", host=REBIND, port=443)
            return seen

    seen = asyncio.run(go())
    granted = seen["granted"]
    assert granted.data["status"] == "HTTP/1.1 200 Connection established", granted.data
    assert granted.data["answer"].startswith("HTTP/1.0 200")
    assert granted.violations == ()
    # From an open network the same probes succeed, so their failure below is the
    # plugin network's doing, not an unreachable target.
    control = _from_an_open_network(egress)
    assert control["connect"]["connected"] is True, control
    assert control["dns"]["answers"] == [ANSWER], control
    for key in ("direct_grant", "direct_out"):
        assert seen[key].data["connected"] is False, key
        assert seen[key].data["errno"] == ENETUNREACH, (key, seen[key].data)
    assert seen["own_dns"].data["answers"] == []
    assert seen["own_dns"].data["errno"] == ENETUNREACH, seen["own_dns"].data
    assert seen["resolve"].data["resolved"] is False
    assert seen["resolve"].data["errno"] == EAI_AGAIN, seen["resolve"].data
    for key in ("direct_grant", "direct_out", "own_dns", "resolve"):
        assert seen[key].violations == (), key  # never reached the proxy: nothing to record

    def refused(key: str, status: str, host: str, port_: int, reason: str) -> None:
        result = seen[key]
        assert result.data["status"] == f"HTTP/1.1 {status}", (key, result.data)
        assert result.data["answer"] == ""
        assert _details(result) == [f"{name}: egress blocked to {host}:{port_} ({reason})"], key

    refused("undeclared", "403 Forbidden", "undeclared.example.net", 443, "host_not_allowed")
    refused("other_port", "403 Forbidden", "api.example.test", 8443, "port_not_allowed")
    refused("plain_http", "403 Forbidden", "api.example.test", 80, "plain_http_not_allowed")
    refused("ip_literal", "403 Forbidden", "192.0.2.1", 443, "ip_literal")
    refused("grant_other_port", "403 Forbidden", gateway, port + 1, "ip_literal")
    refused("metadata", "403 Forbidden", "169.254.169.254", 80, "ip_literal")
    refused("metadata_http", "403 Forbidden", "169.254.169.254", 80, "ip_literal")
    refused("loopback", "403 Forbidden", "127.0.0.1", 3128, "ip_literal")
    redirect = seen["redirect"]
    assert redirect.data["status"] == 403, redirect.data  # the redirect's target, refused
    assert redirect.data["url"] == "http://undeclared.example.net/"
    assert _details(redirect) == [
        f"{name}: egress blocked to undeclared.example.net:80 (host_not_allowed)"
    ]
    refused("rebinding", "403 Forbidden", REBIND, 443, "private_address")
    # The decision is taken on the request target, never on a header.
    refused("host_header", "403 Forbidden", "undeclared.example.net", 80, "host_not_allowed")
    for key in ("userinfo_connect", "userinfo_http"):
        result = seen[key]
        assert result.data["status"] == "HTTP/1.1 400 Bad Request", (key, result.data)
        assert _details(result) == [
            f"{name}: egress refused, unreadable target (malformed_target)"
        ], key
    _gone(f"dev.horae.egress-proxy={name}")


def test_evil_network_cannot_borrow_a_sibling_plugins_proxy(
    tmp_path: Path, plugin_dir: Path, egress: Lan
) -> None:
    """Another plugin's proxy holds another allow-list: it is on that plugin's network only,
    so the attacker cannot even reach it (and it serves its own container only, T48)."""
    gateway, port = egress.gateway, egress.port
    evil = _manifest(NETWORK, NETWORK_TOOLS)
    sibling = _manifest(SIBLING, NETWORK_TOOLS, network={"network_local": [f"{gateway}:{port}"]})

    async def go() -> tuple[Any, Any, Any]:
        async with _host(tmp_path, plugin_dir, evil, sibling) as h:
            env = (await h.call(f"{SIBLING}.env", {}, None)).data["env"]
            own = await h.call(f"{SIBLING}.tunnel", {"host": gateway, "port": port}, None)
            stolen = await h.call(
                f"{NETWORK}.tunnel",
                {"host": gateway, "port": port, "via": env["HTTPS_PROXY"]},
                None,
            )
            mine = await h.call(f"{NETWORK}.tunnel", {"host": gateway, "port": port}, None)
            return own, stolen, mine

    own, stolen, mine = asyncio.run(go())
    assert own.data["status"] == "HTTP/1.1 200 Connection established", own.data  # control
    assert stolen.data["status"] == "", stolen.data  # no route to the sibling's proxy
    # A connection-level failure: the sibling's proxy is on a network this one is not on.
    assert re.search(
        r"timed out|No route to host|Connection refused|Network is unreachable",
        stolen.data["error"],
    ), stolen.data
    assert stolen.violations == ()
    assert mine.data["status"] == "HTTP/1.1 403 Forbidden"  # its own proxy: not granted
    assert _details(mine) == [f"{NETWORK}: egress blocked to {gateway}:{port} (ip_literal)"]


# --- secrets: T03, T09, T43 -----------------------------------------------------------------------


def test_evil_secrets_finds_no_secret_it_did_not_declare(
    tmp_path: Path, plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another plugin's secret travels in its own container's preamble, the core's in Horae's
    environment and secrets directory: none is in this container's environment, files or
    `/proc`. Its own secret comes back redacted, flagged `secret_in_output`."""
    monkeypatch.setenv("HORAE_ACT_KEY", CORE_KEY)
    monkeypatch.setenv("SIBLING_TOKEN", SIBLING_TOKEN)
    evil = _manifest(SECRETS, ("env", "read", "proc"), secret="EVIL_TOKEN")  # noqa: S106 (a name)
    sibling = _manifest(SIBLING, ("env",), secret="SIBLING_TOKEN")  # noqa: S106 (a name)
    secrets_path = tmp_path / "secrets"

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, evil, sibling) as h:
            await h.call(f"{SIBLING}.env", {}, None)  # running, its preamble delivered

            async def tool(tool: str, **params: Any) -> Any:
                return await h.call(f"{SECRETS}.{tool}", params, None)

            return {
                "env": await tool("env"),
                "proc": await tool("proc"),
                "file": await tool("read", path=str(secrets_path / "SIBLING_TOKEN")),
                "run_secrets": await tool("read", path="/run/secrets/SIBLING_TOKEN"),
                "host_environ": await tool("read", path="/proc/1/environ"),
            }

    seen = asyncio.run(go())
    env = seen["env"].data["env"]
    assert set(env) == PLUGIN_KEYS | {"VIRTUAL_ENV", "EVIL_TOKEN", *PROXY_KEYS}
    assert env["EVIL_TOKEN"] == "[REDACTED:EVIL_TOKEN]"  # noqa: S105 (a redaction marker)
    assert _details(seen["env"]) == [
        f"{SECRETS}.env returned secret EVIL_TOKEN in its output (redacted)"
    ]
    proc = seen["proc"]
    assert len(proc.data["pids"]) <= 3, proc.data["pids"]  # docker-init and itself
    dumped = json.dumps(proc.data)
    for value in (SIBLING_TOKEN, CORE_KEY, EVIL_TOKEN):
        assert value not in dumped
    assert "SIBLING_TOKEN" not in dumped
    assert "HORAE_ACT_KEY" not in dumped
    for violation in proc.violations:  # at most its own secret, read from its own /proc
        assert violation.kind == "secret_in_output"
        assert "SIBLING_TOKEN" not in violation.detail
    for key in ("file", "run_secrets"):
        assert seen[key].data == {"read": None, "error": "No such file or directory"}, key
        assert seen[key].violations == ()
    assert SIBLING_TOKEN not in json.dumps(seen["host_environ"].data)
    assert CORE_KEY not in json.dumps(seen["host_environ"].data)


def _cli_processes(label: str) -> dict[int, str]:
    """The host's processes whose command line holds `label`, by pid: the Docker CLI that
    runs the container (and nothing else names its label)."""
    listed = subprocess.run(
        ["/bin/ps", "-ww", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = {}
    for line in listed.splitlines():
        pid, _, args = line.strip().partition(" ")
        if label in args:
            found[int(pid)] = args
    return found


def test_evil_secrets_its_secret_is_in_no_host_side_view(tmp_path: Path, plugin_dir: Path) -> None:
    """T43 from the host: while the plugin runs, having written its secret to its stderr,
    the secret is in neither its container's nor its proxy's configuration (`docker
    inspect`: `Env`, `Args`, `Cmd`, `Entrypoint`, `Labels`, anywhere), nor in any command line
    on the host (`ps`), nor, on Linux, in the Docker CLI's environment; and the daemon kept no
    log of the container's output (`--log-driver none`: `docker logs` has nothing to read). Its
    stderr reached Horae alone, redacted."""
    name = SECRETS
    manifest = _manifest(name, ("env", "shout"), secret="EVIL_TOKEN")  # noqa: S106 (a name)

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest) as h:
            shouted = await h.call(f"{name}.shout", {"key": "EVIL_TOKEN"}, None)
            label = f"dev.horae.plugin={name}"
            (plugin,) = _docker("ps", "--quiet", "--filter", f"label={label}").stdout.split()
            proxy = _docker("ps", "--quiet", "--filter", f"label=dev.horae.egress-proxy={name}")
            clis = _cli_processes(label)
            environs = {}
            for pid in clis:
                with contextlib.suppress(OSError):  # Linux only
                    environs[pid] = Path(f"/proc/{pid}/environ").read_bytes()
            return {
                "shouted": shouted,
                "inspect": _docker("inspect", plugin, *proxy.stdout.split()),
                "logs": _docker("logs", plugin),
                "ps": clis,
                "environs": environs,
            }

    seen = asyncio.run(go())
    inspect = seen["inspect"]
    assert inspect.returncode == 0, inspect.stderr
    assert EVIL_TOKEN not in inspect.stdout
    containers = json.loads(inspect.stdout)
    assert len(containers) == 2  # the plugin and its egress proxy
    for container in containers:
        config = container["Config"]
        for value in (config["Env"], container["Args"], config["Cmd"], config["Labels"]):
            assert "EVIL_TOKEN" not in json.dumps(value)
        assert config["Entrypoint"] is None or EVIL_TOKEN not in json.dumps(config["Entrypoint"])
    plugin = next(c for c in containers if c["Config"]["Labels"].get("dev.horae.plugin"))
    assert plugin["HostConfig"]["LogConfig"]["Type"] == "none"
    logs = seen["logs"]
    assert logs.returncode != 0
    assert "does not support reading" in logs.stderr, logs.stderr
    assert EVIL_TOKEN not in logs.stdout + logs.stderr
    clis = seen["ps"]
    assert clis, "the Docker CLI running the plugin was not found"
    assert all("--log-driver none" in args for args in clis.values()), clis
    assert EVIL_TOKEN not in json.dumps(clis)
    for environ in seen["environs"].values():
        assert EVIL_TOKEN.encode() not in environ
        assert b"EVIL_TOKEN" not in environ
    shouted = seen["shouted"]
    assert EVIL_TOKEN not in (shouted.stderr or "")
    assert "[REDACTED:EVIL_TOKEN]" in (shouted.stderr or ""), shouted.stderr
    _gone(f"dev.horae.plugin={name}")


# --- paths: T04, T51, T53 -------------------------------------------------------------------------


def _instance(tmp_path: Path, secrets_in: bool = False) -> tuple[Path, MountPolicy]:
    """A synthetic instance: a vault (read-only grant) with an inbox (writable grant), an
    undeclared directory and the instance's own controls (the secrets directory inside it
    with `secrets_in`, so that only the sensitive-path rule can refuse it)."""
    root = (tmp_path / "instance").resolve()
    for directory in ("vault/inbox", "undeclared", "state", "plugins", "secrets"):
        (root / directory).mkdir(parents=True)
    (root / "vault" / "note.md").write_text("vault note\n", encoding="utf-8")
    (root / "undeclared" / "private.md").write_text("not for plugins\n", encoding="utf-8")
    (root / "horae.lock").write_text("schema_version: 1\n", encoding="utf-8")
    (root / "horae.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    policy = MountPolicy.for_instance(
        root,
        state_dir=root / "state",
        secrets_dir=root / "secrets" if secrets_in else tmp_path / "secrets",
        home=tmp_path / "home",
    )
    return root, policy


PATH_TOOLS = ("env", "fs", "read", "write", "plant", "link", "hook", "caps")


def test_evil_paths_sees_only_its_grants_and_its_traps_are_undone(
    tmp_path: Path, plugin_dir: Path
) -> None:
    root, policy = _instance(tmp_path)
    vault, inbox = root / "vault", root / "vault" / "inbox"
    name = PATHS
    paths = {"read": [str(vault)], "write": [str(inbox)]}
    manifest = _manifest(name, PATH_TOOLS, paths=paths)
    private = root / "undeclared" / "private.md"

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest, mounts=policy) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return (await h.call(f"{name}.{tool}", params, None)).data

            seen = {
                "undeclared": await tool("fs", path=str(private)),
                "lock": await tool("fs", path=str(root / "horae.lock")),
                "config": await tool("fs", path=str(root / "horae.yaml")),
                "state": await tool("fs", path=str(root / "state")),
                "secrets": await tool("fs", path=str(tmp_path / "secrets")),
                "dotdot": await tool("read", path=f"{inbox}/../../undeclared/private.md"),
                "read_only_file": await tool("write", path=str(vault / "note.md")),
                "read_only_dir": await tool("write", path=str(vault / "new.md")),
                # World-writable in the image (1777): only the read-only root stops it.
                "root_fs": await tool("write", path="/var/tmp/evil"),  # noqa: S108
                "caps": await tool("caps"),
                "planted": await tool(
                    "plant", path=str(inbox), outside=str(private), source=str(vault / "note.md")
                ),
            }
            seen["through_link"] = await tool("read", path=str(inbox / "escape"))
            seen["during"] = sorted(os.listdir(inbox))
            return seen

    seen = asyncio.run(go())
    for key in ("undeclared", "lock", "config", "state", "secrets"):
        assert seen[key]["exists"] is False, key
    assert seen["dotdot"] == {"read": None, "error": "No such file or directory"}
    assert seen["read_only_file"] == {"written": "Read-only file system"}
    assert seen["read_only_dir"] == {"written": "Read-only file system"}
    assert seen["root_fs"] == {"written": "Read-only file system"}
    # Non-root has no effective capability anyway; `--cap-drop ALL` also empties the bounding
    # set, which is what a set-user-ID or file-capability program could otherwise gain.
    caps = seen["caps"]
    assert {key: int(caps[key], 16) for key in ("CapPrm", "CapEff", "CapBnd", "CapAmb")} == {
        "CapPrm": 0,
        "CapEff": 0,
        "CapBnd": 0,
        "CapAmb": 0,
    }
    assert caps["NoNewPrivs"] == "1"
    assert (vault / "note.md").read_text(encoding="utf-8") == "vault note\n"
    assert not (vault / "new.md").exists()
    planted = seen["planted"]
    # A hard link across mounts is refused by the kernel; a write through `..` lands outside
    # every mount (the read-only root filesystem); a device needs CAP_MKNOD.
    assert planted["hardlink"] == "Invalid cross-device link"
    # (the parents of a mount point exist on the read-only root filesystem, created by the
    # engine as root: which error comes first depends on the engine)
    assert planted["dotdot_write"] in (
        "Read-only file system",
        "No such file or directory",
        "Permission denied",
    )
    assert not (root / "evil-dotdot.md").exists()
    assert planted["device"] == "Operation not permitted"
    # What the kernel allowed inside the writable path, the link resolves to nothing there...
    assert planted["symlink"] is True
    assert seen["through_link"]["read"] is None
    assert {"escape", "dotdot", "fifo", "setuid"} <= set(seen["during"])
    # ...and once the container is gone, the sweep undoes every trap left for the host.
    for trap in ("escape", "dotdot", "fifo", "hardlink", "null"):
        assert not os.path.lexists(inbox / trap), trap
    assert planted["setuid"] is True  # the bits stuck in the container...
    assert not os.stat(inbox / "setuid").st_mode & 0o6000  # ...and the sweep cleared them
    assert private.read_text(encoding="utf-8") == "not for plugins\n"
    assert not (root / "state" / "quarantine" / name).exists()  # a clean sweep
    _gone(f"dev.horae.plugin={name}")


def test_evil_paths_a_link_into_a_read_only_grant_gains_nothing_and_is_swept(
    tmp_path: Path, plugin_dir: Path
) -> None:
    """T53, a link planted in the writable grant that points into the read-only one (the
    absolute path, `..`, the vault directory itself, and a chain whose first hop has no `..`).
    Inside the container it is harmless: the plugin could read the vault anyway, and a write
    through any of them meets the read-only mount. After the run the sweep removes each one,
    since it resolves where the plugin may not write (a later reader on the host, a process
    plugin or Obsidian, would otherwise follow it); a link inside the writable grant stays, and
    nothing left resolves outside it."""
    root, policy = _instance(tmp_path)
    vault, inbox = root / "vault", root / "vault" / "inbox"
    name = PATHS
    manifest = _manifest(name, PATH_TOOLS, paths={"read": [str(vault)], "write": [str(inbox)]})
    links = {
        "absolute": str(vault / "note.md"),
        "dotdot": "../note.md",
        "directory": str(vault),
        "chain": "hop",
        "hop": "../note.md",
        "inside": "data.md",  # control: stays in the writable grant
    }

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest, mounts=policy) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return (await h.call(f"{name}.{tool}", params, None)).data

            await tool("write", path=str(inbox / "data.md"), text="mine\n")
            seen: dict[str, Any] = {
                key: await tool("link", path=str(inbox / key), target=target)
                for key, target in links.items()
            }
            seen["read"] = await tool("read", path=str(inbox / "absolute"))
            for key in ("absolute", "dotdot", "chain"):
                seen[f"write_{key}"] = await tool("write", path=str(inbox / key))
            seen["write_directory"] = await tool("write", path=str(inbox / "directory" / "x.md"))
            seen["write_inside"] = await tool("write", path=str(inbox / "inside"))
            return seen

    seen = asyncio.run(go())
    for key in links:
        assert seen[key] == {"linked": True}, key  # the kernel lets it make them
    assert seen["read"]["read"] == "vault note\n"  # what its read grant already gives it
    for key in ("absolute", "dotdot", "chain", "directory"):
        assert seen[f"write_{key}"] == {"written": "Read-only file system"}, key
    assert seen["write_inside"] == {"written": True}
    assert (vault / "note.md").read_text(encoding="utf-8") == "vault note\n"
    assert not (vault / "x.md").exists()
    for key in ("absolute", "dotdot", "directory", "hop"):
        assert not os.path.lexists(inbox / key), key  # swept
    # The chain's first hop has no `..`: the sweep resolves it. Walked before `hop`, it
    # resolves into the vault and goes; walked after, `hop` is gone and it names a missing
    # file inside the writable grant, which is what it then is (scandir order decides).
    # Either way nothing left resolves outside the writable grant.
    for entry in os.scandir(inbox):
        if entry.is_symlink():
            assert Path(os.path.realpath(entry.path)).is_relative_to(inbox), entry.name
    assert os.readlink(inbox / "inside") == "data.md"  # kept: it stays in the grant
    assert not (root / "state" / "quarantine" / name).exists()  # a clean sweep
    _gone(f"dev.horae.plugin={name}")


@pytest.mark.parametrize(
    ("access", "target"),
    [
        ("read", "horae.lock"),
        ("write", "state"),
        ("read", "secrets"),
        ("read", ""),  # the instance itself: an ancestor of every sensitive entry
    ],
)
def test_evil_paths_asking_for_a_sensitive_path_never_starts(
    tmp_path: Path, plugin_dir: Path, access: str, target: str
) -> None:
    """The lock grants it (an approved manifest asked), yet a path that is, holds or lies in
    the instance's controls, state or secrets is never mounted: the plugin does not start.
    Everything here lies in the instance, so only the sensitive-path rule can refuse it."""
    root, policy = _instance(tmp_path, secrets_in=True)
    path = str(root / target) if target else str(root)
    name = PATHS
    manifest = _manifest(name, PATH_TOOLS, paths={access: [path]})

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin_dir, manifest, mounts=policy) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call(f"{name}.env", {}, None)
            return info.value

    error = asyncio.run(go())
    assert error.kind == "runtime_unavailable"
    assert "which no plugin may see" in error.message, error.message
    assert not _docker(
        "ps", "--all", "--quiet", "--filter", f"label=dev.horae.plugin={name}"
    ).stdout.strip()  # nothing was started


def test_evil_paths_planting_a_git_hook_quarantines_the_plugin(
    tmp_path: Path, plugin_dir: Path
) -> None:
    """A new `.git` in a writable path is what the sweep cannot vouch for: it fails closed,
    and the plugin's next start is refused until the operator removes the quarantine file."""
    root, policy = _instance(tmp_path)
    inbox = root / "vault" / "inbox"
    name = PATHS
    manifest = _manifest(name, PATH_TOOLS, paths={"write": [str(inbox)]})

    async def plant() -> Any:
        async with _host(tmp_path, plugin_dir, manifest, mounts=policy) as h:
            return (await h.call(f"{name}.hook", {"path": str(inbox)}, None)).data

    async def again() -> PluginCallError:
        async with _host(tmp_path, plugin_dir, manifest, mounts=policy) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call(f"{name}.env", {}, None)
            return info.value

    assert asyncio.run(plant()) == {"git": True}
    marker = root / "state" / "quarantine" / name
    assert marker.is_file()
    assert ".git" in marker.read_text(encoding="utf-8")
    error = asyncio.run(again())
    assert error.kind == "runtime_unavailable"
    assert "quarantined" in error.message
    assert not _docker(
        "ps", "--all", "--quiet", "--filter", f"label=dev.horae.plugin={name}"
    ).stdout.strip()  # nothing was started


# --- resources: T08, T45, T52 ---------------------------------------------------------------------

RESOURCE_TOOLS = ("env", "fork", "fds", "fill", "hog", "daemon", "spin", "ptrace", "die")


def test_evil_resources_hits_every_limit_and_leaves_nothing_behind(
    tmp_path: Path, plugin_dir: Path
) -> None:
    name = RESOURCES
    limits = ContainerLimits(pids=32, nofile=256, tmp_mb=8, memory_mb=128)
    manifest = _manifest(name, RESOURCE_TOOLS)

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest, limits=limits) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return (await h.call(f"{name}.{tool}", params, None)).data

            return {
                "fork": await tool("fork"),
                "fds": await tool("fds"),
                "fill": await tool("fill", mb=64),
                "daemon": await tool("daemon"),
                "alive": await tool("env"),  # still answers after each attack
            }

    seen = asyncio.run(go())
    assert seen["fork"]["forked"] < 32, seen["fork"]
    assert seen["fork"]["error"] == "Resource temporarily unavailable"
    assert seen["fds"]["opened"] < 256, seen["fds"]
    assert seen["fds"]["error"] == "Too many open files"
    assert seen["fill"]["written_mb"] <= 8, seen["fill"]  # the tmpfs holds 8 MiB, no more
    assert seen["fill"]["error"] == "No space left on device"
    assert seen["daemon"]["daemon"] > 0  # it did leave a process, in its own session
    assert "env" in seen["alive"]
    _gone(f"dev.horae.plugin={name}")  # removed with every process it started


def test_evil_resources_memory_hog_is_killed_and_reported(tmp_path: Path, plugin_dir: Path) -> None:
    name = RESOURCES
    manifest = _manifest(name, RESOURCE_TOOLS)
    limits = ContainerLimits(memory_mb=64, tmp_mb=8)

    async def go() -> PluginCallError:
        async with _host(tmp_path, plugin_dir, manifest, limits=limits) as h:
            with pytest.raises(PluginCallError) as info:
                await h.call(f"{name}.hog", {"mb": 512}, None)
            return info.value

    error = asyncio.run(go())
    assert error.kind == "oom_killed", error
    assert "memory limit (64 MiB" in error.message
    _gone(f"dev.horae.plugin={name}")


def test_evil_resources_a_cpu_spin_is_throttled_to_its_cpus(
    tmp_path: Path, plugin_dir: Path
) -> None:
    """T52: two processes spin for 3 s under `cpus: 0.25`. The kernel enforces the quota the
    engine set (`cpu.max`, `NanoCpus`): it throttled the container (`cpu.stat`
    `nr_throttled`), and the CPU time it got is within the quota (with slack for the period
    boundaries; a busy runner only lowers it)."""
    name = RESOURCES
    manifest = _manifest(name, RESOURCE_TOOLS)
    limits = ContainerLimits(cpus=0.25)

    async def go() -> tuple[dict[str, Any], str, Any]:
        async with _host(tmp_path, plugin_dir, manifest, limits=limits) as h:
            spun = (await h.call(f"{name}.spin", {"seconds": 3.0, "workers": 2}, None)).data
            label = f"label=dev.horae.plugin={name}"
            (container,) = _docker("ps", "--quiet", "--filter", label).stdout.split()
            nano = _docker("inspect", "--format", "{{.HostConfig.NanoCpus}}", container)
            return spun, nano.stdout.strip(), (await h.call(f"{name}.env", {}, None)).data

    spun, nano, alive = asyncio.run(go())
    assert spun["error"] is None, spun
    assert nano == "250000000"
    assert spun["cpu_max"] == "25000 100000"
    assert spun["nr_throttled"] > 0, spun
    share = spun["usage_usec"] / (spun["wall_s"] * 1_000_000)
    assert share <= 0.25 * 1.5, spun  # two spinning processes would take 2.0 unthrottled
    assert "env" in alive  # it still answers
    _gone(f"dev.horae.plugin={name}")


def test_evil_resources_cannot_ptrace_its_launcher_nor_forge_an_oom_kill(
    tmp_path: Path, plugin_dir: Path
) -> None:
    """#193, through the real host: the plugin's parent is the launcher, which is not dumpable,
    so `PTRACE_ATTACH` fails with `EPERM` and its environment and memory cannot be read (the
    live check of the launcher alone is `tests/test_plugin_container_live.py`
    `test_the_plugin_cannot_ptrace_its_launcher`). A plugin that exits with the launcher's
    own `OOM_EXIT` (247) is a crash: the launcher passes it on as 1, and only a real kill under
    the memory limit is `oom_killed`; nor is a plain `exit(137)`, which only looks like a
    `SIGKILL`."""
    name = RESOURCES
    manifest = _manifest(name, RESOURCE_TOOLS)

    async def go() -> tuple[Any, list[PluginCallError]]:
        async with _host(tmp_path, plugin_dir, manifest) as h:
            traced = (await h.call(f"{name}.ptrace", {}, None)).data
            crashes = []
            for code in (OOM_EXIT, 137):  # the second start is the plugin's one restart
                with pytest.raises(PluginCallError) as info:
                    await h.call(f"{name}.die", {"code": code}, None)
                crashes.append(info.value)
            return traced, crashes

    traced, crashes = asyncio.run(go())
    assert "horae-launch: " in " ".join(traced["parent_cmdline"])  # the launcher, indeed
    assert traced["attached"] == -1, traced
    assert traced["errno"] == 1, traced  # EPERM
    assert traced["environ"] == "Permission denied", traced
    assert traced["mem"] == "Permission denied", traced
    forged, killed = crashes
    assert forged.kind == "plugin_crashed", forged
    assert "exited with code 1" in forged.message, forged.message
    assert killed.kind == "plugin_crashed", killed
    assert "exited with code 137" in killed.message, killed.message
    _gone(f"dev.horae.plugin={name}")
