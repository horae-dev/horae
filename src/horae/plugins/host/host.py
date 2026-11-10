# SPDX-License-Identifier: Apache-2.0
"""The plugin host: `PluginRegistry` and `ToolCaller` over MCP stdio plugins (#24).

**Registry.** `HostRegistry` resolves tools and skills from `horae.lock` and the installed
manifests (`plugin.yaml`). A manifest must be pinned in the lock at the same version, and may
not ask for more than the user approved there (`network`, `secrets`, `paths`, `intents`): the
**locked** permissions are the ones in force. A plugin installed by `horae plugin add` also
has an install record (`.horae-install.json`, decision 62 F): its name, version, repo and
commit must be the lock entry's, else the plugin is refused (an installed tree that is not
the approved one, decision 70 D). A plugin without a record (the built-in ones `horae init`
installs, a plugin copied by hand) is checked against the lock by its manifest only. A tool
or skill resolves to the plugin whose manifest `provides` it (decision 11: the namespace need
not be the plugin's name); two plugins
providing the same ref is an error, and so is providing a sink operation name
(`<ns>.<operation>` of any plugin's sink or notifier namespace, #25). Resolution reads only
the documents given (#23).

**Host.** `PluginHost` is used for one run: `async with PluginHost(...) as host`. It is the
run's `ToolCaller`:

- **one process per plugin per run**, started lazily on the first call to one of its tools,
  reused for every later call, and stopped after its **last planned call** (the engine calls
  `release(plugin)` after the last step that uses it, decision 35 A) or when the host exits
  (no daemons, nothing carried between runs). Calls to one plugin are serialised (one in
  flight per process, so its stderr belongs to one call); calls to different plugins run
  concurrently (the engine's parallel `collect`). Serialisation means N calls to one plugin
  in one step can take up to N times the call timeout; the run timeout (#19) bounds the
  whole;
- **opening** resolves every secret it knows of: those declared by every locked plugin and
  every `secrets.refs` entry (a secret that cannot be resolved is skipped here; it fails the
  plugin that needs it later). The redactor is then complete from the first call, whatever
  order plugins start in (decision 35 B);
- **startup**: the declared secrets are resolved (`secret_missing`) and checked against the
  deny list (`env_unsafe`), `argv[0]` is resolved to an absolute path (`resolve_command`; a
  bare name in the plugin's own venv first, #45, decision 69 G), the process is spawned
  (`horae.plugins.host.transport`) with the environment of `horae.plugins.host.env` and an
  empty working directory, and the MCP handshake and
  `tools/list` must finish within the plugin's `startup_timeout_s` (`startup_timeout`);
- **runtime** (schemas decision 74): the plugin's effective `runtime` (`horae.yaml`) picks the
  spawn. `process` is the above; `container` starts the same MCP session in the plugin's own
  Docker container (`horae.plugins.host.container`: read-only, only its directory and its
  locked `permissions.paths` mounted (`horae.plugins.host.mounts`, decision 78), within its
  `limits`, secrets through stdin, and egress only through its own allow-list proxy built from
  the locked network permissions, `horae.plugins.host.egress`, decision 76). Whatever it lacks is
  `runtime_unavailable` and the plugin does not start: there is no fallback to `process`;
- **blocked egress** (decision 76 F): each attempt the proxy refused (host, port, reason; never
  a URL) becomes an `egress_blocked` violation of the call in flight, whether it succeeds or
  fails (`ToolResult.violations`, `ToolCallError.violations`), redacted like any message;
- **calls**: the tool must be in the manifest's `provides` and in `tools/list`
  (`tool_missing`); each call is bounded by its pipeline `timeout_s`, else the plugin's
  `call_timeout_s` (`horae.yaml`), else `plugin_defaults.call_timeout_s`;
- **failures**: after a timeout, a crash, an oversized or malformed message, the process is
  killed (its group: `SIGTERM`, then `SIGKILL`) and the call fails; the next call to the plugin
  starts it again, **once per run** (PR #85 answer 6). A second such failure, or any failed
  start, makes the plugin `plugin_unavailable` for the rest of the run. A timed-out call is
  never retried (no double side effects);
- **results**: `isError: true` is `tool_error`; the data is `structuredContent`, else a single
  text block that parses as JSON (PR #85 answer 9), else `output_invalid`; intents come from
  `_meta["dev.horae/intents"]` (a list) and are validated by the engine;
- **sink operations** (`call_operation`): the core, never a pipeline step, may call a tool a
  plugin lists in `tools/list` but not in `provides`, its sink operations
  (`horae.plugins.host.sink`, #25), through the same process, timeouts and redaction. Only
  `<ns>.<operation>` for an operation of `SINK_OPERATIONS` and a namespace of the plugin's
  sinks or notifiers, which no plugin provides (the registry refuses a plugin providing one,
  as the manifest contract does), else `tool_missing`;
- **trusted paths**: every request carries the plugin's path permissions as approved in
  `horae.lock` in `_meta["dev.horae/paths"]` (`horae.plugins.host.session`, #25);
- **trusted network**: and its network allow-list as approved in `horae.lock` (not the
  manifest's declared one) in `_meta["horae/network"]`, a list that is empty when the lock
  grants no host (#131), so a plugin can refuse a host the user did not approve before M5;
  its approved local-network endpoints (`permissions.network_local`, `host:port`) travel in
  `_meta["horae/network_local"]` the same way (decision 66, #132);
- **runtime.command is not pinned** against the lock in v1: the lock pins the plugin's
  version, commit and permissions, and the installed manifest's command is used as it is.
  Pinning it (and the plugin's files) is M4's install and verify work (#44, #46);
- **redaction** (PR #85 answer 4): every secret the instance resolved
  (`SecretResolver.resolved()`) and its encodings is replaced with `[REDACTED:<NAME>]` in
  stderr, error messages, tool output and intents. A secret found in the output or intents
  does not fail the call: it is redacted and recorded as a `secret_in_output` violation
  (`ToolResult.violations`). This is how the maintainer's answer "add the secrets from the
  output" is read: redact them, flag them, keep going. An object key holding a secret is
  never rewritten: the call fails with `output_invalid` and nothing is stored. The SDK's own
  log records are redacted too (`horae.plugins.host.logs`).

**Stderr** is captured per call (`horae.plugins.host.capture`): what the plugin wrote since
the previous call ended, until the pipe has been quiet for `STDERR_QUIET_S` after the result
(at most `STDERR_SETTLE_S`: a heuristic, stderr and stdout are separate pipes), or until the
process is gone after a failure. What a process wrote before it died between two calls goes
with the next call (its result or error); what it wrote after its last call is kept in
`PluginHost.leftover_stderr` (redacted) when it is released or the host exits.

Nothing here puts a secret into Horae's own environment: each child's mapping is built
explicitly and handed to the spawn call only. **Sibling plugins** (accepted risk until M5,
decision 35 A): every plugin runs as the same user, so while two plugins run at the same time
one can read the other's environment, hence its declared secrets (`/proc/<pid>/environ` on
Linux, `ps eww` on some systems). This is accepted for first-party plugins only (the
"declared" tier) and narrowed by stopping each plugin after its last planned call, so a plugin
is not alive during later steps that do not use it. A plugin with `runtime: container` sees
neither Horae's nor another plugin's processes, files or environment.
"""

