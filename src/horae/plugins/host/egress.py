# SPDX-License-Identifier: Apache-2.0
"""A plugin container's egress proxy, from the host's side (#50, schemas decision 76).

For each plugin container start, `ContainerRuntime.spawn` first starts **its own proxy**
(`start_proxy`): a container running `horae.plugins.host.egress_proxy` with the plugin's
**locked** network permissions as its only policy, on two networks: the plugin's own internal
network (`horae-plugin-<name>`, where the plugin reaches it) and `EGRESS_NETWORK` (the way out).
It is hardened like the plugin (decision 74 C) and runs as `nobody`. The plugin then gets the
proxy's address in `HTTPS_PROXY`, `HTTP_PROXY` (and their lowercase forms) and
`HORAE_EGRESS_PROXY`; its container has no other route.

The proxy reports each refusal as one JSON line on its stdout; `ProxyHandle.watch` reads them,
logs each at WARNING and keeps them (capped) until the host takes them as `egress_blocked`
violations of the call in flight (`ProxyHandle.take`). **Fail closed**: a proxy that does not
report ready within `READY_TIMEOUT_S` is removed and the plugin is not started
(`runtime_unavailable`). `ProxyHandle.stop` closes its stdin, ends its CLI and always runs
`docker rm --force`.
"""

import ipaddress
import json
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

import anyio
import anyio.abc

from horae.contracts.run import Violation
from horae.plugins.host import egress_proxy
from horae.plugins.host.errors import PluginCallError

EGRESS_NETWORK = "horae-egress"
"""The network every egress proxy reaches the outside through (not internal)."""
EGRESS_NETWORK_LABEL = "egress"
"""The value of `dev.horae.network` on `EGRESS_NETWORK`."""
EGRESS_NETWORK_OPTIONS = {"com.docker.network.bridge.enable_icc": "false"}
"""Driver options `EGRESS_NETWORK` must have: proxies do not talk to each other."""
PROXY_USER = "65534:65534"
"""The proxy reads no file: it runs as `nobody`."""
PROXY_LIMITS = ("--pids-limit", "64", "--memory", "128m")
READY_TIMEOUT_S = 30.0
"""How long a proxy may take to report ready."""
STOP_WAIT_S = 2.0
MAX_KEPT = egress_proxy.MAX_EVENTS
"""Blocked attempts kept between two `take`s."""
PROXY_SOURCE = Path(egress_proxy.__file__).read_text(encoding="utf-8")
"""The proxy's source, run with `python3 -I -S -c` (visible in `docker inspect`: no secret)."""
_MAX_LINE = 4096
_HOST = re.compile(r"[a-z0-9.:\-]{1,253}")
_REASON = re.compile(r"[a-z_]{1,40}")

_log = logging.getLogger("horae.plugins.host.egress")


def proxy_policy(network: Sequence[str], network_local: Sequence[str], clients: str) -> str:
    """The proxy's policy argument: the locked lists and the plugin network's subnet (the
    only clients it answers) as compact JSON (no secret)."""
    return json.dumps(
        {
            "network": sorted(network),
            "network_local": sorted(network_local),
            "clients": [clients],
        },
        separators=(",", ":"),
    )


def network_subnet(network: Mapping[str, object]) -> str | None:
    """The IPv4 subnet of a network (`docker network inspect`), or `None`."""
    ipam = network.get("IPAM")
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    if not isinstance(configs, list):
        return None
    for config in configs:
        subnet = config.get("Subnet") if isinstance(config, dict) else None
        if isinstance(subnet, str):
            try:
                return str(ipaddress.IPv4Network(subnet))
            except ValueError:
                continue
    return None


