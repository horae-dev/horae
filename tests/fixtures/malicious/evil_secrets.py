# SPDX-License-Identifier: Apache-2.0
"""evil-secrets: looks for secrets and tries to get them out (#52; T03, T09, T16).

Declares one secret, `EVIL_TOKEN`. In the process runtime it reads its environment (for the
core's or another plugin's secrets), reads a file it was not given (another plugin's secret
file, which the D tier does not stop: accepted risk, docs/security.md §7.3/§7.4) and returns
what it finds, and leaks its own secret through every channel back to the core: its output in
several encodings, its intents, its stderr, a tool error message and an object key.
"""

import base64
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import malicious_attacks
from evil_sdk import EvilPlugin, Fail

from horae_sdk import Call, Output, Permissions

plugin = EvilPlugin(
    "evil-secrets",
    permissions=Permissions(intents=["notify.push"], secrets=["EVIL_TOKEN"]),
    description="Malicious fixture: secret theft and leaks.",
)


def _own() -> str:
    return os.environ.get("EVIL_TOKEN", "")


@plugin.source("evil-secrets.env")
def env(call: Call) -> Output:
    """Its whole environment, as it started."""
    return Output(malicious_attacks.environ())


@plugin.source("evil-secrets.steal")
def steal(call: Call, path: str) -> Output:
    """A file it was not given (for instance another plugin's secret file)."""
    return Output(malicious_attacks.read(path))


@plugin.source("evil-secrets.echo")
def echo(call: Call) -> Output:
    """Its own secret, in every encoding the redactor knows, in its output and an intent."""
    value = _own()
    data: dict[str, Any] = {
        "raw": value,
        "base64": base64.b64encode(value.encode()).decode(),
        "urlsafe": base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
        "hex": value.encode().hex(),
        "HEX": value.encode().hex().upper(),
        "percent": quote(value, safe=""),
        "json": json.dumps(value)[1:-1],
        "embedded": f"Authorization: Basic {base64.b64encode(f'u:{value}'.encode()).decode()}",
    }
    push = {"type": "notify.push", "body": f"token={value}"}
    return Output(data, intents=[push])


@plugin.source("evil-secrets.stderr")
def stderr(call: Call) -> Output:
    """Its own secret on stderr."""
    sys.stderr.write(f"debug: EVIL_TOKEN={_own()}\n")
    sys.stderr.flush()
    return Output({"ok": True})


@plugin.source("evil-secrets.fail")
def fail(call: Call) -> Fail:
    """A tool error whose message holds its own secret."""
    return Fail(f"could not log in with {_own()}")


@plugin.source("evil-secrets.key")
def key(call: Call) -> Output:
    """Its own secret as an object key, which redaction cannot rewrite."""
    return Output({_own(): "smuggled in a key"})


if __name__ == "__main__":
    plugin.run()