import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import anyio
import anyio.abc
import mcp_types

from horae.config import Instance
from horae.config.documents import read_yaml, validate
from horae.contracts.instance import ContainerConfig, PluginDefaults
from horae.contracts.lockfile import LockedPlugin, Lockfile
from horae.contracts.plugin import (
    SINK_OPERATIONS,
    TOOL_KINDS,
    Permissions,
    PluginManifest,
    StdioRuntime,
    merge_personal_fields,
    reserved_tools,
    sink_namespaces,
)
from horae.contracts.run import Violation
from horae.engine.ports import ResolvedPlugin, ToolResult, UnknownPlugin
from horae.marketplace.install import INSTALL_RECORD
from horae.plugins.host import logs
from horae.plugins.host.capture import StderrCapture
from horae.plugins.host.container import ContainerRuntime
from horae.plugins.host.egress import violations as egress_violations
from horae.plugins.host.env import BASE_PATH, build_env, check_names, plugin_venv
from horae.plugins.host.errors import HostErrorKind, PluginCallError
from horae.plugins.host.mounts import MountPolicy
from horae.plugins.host.redact import MAX_EXPANSION, Redactor, SecretInKey
from horae.plugins.host.session import INTENTS_META, CallFailed, PluginProcess, Spawn
from horae.secrets import MAX_SECRET_BYTES, Secret, SecretError, SecretResolver

