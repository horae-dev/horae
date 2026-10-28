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

`LivePorts.ports` is the runner's `Ports(plugins=registry, tools=host, llm=llm, sinks=sinks,
skills=skills)`, the skills read from `<plugins_dir>/<name>/skills/` (`DirectorySkills`, #27).
`llm` is the caller's backend when given; otherwise, when a pipeline of the instance has an
`llm` step, a `horae.llm.ModelBackends` over the instance's model aliases
(`horae.llm.backends.model_backends`, #28), sharing the host's `SecretResolver`. It is lazy:
an alias' provider backend is built and its secret resolved on its first call, so a run that
makes no LLM call never reads an API key. Its connections are closed with the block.

Where `horae plugin add` installs plugins (the `plugins_dir`) is M4's (#44); the walking
skeleton and the tests write it with `horae.plugins.builtin.install`. `horae run`,
`horae diff` and `horae undo` open it on `<instance>/plugins` (`horae.cli_host`, #26).

Raises `ProblemsError` for an invalid manifest and `RegistryError` for one that does not
match `horae.lock`, before any process starts.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from horae.config import Instance
from horae.contracts.pipeline import LlmStep
from horae.engine import LlmBackend
from horae.llm import ModelBackends
from horae.llm.backends import model_backends
from horae.plugins.host import HostRegistry, McpSinks, PluginHost, host_for_instance
from horae.runner._runner import Ports
from horae.secrets import SecretResolver
from horae.skills import DirectorySkills


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
    secrets = secrets or instance.secret_resolver()
    owned: ModelBackends | None = None
    if llm is None and uses_llm(instance):
        llm = owned = model_backends(
            instance.config.models, secrets, redaction_names=instance.redaction_names()
        )
    try:
        async with host_for_instance(instance, registry, secrets=secrets) as host:
            sinks = McpSinks(registry, host)
            skills = DirectorySkills({p.manifest.name: p.root for p in registry.plugins()})
            ports = Ports(plugins=registry, tools=host, llm=llm, sinks=sinks, skills=skills)
            yield LivePorts(registry, host, sinks, ports)
    finally:
        if owned is not None:
            await owned.aclose()


def uses_llm(instance: Instance) -> bool:
    """Whether a pipeline of `instance` has an `llm` step."""
    return any(
        isinstance(step, LlmStep)
        for pipeline in instance.pipelines.values()
        for step in pipeline.steps
    )


__all__ = ["LivePorts", "open_ports", "uses_llm"]