def proxy_argv(
    docker: str,
    *,
    plugin: str,
    name: str,
    image: str,
    plugin_network: str,
    policy: str,
    python: str,
    labels: Mapping[str, str],
) -> list[str]:
    """The proxy's `docker run` (decision 76 B): decision 74 C's hardening, `nobody`, limits,
    on the plugin's network and `EGRESS_NETWORK`."""
    label_args = [arg for key, value in labels.items() for arg in ("--label", f"{key}={value}")]
    return [
        docker,
        "run",
        "--interactive",
        "--rm",
        "--init",
        "--name",
        name,
        *label_args,
        "--pull",
        "never",
        "--log-driver",
        "none",
        "--user",
        PROXY_USER,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *PROXY_LIMITS,
        "--network",
        plugin_network,
        "--network",
        EGRESS_NETWORK,
        "--workdir",
        "/",
        "--entrypoint",
        python,
        image,
        "-I",
        "-S",
        "-c",
        PROXY_SOURCE,
        policy,
    ]


def egress_network_problem(network: Mapping[str, object]) -> str | None:
    """Why `network` (`docker network inspect`) is not the egress network, or `None`."""
    if network.get("Driver") != "bridge":
        return "is not a bridge network"
    if network.get("Internal") is True:
        return "is internal (it would give the proxy no way out)"
    if network.get("EnableIPv6") is True:
        return "has IPv6 enabled"
    if network.get("ConfigOnly") is True:
        return "is a configuration-only network"
    config_from = network.get("ConfigFrom")
    if isinstance(config_from, dict) and config_from.get("Network"):
        return "takes its configuration from another network"
    labels = network.get("Labels")
    if not isinstance(labels, dict) or labels.get("dev.horae.network") != EGRESS_NETWORK_LABEL:
        return f"has no dev.horae.network={EGRESS_NETWORK_LABEL} label"
    options = network.get("Options")
    if not isinstance(options, dict):
        return "has no driver options"
    for key, value in EGRESS_NETWORK_OPTIONS.items():
        if options.get(key) != value:
            return f"does not set {key}={value}"
    return None


def plugin_address(networks: object, plugin_network: str) -> str | None:
    """The proxy's IPv4 address on `plugin_network`, from `docker container inspect`'s
    `NetworkSettings.Networks`; `None` when absent or not an IPv4 address."""
    if not isinstance(networks, dict):
        return None
    entry = networks.get(plugin_network)
    text = entry.get("IPAddress") if isinstance(entry, dict) else None
    if not isinstance(text, str):
        return None
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    return None if address.is_unspecified else str(address)


def blocked_detail(plugin: str, event: object) -> str | None:
    """The violation detail of one proxy line (`{"event": "blocked", ...}`), or `None` for any
    other line. Only a host, a port and a reason code, each checked again here."""
    if not isinstance(event, dict):
        return None
    if event.get("event") == "suppressed":
        return f"{plugin}: more blocked egress attempts (not listed)"
    if event.get("event") != "blocked":
        return None
    host, port, reason = event.get("host"), event.get("port"), event.get("reason")
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        return None
    if not isinstance(host, str) or _HOST.fullmatch(host) is None:
        host = "?"
    if type(port) is not int or not 0 <= port <= 65535:
        port = 0
    shown = f"[{host}]" if ":" in host else host
    return f"{plugin}: egress blocked to {shown}:{port} ({reason})"


