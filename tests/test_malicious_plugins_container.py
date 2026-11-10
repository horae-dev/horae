# SPDX-License-Identifier: Apache-2.0
"""The malicious-plugin suite, container runtime (#52): what only `runtime: container` enforces.

Marked `malicious` and `container`; skipped when no Docker CLI on the base `PATH` reaches a
daemon or the plugin image cannot be pulled (`HORAE_SKIP_CONTAINER_TESTS=1` skips it anyway).
Each fixture is `tests/fixtures/malicious/jailbreak.py` under its own manifest, listing only the
attacks of its guarantee (`malicious_attacks.py`): `evil-network` (T05, T07, T42, T48),
`evil-secrets` (T03, T09, T43, §7.3), `evil-paths` (T04, T51, T53), `evil-resources` (T08, T45,
T52). Every test asserts *how* the attack failed: the proxy's status and the recorded
`egress_blocked` violation with its reason, the error the kernel gave, the redaction marker and
`secret_in_output`, the sweep's undoing, the quarantine, the error kind.

Synthetic names only (`.test`, `.example`); addresses are a documentation one (192.0.2.1), the
cloud metadata one (never reached: refused before any connection), the egress network's
gateway, where a local HTTP server is published, and a container on the egress network whose
alias stands for a public name that resolves to a private address (DNS rebinding). Nothing
reaches the public internet.
"""

import asyncio
import contextlib
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
from horae.contracts.instance import (
    DEFAULT_CONTAINER_IMAGE,
    ContainerConfig,
    ContainerLimits,
    PluginDefaults,
)
from horae.plugins.host import HostRegistry, PluginCallError, PluginHost
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
PLUGINS = (
    "evil-network",
    "evil-sibling",
    "evil-secrets",
    "evil-paths",
    "evil-resources",
)


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
    yield
    for name in PLUGINS:
        _docker("network", "rm", f"horae-plugin-{name}")
    _docker("network", "rm", "horae-egress")  # fails harmlessly while another run uses it


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
        "horae-egress",
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


@pytest.fixture(scope="module")
def egress(engine: None) -> Iterator[tuple[str, int]]:
    """The egress network as Horae creates it, and a local HTTP server on the Docker host,
    reached by the proxy at the network's gateway (`gateway:port`)."""
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
    lan = f"horae-test-evil-lan-{secrets.token_hex(4)}"
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
        while _docker("exec", lan, "python", "-c", probe).returncode != 0:
            assert time.monotonic() < deadline, "the LAN server did not start"
            time.sleep(0.2)
        yield gateway, port
    finally:
        _docker("rm", "--force", lan)


NETWORK_TOOLS = ("env", "connect", "resolve", "raw", "tunnel", "http")


def test_evil_network_reaches_nothing_but_its_grants_and_every_attempt_is_recorded(
    tmp_path: Path, plugin_dir: Path, egress: tuple[str, int]
) -> None:
    gateway, port = egress
    local = f"{gateway}:{port}"
    network = {"network": ["api.example.test", REBIND], "network_local": [local]}
    name = "evil-network"
    manifest = _manifest(name, NETWORK_TOOLS, network=network)

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, manifest) as h:

            async def tool(tool: str, **params: Any) -> Any:
                return await h.call(f"{name}.{tool}", params, None)

            seen = {
                # Positive control: the one grant works, through the proxy.
                "granted": await tool("tunnel", host=gateway, port=port),
                # Around the proxy: no route out of the internal network, no DNS.
                "direct_grant": await tool("connect", host=gateway, port=port),
                "direct_out": await tool("connect", host="192.0.2.1", port=443),
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
            with _rebinding():
                seen["rebinding"] = await tool("tunnel", host=REBIND, port=443)
            return seen

    seen = asyncio.run(go())
    granted = seen["granted"]
    assert granted.data["status"] == "HTTP/1.1 200 Connection established", granted.data
    assert granted.data["answer"].startswith("HTTP/1.0 200")
    assert granted.violations == ()
    assert seen["direct_grant"].data["connected"] is False
    assert seen["direct_out"].data["connected"] is False
    assert seen["resolve"].data["resolved"] is False

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
    tmp_path: Path, plugin_dir: Path, egress: tuple[str, int]
) -> None:
    """Another plugin's proxy holds another allow-list: it is on that plugin's network only,
    so the attacker cannot even reach it (and it serves its own container only, T48)."""
    gateway, port = egress
    evil = _manifest("evil-network", NETWORK_TOOLS)
    sibling = _manifest(
        "evil-sibling", NETWORK_TOOLS, network={"network_local": [f"{gateway}:{port}"]}
    )

    async def go() -> tuple[Any, Any, Any]:
        async with _host(tmp_path, plugin_dir, evil, sibling) as h:
            env = (await h.call("evil-sibling.env", {}, None)).data["env"]
            own = await h.call("evil-sibling.tunnel", {"host": gateway, "port": port}, None)
            stolen = await h.call(
                "evil-network.tunnel",
                {"host": gateway, "port": port, "via": env["HTTPS_PROXY"]},
                None,
            )
            mine = await h.call("evil-network.tunnel", {"host": gateway, "port": port}, None)
            return own, stolen, mine

    own, stolen, mine = asyncio.run(go())
    assert own.data["status"] == "HTTP/1.1 200 Connection established", own.data  # control
    assert stolen.data["status"] == "", stolen.data  # no route to the sibling's proxy
    assert stolen.data["error"]
    assert stolen.violations == ()
    assert mine.data["status"] == "HTTP/1.1 403 Forbidden"  # its own proxy: not granted
    assert _details(mine) == [f"evil-network: egress blocked to {gateway}:{port} (ip_literal)"]