MANIFEST_FILE = "plugin.yaml"
_log = logging.getLogger("horae.plugins.host")
STDERR_QUIET_S = 0.05
STDERR_SETTLE_S = 0.5
CAPTURE_MARGIN = MAX_EXPANSION * MAX_SECRET_BYTES
"""Bytes each stderr window keeps past its cut: the longest pattern any secret can produce."""
_MAX_MESSAGE = 1000
_PROVIDED = TOOL_KINDS


class RegistryError(ValueError):
    """The installed plugins do not match `horae.lock` (the message names the plugin)."""


@dataclass(frozen=True)
class InstalledPlugin:
    """A plugin the host can start: its manifest with the locked permissions."""

    manifest: PluginManifest
    commit: str | None = None
    root: Path | None = None
    """The plugin's directory (relative commands are resolved against it)."""

    @property
    def resolved(self) -> ResolvedPlugin:
        return ResolvedPlugin(self.manifest, self.commit)


def _exceeds(asked: Permissions, approved: Permissions) -> list[str]:
    extra = [
        f"{field}: {', '.join(sorted(set(getattr(asked, field)) - set(getattr(approved, field))))}"
        for field in ("network", "network_local", "secrets", "optional_secrets", "intents")
        if not set(getattr(asked, field)) <= set(getattr(approved, field))
    ]
    for access in ("read", "write"):
        wanted = set(getattr(asked.paths, access)) - set(getattr(approved.paths, access))
        if wanted:
            extra.append(f"paths.{access}: {', '.join(sorted(wanted))}")
    return extra


_MAX_RECORD_BYTES = 64 * 1024


def check_install_record(name: str, locked: LockedPlugin, root: Path) -> None:
    """`RegistryError` when `<root>/.horae-install.json` exists and does not name the plugin,
    version, repo and commit that `horae.lock` pins (decision 70 D). No record: nothing to
    compare (a built-in or hand-copied plugin)."""
    path = root / INSTALL_RECORD
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RegistryError(f"plugin {name}: cannot read {INSTALL_RECORD}: {exc}") from None
    if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_RECORD_BYTES:
        raise RegistryError(f"plugin {name}: {INSTALL_RECORD} is not a small regular file")
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise RegistryError(f"plugin {name}: {INSTALL_RECORD} cannot be read") from None
    if not isinstance(record, dict):
        raise RegistryError(f"plugin {name}: {INSTALL_RECORD} is not an object")
    expected = {
        "name": name,
        "version": locked.version,
        "repo": locked.repo,
        "commit": locked.commit,
    }
    differ = sorted(key for key, value in expected.items() if record.get(key) != value)
    if differ:
        found = ", ".join(f"{key} {record.get(key)!r}" for key in differ)
        pinned = ", ".join(f"{key} {expected[key]!r}" for key in differ)
        raise RegistryError(
            f"plugin {name}: installed with {found} but horae.lock pins {pinned} "
            "(the installed tree is not the approved one: reinstall it with `horae plugin add`)"
        )


