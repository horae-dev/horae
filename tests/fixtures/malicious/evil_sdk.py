# SPDX-License-Identifier: Apache-2.0
"""`EvilPlugin`: a `horae_sdk.Plugin` with the SDK's own checks taken out (#52).

The SDK refuses, in the plugin's process, an undeclared intent, more than `MAX_INTENTS` of
them and an output too large for the host (`Plugin.result`). Those are self-checks for honest
plugins: a malicious one simply skips them. Every malicious fixture of the process runtime is
an `EvilPlugin`, so what reaches the host is exactly what the attacker chose, and the core's
own checks are the ones under test. A tool may also return `Fail(message)`, a tool error with
a message of the attacker's choosing (the SDK would only say the exception's type).
"""

import json
from dataclasses import dataclass
from typing import Any

from mcp_types import CallToolResult, TextContent

from horae_sdk import Output, Plugin
from horae_sdk.protocol import INTENTS_META


@dataclass(frozen=True)
class Fail:
    """A tool error whose text the plugin chooses."""

    message: str


class EvilPlugin(Plugin):
    """A `Plugin` whose results are sent as written, unchecked."""

    def result(self, ref: str, value: Any) -> CallToolResult:
        if isinstance(value, Fail):
            return CallToolResult(content=[TextContent(text=value.message)], is_error=True)
        output = value if isinstance(value, Output) else Output(value)
        intents = [dict(intent) for intent in output.intents]
        meta = {INTENTS_META: intents} if intents else None
        if isinstance(output.data, dict):
            return CallToolResult(
                content=[TextContent(text="ok")], structured_content=output.data, _meta=meta
            )
        return CallToolResult(content=[TextContent(text=json.dumps(output.data))], _meta=meta)
