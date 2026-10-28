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

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from horae.apply import SinkRegistry
from horae.cli_output import EXIT_PROBLEMS, CommandFailed, plural
from horae.config import CONFIG_FILE, Instance, InstanceError, Problem, ProblemsError, load_instance
from horae.contracts.instance import ModelAlias
from horae.contracts.pipeline import LlmStep
from horae.contracts.plugin import StdioRuntime
from horae.llm.backends import FACTORIES, claude_cli
from horae.plugins.host import HostRegistry, PluginCallError, RegistryError
from horae.plugins.host.host import resolve_command
from horae.runner import LivePorts, open_ports

PLUGINS_DIR = "plugins"
LOCK_FILE = "horae.lock"
OWN_VAULT = "vault"
"""The vault `horae init` creates inside the instance."""
NOT_RELOCATABLE = (
    "instances are not relocatable in v1: horae.lock holds absolute paths and the installed "
    "plugins a pinned interpreter; fix the lock (or run `horae init` in a new directory)"
)


def plugins_dir(root: Path) -> Path:
    """Where the instance's plugins are installed."""
    return root / PLUGINS_DIR


def uses_host(root: Path, vault: Path | None) -> bool:
    """Whether `horae diff` / `horae undo` go through the plugin host: no `--vault`, and the
    instance has a `plugins/` directory."""
    return vault is None and plugins_dir(root).is_dir()


def load(root: Path) -> Instance:
    """The instance, or `CommandFailed(config_invalid)` listing its problems. Every secret
    it names is resolved for redaction (`Instance.prime_redaction`, #53)."""
    try:
        instance = load_instance(root)
    except InstanceError as exc:
        found = plural(len(exc.problems), "problem") + " found"
        message = "; ".join([*map(str, exc.problems), found])
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    instance.prime_redaction()
    return instance


def check_plugins(instance: Instance) -> None:
    """`CommandFailed(plugins_invalid)` when a plugin of `horae.lock` is not installed, its
    manifest is invalid, or it does not match the lock (checked before any process starts)."""
    _registry(instance)


def _registry(instance: Instance) -> HostRegistry:
    try:
        return HostRegistry.from_directory(instance.lock, plugins_dir(instance.root))
    except (ProblemsError, RegistryError) as exc:
        problems = exc.problems if isinstance(exc, ProblemsError) else ()
        detail = "; ".join(map(str, problems)) if problems else str(exc)
        message = (
            f"{detail} (the plugins of horae.lock must be installed in "
            f"{plugins_dir(instance.root)}; `horae init` installs the built-in ones)"
        )
        raise CommandFailed(EXIT_PROBLEMS, "plugins_invalid", message) from None


def _lock_problem(message: str) -> Problem:
    return Problem(LOCK_FILE, None, None, message)


def _path_problems(instance: Instance) -> list[Problem]:
    """Every `paths.read` / `paths.write` entry of `horae.lock` that does not exist, and an
    own `vault/` the lock does not write to (a copied or moved instance)."""
    problems: list[Problem] = []
    written: set[str] = set()
    for name, plugin in sorted(instance.lock.plugins.items()):
        for access in ("read", "write"):
            for entry in getattr(plugin.permissions.paths, access):
                if access == "write":
                    written.add(os.path.realpath(entry))
                if not os.path.exists(entry):
                    problems.append(
                        _lock_problem(
                            f"plugins.{name}.permissions.paths.{access}: {entry} does not "
                            f"exist ({NOT_RELOCATABLE})"
                        )
                    )
    own = instance.root / OWN_VAULT
    if written and own.is_dir() and os.path.realpath(own) not in written:
        elsewhere = ", ".join(sorted(written))
        problems.append(
            _lock_problem(
                f"the instance has its own {OWN_VAULT}/ ({own.resolve()}) but horae.lock "
                f"writes to {elsewhere}: a copied or moved instance? ({NOT_RELOCATABLE})"
            )
        )
    return problems


def _command_problems(registry: HostRegistry) -> list[Problem]:
    """Each installed plugin whose command (its interpreter) is missing or not executable."""
    problems: list[Problem] = []
    for installed in registry.plugins():
        runtime = installed.manifest.runtime
        if not isinstance(runtime, StdioRuntime):
            continue
        try:
            resolve_command(list(runtime.command), installed.root)
        except PluginCallError as exc:
            where = f"{PLUGINS_DIR}/{installed.manifest.name}/plugin.yaml"
            problems.append(
                Problem(where, None, None, f"runtime.command: {exc.message} ({NOT_RELOCATABLE})")
            )
    return problems


CLOUD_BACKENDS = frozenset({"anthropic", "openai-compatible"})
"""Backends billed per token, whose aliases `horae doctor` expects to have a `price`. `ollama`
is local (no warning); an `openai-compatible` alias is treated as cloud even when it points
at a local server (a warning, which does not fail the check; schemas decision 40)."""