class ProxyHandle:
    """One running proxy (see the module docstring)."""

    def __init__(
        self,
        plugin: str,
        name: str,
        process: anyio.abc.Process,
        remove: Callable[[], Awaitable[None]],
    ) -> None:
        self.plugin = plugin
        self.name = name
        self.url = ""
        """`http://<address>:<port>` on the plugin's network, once ready."""
        self._process = process
        self._remove = remove
        self._kept: list[str] = []
        self._dropped = 0
        self._stopped = False

    async def ready(self, timeout_s: float = READY_TIMEOUT_S) -> bool:
        """Wait for the proxy's `ready` line; `False` if it ends or times out first."""
        stdout = self._process.stdout
        if stdout is None:  # pragma: no cover (always piped)
            return False
        buffer = b""
        with anyio.move_on_after(timeout_s):
            while True:
                try:
                    chunk = await stdout.receive(1)
                except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
                    return False
                buffer += chunk
                if chunk == b"\n":
                    if _event(buffer) == {"event": "ready", "port": egress_proxy.LISTEN_PORT}:
                        return True
                    buffer = b""
                if len(buffer) > _MAX_LINE:
                    return False
        return False

    async def error(self) -> str:
        """The first line the CLI wrote to stderr (once it failed), for the error message."""
        stderr = self._process.stderr
        data = b""
        if stderr is not None:
            with (
                anyio.move_on_after(STOP_WAIT_S),
                suppress(anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError),
            ):
                while len(data) < _MAX_LINE:
                    data += await stderr.receive()
        lines = data.decode("utf-8", errors="replace").strip().splitlines()
        return re.sub(r"[\x00-\x1f\x7f]", " ", lines[0])[:300] if lines else "no message"

    async def watch(self) -> None:
        """Read the proxy's events until it ends (run in the host's task group); its CLI's
        stderr is drained and discarded."""
        async with anyio.create_task_group() as group:
            group.start_soon(self._drain)
            await self._events()

    async def _drain(self) -> None:
        stderr = self._process.stderr
        if stderr is None:  # pragma: no cover (always piped)
            return
        with suppress(anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            while True:
                await stderr.receive()

    async def _events(self) -> None:
        stdout = self._process.stdout
        if stdout is None:  # pragma: no cover (always piped)
            return
        pending = b""
        with suppress(anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            while True:
                pending += await stdout.receive()
                *lines, pending = pending.split(b"\n")
                if len(pending) > _MAX_LINE:
                    pending = b""
                for line in lines:
                    self.feed(line)

    def feed(self, line: bytes) -> None:
        """Keep one event line (a blocked attempt) and log it."""
        detail = blocked_detail(self.plugin, _event(line))
        if detail is None:
            return
        _log.warning("%s", detail)
        if len(self._kept) < MAX_KEPT:
            self._kept.append(detail)
        else:
            self._dropped += 1

    def take(self) -> list[str]:
        """The blocked attempts seen since the last call (and empties the list)."""
        kept, self._kept = self._kept, []
        if self._dropped:
            kept.append(f"{self.plugin}: {self._dropped} more blocked egress attempts")
            self._dropped = 0
        return kept

    async def stop(self) -> None:
        """Close stdin (the proxy exits), end the CLI, then always `docker rm --force`. Never
        raises; bounded."""
        if self._stopped:
            return
        self._stopped = True
        with anyio.CancelScope(shield=True):
            process = self._process
            with suppress(Exception):
                if process.stdin is not None:
                    await process.stdin.aclose()
            with anyio.move_on_after(STOP_WAIT_S):
                await process.wait()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with anyio.move_on_after(STOP_WAIT_S):
                    await process.wait()
            with suppress(Exception):
                await self._remove()


def violations(details: Sequence[str]) -> tuple[Violation, ...]:
    """`egress_blocked` violations of `details`."""
    return tuple(Violation(kind="egress_blocked", detail=detail) for detail in details)


def _event(line: bytes) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


def unavailable(plugin: str, message: str) -> PluginCallError:
    return PluginCallError("runtime_unavailable", f"plugin {plugin} (runtime container): {message}")


async def open_proxy(argv: Sequence[str], env: Mapping[str, str]) -> anyio.abc.Process:
    """Start the proxy's CLI (stdin, stdout and stderr piped)."""
    return await anyio.open_process(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=True,
    )


__all__ = [
    "EGRESS_NETWORK",
    "EGRESS_NETWORK_LABEL",
    "EGRESS_NETWORK_OPTIONS",
    "PROXY_SOURCE",
    "PROXY_USER",
    "ProxyHandle",
    "blocked_detail",
    "egress_network_problem",
    "network_subnet",
    "open_proxy",
    "plugin_address",
    "proxy_argv",
    "proxy_policy",
    "violations",
]
