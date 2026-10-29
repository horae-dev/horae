# SPDX-License-Identifier: Apache-2.0
"""hello: the smallest useful Horae plugin, a source and a transform (horae-sdk example).

Run it with `python hello_plugin.py`: it serves MCP over stdio, as the core's plugin host
starts it. `plugin.yaml` next to it is `plugin.manifest(...)` written out.
"""

from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, intents

plugin = Plugin(
    "hello",
    version="0.1.0",
    permissions=Permissions(intents=["task.create"]),
    description="Greets someone and proposes a task to greet them back.",
)


@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output:
    """Greet `name`; propose a task (its title escaped for Obsidian Tasks)."""
    task = intents.task_create(f"Greet {name} back", fingerprint=intents.fingerprint("hello", name))
    return Output({"greeting": f"Hello, {name}!"}, intents=[task])


@plugin.transform("hello.shout")
def shout(call: Call) -> dict[str, Any]:
    """Upper-case the strings of the previous step's data."""
    data = call.input if isinstance(call.input, dict) else {}
    return {key: value.upper() if isinstance(value, str) else value for key, value in data.items()}


if __name__ == "__main__":
    plugin.run()