def _backend_problems(instance: Instance) -> list[Problem]:
    """For each model alias used by an `llm` step: an error when its backend is not
    implemented in this Horae (`horae.llm.backends.FACTORIES`), a warning when it is a
    cloud backend without a `price` (its calls are then recorded at `usd: 0` and the step's
    `usd` budget does not bound them; schemas decision 38 A), and a warning when it sends a
    `secret` to an `http://` `base_url` (the key crosses the LAN in clear; decision 40).
    A `claude-cli` alias (decision 42) needs its `secret` (token mode), an executable found at
    a supported version (`claude_cli.check`, which runs `<command> --version`) and a `price`
    (decision 42 A: the backend refuses to run without one, so that every call has a dollar
    reservation), and gets a warning when administrator-managed Claude Code settings exist
    (decision 42 D: no flag turns them off). Aliases no pipeline uses are not checked."""
    used = sorted(
        {
            step.llm.model
            for pipeline in instance.pipelines.values()
            for step in pipeline.steps
            if isinstance(step, LlmStep)
        }
    )
    problems: list[Problem] = []
    for alias in used:
        spec = instance.config.models.get(alias)
        if spec is None:
            continue
        if spec.backend not in FACTORIES:
            available = ", ".join(sorted(FACTORIES))
            problems.append(
                Problem(
                    CONFIG_FILE,
                    None,
                    None,
                    f"models.{alias}.backend: backend {spec.backend!r} is not available in "
                    f"this version of Horae (available: {available})",
                )
            )
        elif spec.backend == "claude-cli":
            problems.extend(_claude_cli_problems(alias, spec))
        elif spec.backend in CLOUD_BACKENDS and spec.price is None:
            problems.append(
                Problem(
                    CONFIG_FILE,
                    None,
                    None,
                    f"models.{alias}.price: model alias {alias!r} (backend {spec.backend!r}) "
                    "has no price: its calls are recorded at usd 0 and the step's usd budget "
                    "does not bound them (only tokens_out does)",
                    severity="warning",
                )
            )
        if (
            spec.backend in FACTORIES
            and spec.secret is not None
            and spec.base_url is not None
            and spec.base_url.startswith("http://")
        ):
            problems.append(
                Problem(
                    CONFIG_FILE,
                    None,
                    None,
                    f"models.{alias}.secret: model alias {alias!r} sends its key in clear "
                    "text over http:// (anyone on the local network path can read it); use "
                    "an https:// base_url, or drop the secret if the server needs none",
                    severity="warning",
                )
            )
    return problems


def _claude_cli_problems(alias: str, spec: ModelAlias) -> list[Problem]:
    """The `claude-cli` checks of `_backend_problems`."""
    problems: list[Problem] = []
    if spec.secret is None:
        problems.append(
            Problem(
                CONFIG_FILE,
                None,
                None,
                f"models.{alias}.secret: backend 'claude-cli' needs `secret`, a "
                f"{claude_cli.TOKEN_ENV} made with `claude setup-token` (token mode is the "
                "only mode)",
            )
        )
    failure = claude_cli.check(spec)
    if failure is not None:
        problems.append(
            Problem(
                CONFIG_FILE,
                None,
                None,
                f"models.{alias}.command: {failure} (supported: Claude Code "
                f"{claude_cli.CLAUDE_CODE_VERSION_RANGE})",
            )
        )
    if spec.price is None:
        problems.append(
            Problem(
                CONFIG_FILE,
                None,
                None,
                f"models.{alias}.price: backend 'claude-cli' needs `price` (the notional "
                "total_cost_usd is known only after a call; the price reserves each call's "
                "dollars before it is made)",
            )
        )
    managed = claude_cli.managed_settings()
    if managed:
        problems.append(
            Problem(
                CONFIG_FILE,
                None,
                None,
                f"models.{alias}: administrator-managed Claude Code settings exist "
                f"({', '.join(managed)}): they apply to every `claude` run, Horae's included, "
                "and may add hooks, environment variables or MCP servers that no flag turns "
                "off (not read by Horae; see docs/security.md T11)",
                severity="warning",
            )
        )
    return problems


def _budget_problems(instance: Instance) -> list[Problem]:
    """A warning for each `usd` daily ceiling (`budgets.daily`, `budgets.pipelines.<name>`)
    that covers a pipeline whose `llm` step uses an alias without `price` (schemas decision
    41 B): that alias' calls are recorded at `usd: 0`, so the ceiling does not bound their
    dollars (only `tokens_out` does)."""
    budgets = instance.config.budgets
    problems: list[Problem] = []
    for pipeline in sorted(instance.pipelines.values(), key=lambda p: p.name):
        aliases = sorted({s.llm.model for s in pipeline.steps if isinstance(s, LlmStep)})
        unpriced = [
            alias
            for alias in aliases
            if (spec := instance.config.models.get(alias)) is not None and spec.price is None
        ]
        if not unpriced:
            continue
        own = budgets.pipelines.get(pipeline.name)
        ceilings = []
        if budgets.daily is not None and budgets.daily.usd is not None:
            ceilings.append("budgets.daily.usd")
        if own is not None and own.daily.usd is not None:
            ceilings.append(f"budgets.pipelines.{pipeline.name}.daily.usd")
        for where in ceilings:
            for alias in unpriced:
                problems.append(
                    Problem(
                        CONFIG_FILE,
                        None,
                        None,
                        f"{where}: pipeline {pipeline.name!r} uses model alias {alias!r}, "
                        "which has no price: the usd ceiling does not bound its calls "
                        "(only tokens_out does)",
                        severity="warning",
                    )
                )
    return problems


def instance_problems(instance: Instance) -> list[Problem]:
    """What `horae doctor` checks beyond the configuration (`check_instance`): the lock's
    paths exist, the instance's own `vault/` is the one the lock writes to, each model alias
    an `llm` step uses has an available backend (and, for a cloud backend, a `price`, else a
    warning; a warning too for a `usd` daily ceiling over such an unpriced alias), and,
    when the instance has a `plugins/` directory, the plugins are installed
    as the lock pins them (`check_plugins`) and each command (its interpreter) exists and is
    executable."""
    problems = _path_problems(instance) + _backend_problems(instance) + _budget_problems(instance)
    if plugins_dir(instance.root).is_dir():
        try:
            registry = _registry(instance)
        except CommandFailed as failed:
            problems.append(Problem(f"{PLUGINS_DIR}/", None, None, failed.message))
        else:
            problems.extend(_command_problems(registry))
    return problems


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
    "instance_problems",
    "live_ports",
    "load",
    "plugins_dir",
    "uses_host",
]
