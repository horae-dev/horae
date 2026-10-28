# SPDX-License-Identifier: Apache-2.0
"""Live ports for one run: the plugin host, its registry and its sinks (#25).

`open_ports(instance, plugins_dir)` is an async context manager that builds, for one run (or
one `horae undo`), everything the runner needs from the installed plugins:

- a `HostRegistry` from the instance's `horae.lock` and `<plugins_dir>/<name>/plugin.yaml`
  for every locked plugin (`HostRegistry.from_directory`: a manifest must match its lock
  entry, and the locked permissions are the ones in force);
- a `PluginHost` (`host_for_instance`: the instance's time zone, plugin timeouts, secrets
  and `<state_dir>/plugin-runs` as its work directory), opened for the block and closed at
  its end (every plugin process stopped, nothing carried over to the next run);
- `McpSinks`, the `SinkRegistry` that applies routed intents through the plugins' sink
  operations (`horae.plugins.host.sink`).

`LivePorts.ports` is the runner's `Ports(plugins=registry, tools=host, sinks=sinks)`.
`llm` is `None` until M2 wires a backend. Where `horae plugin add` installs plugins (the
`plugins_dir`) is M4's (#44); the walking skeleton and the tests write it with
`horae.plugins.builtin.install`. The CLI wiring is #26.

Raises `ProblemsError` for an invalid manifest and `RegistryError` for one that does not
match `horae.lock`, before any process starts.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from horae.config import Instance
from horae.engine import LlmBackend
from horae.plugins.host import HostRegistry, McpSinks, PluginHost, host_for_instance
from horae.runner._runner import Ports
from horae.secrets import SecretResolver


@dataclass(frozen=True)
class LivePorts:
    """What `open_ports` yields."""

    registry: HostRegistry
    host: PluginHost
    sinks: McpSinks
    ports: Ports


@asynccontextmanager
async def open_ports(
    instance: Instance,
    plugins_dir: Path,
    *,
    secrets: SecretResolver | None = None,
    llm: LlmBackend | None = None,
) -> AsyncIterator[LivePorts]:
    """The live ports of one run (see the module docstring)."""
    registry = HostRegistry.from_directory(instance.lock, plugins_dir)
    async with host_for_instance(instance, registry, secrets=secrets) as host:
        sinks = McpSinks(registry, host)
        ports = Ports(plugins=registry, tools=host, llm=llm, sinks=sinks)
        yield LivePorts(registry, host, sinks, ports)


__all__ = ["LivePorts", "open_ports"]
