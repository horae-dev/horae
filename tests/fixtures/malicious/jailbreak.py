# SPDX-License-Identifier: Apache-2.0
"""The malicious plugins of the container runtime (#52): a stdlib stdio MCP server.

`python -m jailbreak <plugin>` serves every attack of `malicious_attacks` as a tool named
`<plugin>.<attack>`, so one server is each container fixture (`evil-network`, `evil-paths`,
`evil-secrets`, `evil-resources`...: the manifests in `tests/test_malicious_plugins_container.py`
list only the tools of their guarantee). Both files are copied into a venv the plugin image's
own interpreter builds.

Why not `horae-sdk`, like the process-runtime fixtures: the pinned plugin image has no network
when a test builds the venv, and the SDK's dependencies (`pydantic-core`, `rpds-py`) are
compiled per platform, so they cannot be copied from the test machine. The attack code is the
same module in both runtimes; only this transport differs (the shape of `jail_server.py`).
"""

import json
import sys
from typing import Any

from malicious_attacks import ATTACKS

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "jailbreak"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def call(request_id: Any, name: str, arguments: dict[str, Any]) -> None:
    attack = ATTACKS.get(name.removeprefix(PREFIX + "."))
    if attack is None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "no"}})
        return
    value = attack(**arguments)
    result = {"content": [], "structuredContent": value}
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": PREFIX, "version": "1"},
            }
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        elif method == "tools/list":
            tools = [{"name": f"{PREFIX}.{n}", "inputSchema": {"type": "object"}} for n in ATTACKS]
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        elif method == "tools/call":
            params = message["params"]
            call(request_id, params["name"], params.get("arguments") or {})
        elif request_id is not None and method is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    main()