# --- secrets: T03, T09, T43 -----------------------------------------------------------------------


def test_evil_secrets_finds_no_secret_it_did_not_declare(
    tmp_path: Path, plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another plugin's secret travels in its own container's preamble, the core's in Horae's
    environment and secrets directory: none is in this container's environment, files or
    `/proc`. Its own secret comes back redacted, flagged `secret_in_output`."""
    monkeypatch.setenv("HORAE_ACT_KEY", CORE_KEY)
    monkeypatch.setenv("SIBLING_TOKEN", SIBLING_TOKEN)
    evil = _manifest("evil-secrets", ("env", "read", "proc"), secret="EVIL_TOKEN")  # noqa: S106 (a name)
    sibling = _manifest("evil-sibling", ("env",), secret="SIBLING_TOKEN")  # noqa: S106 (a name)
    secrets_path = tmp_path / "secrets"

    async def go() -> dict[str, Any]:
        async with _host(tmp_path, plugin_dir, evil, sibling) as h:
            await h.call("evil-sibling.env", {}, None)  # running, its preamble delivered

            async def tool(tool: str, **params: Any) -> Any:
                return await h.call(f"evil-secrets.{tool}", params, None)

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
        "evil-secrets.env returned secret EVIL_TOKEN in its output (redacted)"
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


# --- paths: T04, T51, T53 -------------------------------------------------------------------------


def _instance(tmp_path: Path) -> tuple[Path, MountPolicy]:
    """A synthetic instance: a vault (read-only grant) with an inbox (writable grant), an
    undeclared directory and the instance's own controls."""
    root = (tmp_path / "instance").resolve()
    for directory in ("vault/inbox", "undeclared", "state", "plugins"):
        (root / directory).mkdir(parents=True)
    (root / "vault" / "note.md").write_text("vault note\n", encoding="utf-8")
    (root / "undeclared" / "private.md").write_text("not for plugins\n", encoding="utf-8")
    (root / "horae.lock").write_text("schema_version: 1\n", encoding="utf-8")
    (root / "horae.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    policy = MountPolicy.for_instance(
        root, state_dir=root / "state", secrets_dir=tmp_path / "secrets", home=tmp_path / "home"
    )
    return root, policy


PATH_TOOLS = ("env", "fs", "read", "write", "plant", "hook")


def test_evil_paths_sees_only_its_grants_and_its_traps_are_undone(
    tmp_path: Path, plugin_dir: Path
) -> None:
    root, policy = _instance(tmp_path)
    vault, inbox = root / "vault", root / "vault" / "inbox"
    name = "evil-paths"
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
    assert (vault / "note.md").read_text(encoding="utf-8") == "vault note\n"
    assert not (vault / "new.md").exists()
    planted = seen["planted"]
    # A hard link across mounts is refused by the kernel; a write through `..` lands outside
    # every mount (the read-only root filesystem); a device needs CAP_MKNOD.
    assert planted["hardlink"] == "Invalid cross-device link"
    assert planted["dotdot_write"] in ("Read-only file system", "No such file or directory")
    assert not (root / "evil-dotdot.md").exists()
    assert planted["device"] == "Operation not permitted"
    # What the kernel allowed inside the writable path, the link resolves to nothing there...
    assert planted["symlink"] is True
    assert seen["through_link"]["read"] is None
    assert {"escape", "dotdot", "fifo", "setuid"} <= set(seen["during"])
    # ...and once the container is gone, the sweep undoes every trap left for the host.
    for trap in ("escape", "dotdot", "fifo", "hardlink", "null"):
        assert not os.path.lexists(inbox / trap), trap
    if planted["setuid"] is True:
        assert not os.stat(inbox / "setuid").st_mode & 0o6000
    assert private.read_text(encoding="utf-8") == "not for plugins\n"
    assert not (root / "state" / "quarantine" / name).exists()  # a clean sweep
    _gone(f"dev.horae.plugin={name}")


def test_evil_paths_planting_a_git_hook_quarantines_the_plugin(
    tmp_path: Path, plugin_dir: Path
) -> None:
    """A new `.git` in a writable path is what the sweep cannot vouch for: it fails closed,
    and the plugin's next start is refused until the operator removes the quarantine file."""
    root, policy = _instance(tmp_path)
    inbox = root / "vault" / "inbox"
    name = "evil-paths"
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

RESOURCE_TOOLS = ("env", "fork", "fds", "fill", "hog", "daemon")


def test_evil_resources_hits_every_limit_and_leaves_nothing_behind(
    tmp_path: Path, plugin_dir: Path
) -> None:
    name = "evil-resources"
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
    name = "evil-resources"
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
