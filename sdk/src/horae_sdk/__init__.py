# SPDX-License-Identifier: Apache-2.0
"""horae-sdk: write Horae plugins in Python.

A Horae plugin is an MCP server over stdio that the core's plugin host starts for one run
(ADR 0003). This SDK speaks exactly the host's contract (`horae.plugins.host`):

- `horae_sdk.plugin`: `Plugin`, the base classes of the host's roles (`Source`, `Transform`,
  `Sink`, `Notifier`), `Call` (the trusted parameters, the transform input and the locked
  paths) and `Output` (data and proposed intents);
- `horae_sdk.sinks`: the sink operations the core calls (`<ns>.read`, `<ns>.apply`,
  `<ns>.restore`, `<ns>.parent_exists`, `<ns>.identity`);
- `horae_sdk.manifest`: `plugin.yaml` helpers;
- `horae_sdk.intents`: builders of the closed set of intents (ADR 0002);
- `horae_sdk.tasks`: Obsidian Tasks syntax, with the title escaping of the threat model (T02);
- `horae_sdk.testing`: the recorded-input test harness.
"""

from horae_sdk import intents, tasks
from horae_sdk.manifest import Permissions, dump_manifest, manifest
from horae_sdk.plugin import Call, Output, Paths, Plugin, PluginError, Source, Transform
from horae_sdk.sinks import Applied, ApplyRequest, Notifier, Sink, SinkError

__version__ = "0.1.0"

__all__ = [
    "Applied",
    "ApplyRequest",
    "Call",
    "Notifier",
    "Output",
    "Paths",
    "Permissions",
    "Plugin",
    "PluginError",
    "Sink",
    "SinkError",
    "Source",
    "Transform",
    "__version__",
    "dump_manifest",
    "intents",
    "manifest",
    "tasks",
]
