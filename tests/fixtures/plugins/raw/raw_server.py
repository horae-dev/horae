# SPDX-License-Identifier: Apache-2.0
"""A hand-written stdio JSON-RPC server that misbehaves on purpose (plugin host tests, #24).

`raw_server.py <mode>`; modes: `normal`, `silent` (never answers: a startup timeout), `exit`
(exits with code 5 at once), `stubborn` (answers, then ignores stdin EOF and `SIGTERM` until
it is killed). Tools (all `raw.*`) misbehave in the ways the host must classify; `raw.init`
returns the client's `initialize` parameters, `raw.ask` sends the client a request
(`method`, `params`) and returns its answer.
"""

import json
import os
import signal
import sys
import time
from typing import Any

TOOLS = [
    "raw.ok",
    "raw.junk",
    "raw.big",
    "raw.rpc_error",
    "raw.ask",
    "raw.init",
    "raw.progress",
    "raw.log",
    "raw.bad_result",
    "raw.bad_intents",
    "raw.two_blocks",
    "raw.not_json",
    "raw.null",
    "raw.close",
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id: Any, value: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


INITIALIZE: dict[str, Any] = {}


def call(request_id: Any, name: str, arguments: dict[str, Any]) -> None:
    if name == "raw.ok":
        result(request_id, {"content": [], "structuredContent": {"ok": True}})
    elif name == "raw.junk":
        sys.stdout.write("this is not json-rpc\n")
        sys.stdout.flush()
    elif name == "raw.big":
        sys.stdout.write("x" * (9 * 1024 * 1024))
        sys.stdout.flush()
    elif name == "raw.rpc_error":
        error = {"code": -32602, "message": "unknown argument"}
        send({"jsonrpc": "2.0", "id": request_id, "error": error})
    elif name == "raw.ask":
        method, params = arguments["method"], arguments.get("params", {})
        send({"jsonrpc": "2.0", "id": "s1", "method": method, "params": params})
        answer = json.loads(sys.stdin.readline())
        result(request_id, {"content": [], "structuredContent": {"answer": answer}})
    elif name == "raw.init":
        result(request_id, {"content": [], "structuredContent": INITIALIZE})
    elif name == "raw.progress":  # malformed: `progress` must be a number
        params = {"progressToken": arguments["token"], "progress": arguments["token"]}
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})
        result(request_id, {"content": [], "structuredContent": {"ok": True}})
    elif name == "raw.log":
        params = {"level": "info", "data": {"token": arguments["token"]}}
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": params})
        result(request_id, {"content": [], "structuredContent": {"ok": True}})
    elif name == "raw.bad_result":
        result(request_id, {"content": "not a list"})
    elif name == "raw.bad_intents":
        meta = {"dev.horae/intents": {"type": "notify.push"}}
        result(request_id, {"content": [], "structuredContent": {}, "_meta": meta})
    elif name == "raw.two_blocks":
        blocks = [{"type": "text", "text": "1"}, {"type": "text", "text": "2"}]
        result(request_id, {"content": blocks})
    elif name == "raw.not_json":
        result(request_id, {"content": [{"type": "text", "text": "hello"}]})
    elif name == "raw.null":
        result(request_id, {"content": [], "structuredContent": None})
    elif name == "raw.close":
        os.close(1)
        time.sleep(30)


def main(mode: str) -> None:
    if mode == "exit":
        sys.exit(5)
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for line in sys.stdin:
        if mode == "silent":
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            INITIALIZE.update(message["params"])
            version = message["params"]["protocolVersion"]
            info = {"name": "raw", "version": "1"}
            result(
                request_id,
                {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": info},
            )
        elif method == "tools/list":
            tools = [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS]
            result(request_id, {"tools": tools})
        elif method == "tools/call":
            params = message["params"]
            call(request_id, params["name"], params.get("arguments") or {})
        elif request_id is not None and method is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if mode == "stubborn":
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "normal")