class HostRegistry:
    """A `PluginRegistry` over `horae.lock` and the installed manifests."""

    def __init__(
        self,
        lock: Lockfile,
        manifests: Mapping[str, PluginManifest],
        roots: Mapping[str, Path] | None = None,
    ) -> None:
        self._plugins: dict[str, InstalledPlugin] = {}
        self._tools: dict[str, str] = {}
        self._skills: dict[str, str] = {}
        for name, manifest in sorted(manifests.items()):
            locked = lock.plugins.get(name)
            if manifest.name != name or locked is None:
                raise RegistryError(f"plugin {name}: not pinned in horae.lock")
            if manifest.version != locked.version:
                raise RegistryError(
                    f"plugin {name}: version {manifest.version} is installed, "
                    f"horae.lock pins {locked.version}"
                )
            extra = _exceeds(manifest.permissions, locked.permissions)
            if extra:
                raise RegistryError(f"plugin {name}: permissions not approved ({'; '.join(extra)})")
            # Decision 68 B: the personal fields in force are those of the manifest and of
            # the lock together, so neither an edited manifest nor a stale lock drops one.
            provided = {
                ref
                for kind in ("sources", "transforms")
                for ref in getattr(manifest.provides, kind)
            }
            personal = merge_personal_fields(
                manifest.provides.personal_fields, locked.personal_fields, tools=provided
            )
            effective = manifest.model_copy(
                update={
                    "permissions": locked.permissions,
                    "provides": manifest.provides.model_copy(update={"personal_fields": personal}),
                }
            )
            root = (roots or {}).get(name)
            self._plugins[name] = InstalledPlugin(effective, locked.commit, root)
            provides = manifest.provides
            for ref in (ref for kind in _PROVIDED for ref in getattr(provides, kind)):
                self._claim(self._tools, ref, name)
            for skill in provides.skills:
                self._claim(self._skills, skill, name)
        for name, plugin in self._plugins.items():
            clash = sorted(set(self._tools) & reserved_tools(plugin.manifest.provides))
            if clash:
                raise RegistryError(
                    f"plugin {name}: {', '.join(clash)} is a sink operation of its namespace "
                    "and cannot be provided by any plugin"
                )

    @staticmethod
    def _claim(index: dict[str, str], ref: str, name: str) -> None:
        owner = index.setdefault(ref, name)
        if owner != name:
            raise RegistryError(f"{ref} is provided by both {owner} and {name}")

    @classmethod
    def from_directory(cls, lock: Lockfile, directory: Path) -> Self:
        """Read `<directory>/<name>/plugin.yaml` for every plugin in `lock`.

        Raises `ProblemsError` for an invalid manifest, `RegistryError` for one that does not
        match the lock, or an install record that does not (`check_install_record`). This is
        the layout `horae plugin add` produces."""
        manifests: dict[str, PluginManifest] = {}
        roots: dict[str, Path] = {}
        for name in sorted(lock.plugins):
            root = directory / name
            manifests[name] = validate(read_yaml(root / MANIFEST_FILE), PluginManifest)
            check_install_record(name, lock.plugins[name], root)
            roots[name] = root
        return cls(lock, manifests, roots)

    def plugins(self) -> tuple[InstalledPlugin, ...]:
        """Every installed plugin, by name."""
        return tuple(self._plugins.values())

    def installed(self, name: str) -> InstalledPlugin:
        try:
            return self._plugins[name]
        except KeyError:
            raise UnknownPlugin(name) from None

    def installed_for_tool(self, ref: str) -> InstalledPlugin:
        try:
            return self._plugins[self._tools[ref]]
        except KeyError:
            raise UnknownPlugin(ref) from None

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        return self.installed_for_tool(ref).resolved

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        try:
            return self._plugins[self._skills[skill]].resolved
        except KeyError:
            raise UnknownPlugin(skill) from None


def resolve_command(command: list[str], root: Path | None) -> list[str]:
    """`command` with `argv[0]` as an absolute path.

    - an absolute path is kept as written (resolving its symlinks would change `argv[0]`,
      which interpreters such as a virtual environment's `python` use to find their
      environment);
    - a relative path (with a `/`) is resolved against the plugin's directory with
      `Path.resolve()`, and its real path must stay inside the real plugin directory (no
      `../`, no symlink out of it);
    - a bare name is looked up with `shutil.which` on the child's `PATH` (absolute entries):
      for a plugin with its own venv (`<root>/.venv`, #45, decision 69) its `bin` first, so
      `python`, `python3` or a console script of the plugin run from that venv; the path is
      kept as found (the venv's `python` is a symlink its interpreter needs as `argv[0]`).

    `spawn_failed` if it is not found, not an executable file, or escapes the plugin."""
    first = command[0]
    path: str | None
    if os.path.isabs(first):
        path = first
    elif "/" in first:
        path = None
        if root is not None:
            real_root = root.resolve()
            real = (real_root / first).resolve()
            if not real.is_relative_to(real_root):
                raise PluginCallError(
                    "spawn_failed", f"command {first!r} is outside the plugin's directory"
                )
            path = str(real)
    else:
        venv = plugin_venv(root)
        search = BASE_PATH if venv is None else f"{venv / 'bin'}:{BASE_PATH}"
        path = shutil.which(first, path=search)
    if path is None or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise PluginCallError("spawn_failed", f"command {first!r} not found or not executable")
    return [os.path.abspath(path), *command[1:]]


@dataclass
class _Entry:
    """One plugin's state in this run."""

    installed: InstalledPlugin
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    process: PluginProcess | None = None
    starts: int = 0
    failures: int = 0
    """Processes lost to a timeout, crash or protocol failure (one restart allowed)."""
    dead: str | None = None
    """Why the plugin is unavailable for the rest of the run."""
    pending_stderr: str | None = None
    """What a process wrote before it died between two calls (goes with the next call)."""


