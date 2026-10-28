# SPDX-License-Identifier: Apache-2.0
"""Where the CLI's sinks come from (#26): the plugin host, or an explicit local `--vault`.

An instance's plugins are installed in `<instance>/plugins/<name>/plugin.yaml` (`horae init`
writes the built-in ones with `horae.plugins.builtin.install`; M4's `horae plugin add`, #44,
will install the others there). `horae run` always goes through the host (`open_ports`).
`horae diff` and `horae undo` do too when the instance has a `plugins/` directory, so they
read and restore notes through the sink that applied them (`file-sink`'s identity, decision
36); `--vault` stays as an explicit override for instances without plugins (runs applied
before #25, or by the built-in `LocalFileSink`): it reads or writes that local directory
with the built-in sinks.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from horae.apply import SinkRegistry
from horae.cli_output import EXIT_PROBLEMS, CommandFailed, plural
from horae.config import Instance, InstanceError, ProblemsError, load_instance
from horae.plugins.host import HostRegistry, RegistryError
from horae.runner import LivePorts, open_ports

PLUGINS_DIR = "plugins"


def plugins_dir(root: Path) -> Path:
    """Where the instance's plugins are installed."""
    return root / PLUGINS_DIR


def uses_host(root: Path, vault: Path | None) -> bool:
    """Whether `horae diff` / `horae undo` go through the plugin host: no `--vault`, and the
    instance has a `plugins/` directory."""
    return vault is None and plugins_dir(root).is_dir()


def load(root: Path) -> Instance:
    """The instance, or `CommandFailed(config_invalid)` listing its problems."""
    try:
        return load_instance(root)
    except InstanceError as exc:
        found = plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None


def check_plugins(instance: Instance) -> None:
    """`CommandFailed(plugins_invalid)` when a plugin of `horae.lock` is not installed, its
    manifest is invalid, or it does not match the lock (checked before any process starts)."""
    try:
        HostRegistry.from_directory(instance.lock, plugins_dir(instance.root))
    except (ProblemsError, RegistryError) as exc:
        problems = exc.problems if isinstance(exc, ProblemsError) else ()
        detail = "; ".join(map(str, problems)) if problems else str(exc)
        message = (
            f"{detail} (the plugins of horae.lock must be installed in "
            f"{plugins_dir(instance.root)}; `horae init` installs the built-in ones)"
        )
        raise CommandFailed(EXIT_PROBLEMS, "plugins_invalid", message) from None


@asynccontextmanager
async def live_ports(instance: Instance) -> AsyncIterator[LivePorts]:
    """`open_ports` on the instance's plugins, checked first (`check_plugins`)."""
    check_plugins(instance)
    async with open_ports(instance, plugins_dir(instance.root)) as live:
        yield live


@asynccontextmanager
async def host_sinks(instance: Instance) -> AsyncIterator[SinkRegistry]:
    """The instance's `McpSinks`, for one `horae diff` or `horae undo`."""
    async with live_ports(instance) as live:
        yield live.sinks


__all__ = [
    "PLUGINS_DIR",
    "check_plugins",
    "host_sinks",
    "live_ports",
    "load",
    "plugins_dir",
    "uses_host",
]
