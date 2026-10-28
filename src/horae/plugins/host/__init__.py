# SPDX-License-Identifier: Apache-2.0
"""The plugin host v1, "declared" tier: MCP stdio plugins with declared secrets only (#24).

- `horae.plugins.host.host`: `HostRegistry` (a `PluginRegistry` over `horae.lock` and the
  manifests) and `PluginHost` (the run's `ToolCaller`): process model, timeouts, restarts,
  results, redaction;
- `horae.plugins.host.env`: the child environment and its deny list;
- `horae.plugins.host.transport`: spawn (argv, exact env, new session), JSON-RPC pipes,
  shutdown;
- `horae.plugins.host.session`: one process, its MCP session and tool calls;
- `horae.plugins.host.capture` and `horae.plugins.host.redact`: stderr capture and redaction;
- `horae.plugins.host.logs`: redaction of the MCP SDK's own log records;
- `horae.plugins.host.errors`: the error kinds.

**Not enforced until M5** (ADR 0003, "declared" tier). A plugin runs as the same user as Horae:
it can reach any host, read and write any path the user can (its empty `HOME`, `TMPDIR` and
working directory are hygiene, not isolation), use unbounded CPU and memory, and on Linux read
`/proc/<horae-pid>/environ`, i.e. every secret Horae itself got through **its own**
environment. Horae never adds a resolved secret to its own environment (each child's is built
explicitly), so secrets read from files (`/run/secrets/<NAME>`, the default, schemas decision
25) are not exposed that way; `{env: VAR}` references are. Prefer secret files. Likewise a
plugin can read the environment, hence the declared secrets, of another plugin running at the
same time (accepted for first-party plugins; each plugin is stopped after its last planned
call to narrow it, schemas decision 35 A). The proper fix
(one container per plugin, M5 #50/#51) is tracked there. `undeclared_secret`,
`undeclared_path` and `egress_blocked` (network) are not detected in v1. The MCP-backed `Sink`
(apply port) is a follow-up: sinks and notifiers are not called through this host yet.
"""

from horae.plugins.host.errors import HostErrorKind, PluginCallError
from horae.plugins.host.host import (
    HostRegistry,
    InstalledPlugin,
    PluginHost,
    RegistryError,
    host_for_instance,
)

__all__ = [
    "HostErrorKind",
    "HostRegistry",
    "InstalledPlugin",
    "PluginCallError",
    "PluginHost",
    "RegistryError",
    "host_for_instance",
]