def _text(result: mcp_types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


class PluginHost:
    """The run's `ToolCaller` (see the module docstring)."""

    def __init__(
        self,
        registry: HostRegistry,
        *,
        secrets: SecretResolver,
        work_dir: Path,
        timezone: str = "UTC",
        timeouts: Callable[[str], PluginDefaults] | None = None,
        container: ContainerConfig | None = None,
        mounts: MountPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.secrets = secrets
        self.timezone = timezone
        self._work_root = work_dir
        self._timeouts = timeouts or (lambda _name: PluginDefaults())
        self._containers = ContainerRuntime(container, mounts=mounts)
        """Starts the plugins whose runtime is `container` (schemas decision 74)."""
        self._entries: dict[str, _Entry] = {}
        self._group: anyio.abc.TaskGroup | None = None
        self._run_dir: Path | None = None
        self._redactor: tuple[tuple[Secret, ...], Redactor] | None = None
        self.leftover_stderr: dict[str, str] = {}
        """Per plugin, the (redacted) stderr written after its last call, kept when it was
        released or the host exited."""

    # --- lifecycle -------------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._resolve_known_secrets()
        self._run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=self._work_root))
        logs.register(self)
        self._group = anyio.create_task_group()
        await self._group.__aenter__()
        return self

    def _resolve_known_secrets(self) -> None:
        """Resolve every declared and referenced secret, for redaction (decision 35 B)."""
        names = {
            name
            for plugin in self.registry.plugins()
            for name in plugin.manifest.permissions.all_secrets
        }
        names.update(self.secrets.referenced())
        for name in sorted(names):
            try:
                self.secrets.resolve(name)
            except SecretError:
                continue  # the plugin that needs it fails with `secret_missing`

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        group, self._group = self._group, None
        try:
            with anyio.CancelScope(shield=True):
                async with anyio.create_task_group() as stopping:
                    for entry in self._entries.values():
                        if entry.process is not None:
                            stopping.start_soon(entry.process.stop)
                for entry in self._entries.values():
                    self._keep_leftover(entry)
            if group is not None:
                group.cancel_scope.cancel()
                await group.__aexit__(None, None, None)
        finally:
            logs.unregister(self)
            if self._run_dir is not None:
                shutil.rmtree(self._run_dir, ignore_errors=True)
        return None

    async def release(self, plugin: str) -> None:
        """Stop `plugin`'s process: the run will not call it again (decision 35 A). A later
        call would start it again (without using its one restart). Never raises."""
        entry = self._entries.get(plugin)
        if entry is None:
            return
        async with entry.lock:
            process, entry.process = entry.process, None
            if process is not None:
                await process.stop()
            self._keep_leftover(entry, process)

    def _keep_leftover(self, entry: _Entry, process: PluginProcess | None = None) -> None:
        """Keep what `entry`'s process wrote after its last call in `leftover_stderr`."""
        process = process or entry.process
        parts = [entry.pending_stderr]
        entry.pending_stderr = None
        if process is not None:
            parts.append(process.capture.take(self.redactor()))
        text = "".join(part for part in parts if part)
        if text:
            name = entry.installed.manifest.name
            self.leftover_stderr[name] = self.leftover_stderr.get(name, "") + text

    # --- redaction -------------------------------------------------------------------------

    def redactor(self) -> Redactor:
        """A redactor of every secret resolved so far (rebuilt when they change). Every
        secret is at least `MIN_SECRET_BYTES` long (the resolver refuses a shorter one,
        schemas decision 43 H, superseding the short-secret handling of decision 35 D)."""
        resolved = self.secrets.resolved()
        if self._redactor is None or self._redactor[0] != resolved:
            self._redactor = (resolved, Redactor(resolved))
        return self._redactor[1]

    def _redact_message(self, message: str) -> str:
        """`message` redacted as a whole first (a secret may contain or straddle `": "`); then
        the body of the redacted `<ref>: <body>` message is also matched as a whole value."""
        redactor = self.redactor()
        redacted, _ = redactor.redact_text(message)
        prefix, separator, body = redacted.partition(": ")
        if not separator:
            return redacted
        return prefix + separator + redactor.redact_text(body)[0]

    def _redacted(self, error: PluginCallError) -> PluginCallError:
        message = self._redact_message(error.message)
        if len(message) > _MAX_MESSAGE:
            message = message[: _MAX_MESSAGE - 1] + "…"
        kind: HostErrorKind = error.host_kind
        return PluginCallError(kind, message, stderr=error.stderr, violations=error.violations)

    # --- calls -----------------------------------------------------------------------------

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        """Call tool `ref` (see the module docstring); raises `PluginCallError` only."""
        try:
            return await self._call(ref, params, input, timeout_s)
        except PluginCallError as error:
            raise self._redacted(error) from None
        except Exception:  # a host bug must never crash the run, nor leak a message
            raise PluginCallError("host_error", f"{ref}: unexpected host error") from None

    async def call_operation(
        self,
        plugin: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> ToolResult:
        """Call `plugin`'s sink operation `tool` (`horae.plugins.host.sink`), a tool it lists in
        `tools/list` but not in `provides`, which only the core calls, never a pipeline step
        (the engine's calls go through `call`, which requires `provides`). `tool` must be
        `<ns>.<operation>` with `operation` in `SINK_OPERATIONS` and `<ns>` the namespace of
        one of the plugin's `provides.sinks` or `provides.notifiers`, and no plugin may
        provide it (`tool_missing` otherwise: this is not a way to call arbitrary unprovided
        tools). Same process, timeouts, restarts, results and redaction as `call`; no input
        `_meta`. Raises `PluginCallError` only."""
        try:
            try:
                installed = self.registry.installed(plugin)
            except UnknownPlugin:
                raise PluginCallError("tool_missing", f"no installed plugin {plugin}") from None
            namespace, _, operation = tool.partition(".")
            provided = True
            try:
                self.registry.installed_for_tool(tool)
            except UnknownPlugin:
                provided = False
            if (
                operation not in SINK_OPERATIONS
                or namespace not in sink_namespaces(installed.manifest.provides)
                or provided
            ):
                raise PluginCallError("tool_missing", f"{tool} is not a sink operation of {plugin}")
            return await self._call_on(installed, tool, arguments, None, timeout_s)
        except PluginCallError as error:
            raise self._redacted(error) from None
        except Exception:  # a host bug must never crash the run, nor leak a message
            raise PluginCallError("host_error", f"{tool}: unexpected host error") from None

    async def _call(
        self, ref: str, params: dict[str, Any], input: Any, timeout_s: float | None
    ) -> ToolResult:
        try:
            installed = self.registry.installed_for_tool(ref)
        except UnknownPlugin:
            raise PluginCallError("tool_missing", f"no installed plugin provides {ref}") from None
        return await self._call_on(installed, ref, params, input, timeout_s)

    async def _call_on(
        self,
        installed: InstalledPlugin,
        ref: str,
        params: dict[str, Any],
        input: Any,
        timeout_s: float | None,
    ) -> ToolResult:
        if self._group is None:
            raise PluginCallError("host_error", "the plugin host is not open")
        name = installed.manifest.name
        entry = self._entries.setdefault(name, _Entry(installed))
        async with entry.lock:
            try:
                outcome = await self._call_locked(entry, ref, params, input, timeout_s)
            except PluginCallError as error:
                pending = self._pending(entry, error.stderr)
                raise PluginCallError(
                    error.host_kind, error.message, stderr=pending, violations=error.violations
                ) from None
            pending = self._pending(entry, outcome.stderr)
            if pending is outcome.stderr:
                return outcome
            return ToolResult(
                data=outcome.data,
                intents=outcome.intents,
                stderr=pending,
                violations=outcome.violations,
            )

    def _pending(self, entry: _Entry, stderr: str | None) -> str | None:
        """`stderr` after what an earlier process wrote before it died, if anything."""
        pending, entry.pending_stderr = entry.pending_stderr, None
        if pending is None:
            return stderr
        return f"--- previous process ---\n{pending}" + (stderr or "")

    async def _call_locked(
        self, entry: _Entry, ref: str, params: dict[str, Any], input: Any, timeout_s: float | None
    ) -> ToolResult:
        name = entry.installed.manifest.name
        process = await self._ready(entry)
        if ref not in process.tools:
            raise PluginCallError("tool_missing", f"{name} does not list {ref} in tools/list")
        timeout = timeout_s or self._timeouts(name).call_timeout_s
        permissions = entry.installed.manifest.permissions  # the locked ones (`HostRegistry`)
        locked = permissions.paths
        paths = {"read": list(locked.read), "write": list(locked.write)}
        try:
            result = await process.call(
                ref,
                params,
                input,
                timeout,
                paths=paths,
                network=permissions.network,
                network_local=permissions.network_local,
            )
        except CallFailed as failed:
            await process.stop(forced=True)
            self._lost(entry)
            stderr = process.capture.take(self.redactor())
            blocked = self._blocked(process)
            raise PluginCallError(
                failed.kind, f"{ref}: {failed.message}", stderr=stderr, violations=blocked
            ) from None
        except PluginCallError as error:
            stderr = await self._stderr(process)
            kind: HostErrorKind = error.host_kind
            raise PluginCallError(
                kind, f"{ref}: {error.message}", stderr=stderr, violations=self._blocked(process)
            ) from None
        stderr = await self._stderr(process)
        blocked = self._blocked(process)
        try:
            outcome = self._result(ref, result, stderr)
        except PluginCallError as error:
            raise PluginCallError(
                error.host_kind, error.message, stderr=error.stderr, violations=blocked
            ) from None
        if not blocked:
            return outcome
        return ToolResult(
            data=outcome.data,
            intents=outcome.intents,
            stderr=outcome.stderr,
            violations=blocked + outcome.violations,
        )

    def _blocked(self, process: PluginProcess) -> tuple[Violation, ...]:
        """The egress attempts `process`'s proxy refused since the last call, as redacted
        `egress_blocked` violations (a refused host name may carry a secret)."""
        proxy = process.spawn.egress
        if proxy is None:
            return ()
        details = [self._redact_message(detail)[:_MAX_MESSAGE] for detail in proxy.take()]
        return egress_violations(details)

    def _lost(self, entry: _Entry) -> None:
        """The process failed: it may be started once more in this run."""
        entry.process = None
        entry.failures += 1
        if entry.failures >= 2:
            entry.dead = "it failed twice in this run (one restart allowed)"

    async def _ready(self, entry: _Entry) -> PluginProcess:
        name = entry.installed.manifest.name
        if entry.process is not None and not entry.process.alive:
            dead = entry.process  # it died between two calls: keep what it wrote
            await dead.stop(forced=True)
            entry.pending_stderr = dead.capture.take(self.redactor())
            self._lost(entry)
        if entry.dead is not None:
            raise PluginCallError(
                "plugin_unavailable", f"plugin {name} is unavailable: {entry.dead}"
            )
        if entry.process is not None:
            return entry.process
        entry.starts += 1
        try:
            process = await self._start(entry)
        except PluginCallError as error:
            entry.dead = "it could not be started"
            raise error
        entry.process = process
        return process

    async def _start(self, entry: _Entry) -> PluginProcess:
        manifest = entry.installed.manifest
        name = manifest.name
        runtime = manifest.runtime
        if not isinstance(runtime, StdioRuntime):
            what = "no runtime" if runtime is None else f"runtime {runtime.protocol}"
            raise PluginCallError("runtime_unsupported", f"plugin {name} has {what}")
        permissions = manifest.permissions
        check_names(name, permissions.all_secrets)
        values: dict[str, Secret] = {}
        for secret in permissions.secrets:
            try:
                values[secret] = self.secrets.resolve(secret)
            except SecretError as error:
                raise PluginCallError("secret_missing", str(error)) from None
        for secret in permissions.optional_secrets:
            try:
                values[secret] = self.secrets.resolve(secret)
            except SecretError:
                continue  # absent from the environment: the plugin says so if a call needs it
        if self._run_dir is None or self._group is None:  # pragma: no cover (checked in _call)
            raise PluginCallError("host_error", "the plugin host is not open")
        settings = self._timeouts(name)
        base = self._run_dir / f"{name}.{entry.starts}"
        cwd = base / "cwd"
        spawn: Spawn
        if settings.runtime == "container":
            # Fail closed (decision 74 H): `runtime_unavailable`, never the process runtime.
            cwd.mkdir(mode=0o700, parents=True)
            spawn = await self._containers.spawn(
                name,
                root=entry.installed.root,
                command=list(runtime.command),
                secrets=values,
                timezone=self.timezone,
                cwd=cwd,
                startup_timeout_s=settings.startup_timeout_s,
                network=permissions.network,
                network_local=permissions.network_local,
                paths_read=permissions.paths.read,
                paths_write=permissions.paths.write,
                limits=settings.limits,
                shared=self._container_writers(name),
            )
        else:
            argv = resolve_command(list(runtime.command), entry.installed.root)
            home, tmpdir = base / "home", base / "tmp"
            for directory in (home, tmpdir, cwd):
                directory.mkdir(mode=0o700, parents=True)
            env = build_env(
                name,
                home=home,
                tmpdir=tmpdir,
                timezone=self.timezone,
                secrets=values,
                venv=plugin_venv(entry.installed.root),
            )
            spawn = Spawn(name, tuple(argv), env, cwd, settings.startup_timeout_s)
        process = PluginProcess(spawn, StderrCapture(CAPTURE_MARGIN))
        if spawn.egress is not None:
            self._group.start_soon(spawn.egress.watch)
        self._group.start_soon(process.serve)
        await process.ready.wait()
        if process.error is None and spawn.egress is not None:
            try:  # its proxy serves this container only (decision 76 C)
                await self._containers.admit(name, spawn.egress)
            except PluginCallError as error:
                process.error = error
        if process.error is not None:
            await process.stop(forced=True)
            stderr = process.capture.take(self.redactor())
            raise PluginCallError(process.error.host_kind, process.error.message, stderr=stderr)
        return process

    def _container_writers(self, name: str) -> list[tuple[str, str]]:
        """The locked writable paths of every other plugin that runs in a container (decision
        78 B). Process-runtime plugins are left out: they run unconfined anyway (T04)."""
        return [
            (other.manifest.name, path)
            for other in self.registry.plugins()
            if other.manifest.name != name
            and self._timeouts(other.manifest.name).runtime == "container"
            for path in other.manifest.permissions.paths.write
        ]

    async def _stderr(self, process: PluginProcess) -> str | None:
        """The call's stderr, once the pipe has been quiet (see the module docstring)."""
        start = anyio.current_time()
        while True:
            now = anyio.current_time()
            quiet = now - max(start, process.last_stderr)
            if quiet >= STDERR_QUIET_S or now - start >= STDERR_SETTLE_S:
                break
            await anyio.sleep(STDERR_QUIET_S - quiet)
        return process.capture.take(self.redactor())

    def _result(self, ref: str, result: mcp_types.CallToolResult, stderr: str | None) -> ToolResult:
        def invalid(message: str) -> PluginCallError:
            return PluginCallError("output_invalid", f"{ref}: {message}", stderr=stderr)

        if result.is_error:
            text = _text(result) or "the tool reported an error"
            raise PluginCallError("tool_error", f"{ref}: {text}", stderr=stderr)
        if result.structured_content is not None or "structured_content" in result.model_fields_set:
            data = result.structured_content
        elif len(result.content) == 1 and result.content[0].type == "text":
            try:
                data = json.loads(result.content[0].text)
            except (ValueError, RecursionError):
                raise invalid("the text result is not JSON (or nested too deeply)") from None
        else:
            raise invalid("no structured content and not a single JSON text block")
        intents = (result.meta or {}).get(INTENTS_META, [])
        if not isinstance(intents, list):
            raise invalid(f"_meta[{INTENTS_META!r}] is not a list")
        redactor = self.redactor()
        try:
            data, in_data = redactor.redact_data(data)
            intents, in_intents = redactor.redact_data(intents)
        except SecretInKey as error:
            raise invalid(f"an object key holds secret {error.name} (nothing stored)") from None
        violations: tuple[Violation, ...] = ()
        found = in_data | in_intents
        if found:
            where = " and ".join(
                part for part, hit in (("output", in_data), ("intents", in_intents)) if hit
            )
            detail = f"{ref} returned secret {', '.join(sorted(found))} in its {where} (redacted)"
            violations = (Violation(kind="secret_in_output", detail=detail),)
        return ToolResult(data=data, intents=intents, stderr=stderr, violations=violations)


def host_for_instance(
    instance: Instance, registry: HostRegistry, *, secrets: SecretResolver | None = None
) -> PluginHost:
    """A `PluginHost` for `instance` (`horae.config.Instance`): its time zone, plugin timeouts
    and runtimes, container settings and mount policy (decision 78 B), secret resolver and
    `<state_dir>/plugin-runs` as the work directory."""
    return PluginHost(
        registry,
        secrets=secrets or instance.secret_resolver(),
        work_dir=instance.state_dir / "plugin-runs",
        timezone=instance.config.timezone,
        timeouts=instance.plugin_timeouts,
        container=instance.config.container,
        mounts=MountPolicy.for_instance(
            instance.root,
            state_dir=instance.state_dir,
            secrets_dir=instance.secrets_dir,
            mount_roots=instance.config.container.mount_roots,
        ),
    )


__all__ = [
    "CAPTURE_MARGIN",
    "MANIFEST_FILE",
    "HostRegistry",
    "InstalledPlugin",
    "PluginHost",
    "RegistryError",
    "host_for_instance",
    "resolve_command",
]
