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
    description="Greets someone and proposes tasks to greet them back and to reply.",
)


@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output:
    """Greet `name` (a trusted parameter); propose a task to greet them back."""
    task = intents.task_create(f"Greet {name} back", key=intents.fingerprint("hello", name))
    return Output({"greeting": f"Hello, {name}!"}, intents=[task])


@plugin.transform("hello.shout")
def shout(call: Call) -> Output:
    """Upper-case the strings of the previous step's data. That data is untrusted (it may
    be a mail): its `subject` only reaches a task through `task_create`, which escapes it."""
    data: dict[str, Any] = call.input if isinstance(call.input, dict) else {}
    loud = {key: value.upper() if isinstance(value, str) else value for key, value in data.items()}
    subject = data.get("subject")
    if not isinstance(subject, str):
        return Output(loud)
    key = intents.fingerprint("hello", "reply", subject)
    return Output(loud, intents=[intents.task_create(f"Reply to {subject}", key=key)])


if __name__ == "__main__":
    plugin.run()
