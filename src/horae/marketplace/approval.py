# SPDX-License-Identifier: Apache-2.0
"""Permission approval at install (#46, schemas decision 70).

- `require_installable(name, tier, index)`: before M5 only `first-party` marketplace plugins install
  (docs/security.md T04); a direct install from a git URL (#48) has no tier and passes `None`,
  which is refused the same way while `DIRECT_INSTALLS` is `False`. One check for both.
- `with_grants(asked, hosts, local)`: what the manifest asks for, plus the hosts and
  local-network endpoints the user adds at approval (`--allow-host`, `--allow-local`). A lock
  may grant more than a manifest declares (the plugin host refuses only less, decision 66);
  this is how a user approves their own school or Home Assistant host.
- `describe(...)`: the approval text, grouped and labelled plainly.
- `lock_mismatch(existing, wanted)`: why `--yes` cannot approve `wanted` from the lock entry
  already there, or `None` when the entry is for the same version and commit with identical
  permissions.
"""

from collections.abc import Sequence

from pydantic import TypeAdapter, ValidationError

from horae.contracts.instance import DEFAULT_MARKETPLACE
from horae.contracts.intents import IntentType
from horae.contracts.lockfile import LockedPlugin
from horae.contracts.marketplace import Tier
from horae.contracts.plugin import Permissions
from horae.contracts.types import Hostname, LocalEndpoint
from horae.marketplace.errors import InvalidReference, TierRefused

DIRECT_INSTALLS = False
"""Whether a direct install from a git URL (#48, no marketplace tier) may install. `False`
before M5 (docs/security.md T04); lifting the gate is setting this to `True`."""

INSTALLABLE_TIERS: frozenset[str] = frozenset({"first-party"})
"""The tiers that install while plugins run as plain subprocesses (before M5)."""

FIRST_PARTY_INDEXES: frozenset[str] = frozenset({DEFAULT_MARKETPLACE})
"""The indexes whose `tier: first-party` is believed. The tier is a claim in the index file,
so an index the user added (`marketplaces`) cannot vouch for itself: its plugins are refused
like community ones."""

INTENT_MEANINGS: dict[IntentType, str] = {
    "note.create": "create a new note in your vault (a note Horae owns)",
    "note.append_section": "add a section to a note Horae owns",
    "note.replace_owned_block": "rewrite the block Horae owns in a note",
    "task.create": "add a task to your task list",
    "task.update_status": "change the status of an existing task",
    "notify.push": "send you a push notification",
    "http.post": "send an HTTP POST request to a configured endpoint",
}
"""One line per intent, shown at approval (a test checks every `INTENT_TYPES` has one)."""

PERSONAL_NOTE = (
    "this plugin marks these fields as personal; they are de-identified before cloud models"
)

_host: TypeAdapter[str] = TypeAdapter(Hostname)
_local: TypeAdapter[str] = TypeAdapter(LocalEndpoint)


def require_installable(name: str, tier: Tier | None, index: str | None) -> None:
    """`TierRefused` unless `tier`, claimed by the index `index`, may be installed now. `None`
    is a direct install (#48). `first-party` counts only from `FIRST_PARTY_INDEXES`."""
    if tier in INSTALLABLE_TIERS and index in FIRST_PARTY_INDEXES:
        return
    if tier is None and DIRECT_INSTALLS:
        return
    if tier is None:
        what = "a direct install (no marketplace tier)"
    elif tier in INSTALLABLE_TIERS:
        what = f"tier 'first-party' claimed by {index}, which is not the Horae marketplace"
    else:
        what = f"tier {tier!r}"
    raise TierRefused(
        f"refusing to install {name}: {what}. Until M5 plugins run as plain subprocesses with "
        "your user's rights, so only first-party marketplace plugins can be installed "
        "(docs/security.md T04); other plugins are planned with the M5 sandbox"
    )


def parse_grants(hosts: Sequence[str], local: Sequence[str]) -> tuple[list[str], list[str]]:
    """Validate `--allow-host` values as `Hostname` and `--allow-local` values as
    `LocalEndpoint` (`host:port`), or raise `InvalidReference` (a usage error)."""
    for value, adapter, flag, shape in (
        *(
            (v, _host, "--allow-host", "a lowercase DNS name such as api.example.org")
            for v in hosts
        ),
        *(
            (v, _local, "--allow-local", "host:port on the local network, e.g. 192.168.1.10:8123")
            for v in local
        ),
    ):
        try:
            adapter.validate_python(value)
        except ValidationError:
            raise InvalidReference(f"{flag} {value!r}: expected {shape}") from None
    return sorted(set(hosts)), sorted(set(local))


def with_grants(asked: Permissions, hosts: Sequence[str], local: Sequence[str]) -> Permissions:
    """`asked` plus the extra hosts and local endpoints (validated, no duplicates)."""
    return Permissions.model_validate(
        {
            **asked.model_dump(mode="json"),
            "network": sorted({*asked.network, *hosts}),
            "network_local": sorted({*asked.network_local, *local}),
        }
    )


def _group(lines: list[str], label: str, values: Sequence[str], added: set[str]) -> None:
    if not values:
        return
    lines.append(f"  {label}:")
    lines.extend(
        f"    - {value}{'  (added by you)' if value in added else ''}" for value in sorted(values)
    )


def describe(
    name: str,
    version: str,
    asked: Permissions,
    granted: Permissions,
    personal_fields: dict[str, list[str]],
) -> list[str]:
    """The approval text: what `name@version` asks for (`granted`, with what the user adds
    beyond `asked` marked), grouped; then its personal fields as information."""
    lines = [f"{name}@{version} asks for these permissions:"]
    _group(
        lines, "network (https hosts)", granted.network, set(granted.network) - set(asked.network)
    )
    _group(
        lines,
        "local network (plain http allowed)",
        granted.network_local,
        set(granted.network_local) - set(asked.network_local),
    )
    _group(lines, "secrets (required)", granted.secrets, set())
    _group(lines, "secrets (optional)", granted.optional_secrets, set())
    if granted.intents:
        lines.append("  intents:")
        lines.extend(
            f"    - {intent}: {INTENT_MEANINGS[intent]}" for intent in sorted(granted.intents)
        )
    _group(lines, "paths (read)", granted.paths.read, set())
    _group(lines, "paths (read and write)", granted.paths.write, set())
    if len(lines) == 1:
        lines.append("  nothing: no network, secret, intent or path")
    if personal_fields:
        lines.append("personal fields (information only, no approval needed):")
        lines.append(f"  {PERSONAL_NOTE}:")
        lines.extend(
            f"    - {ref}: {', '.join(sorted(paths))}"
            for ref, paths in sorted(personal_fields.items())
        )
    return lines


def lock_mismatch(existing: LockedPlugin | None, wanted: LockedPlugin) -> str | None:
    """Why the lock entry `existing` does not already approve `wanted` (for `--yes`), or
    `None` when it does: same repository, version and commit, identical permissions."""
    if existing is None:
        return "horae.lock has no entry for this plugin"
    if existing.repo != wanted.repo:
        return f"horae.lock pins repository {existing.repo}, not {wanted.repo}"
    if existing.version != wanted.version:
        return f"horae.lock pins version {existing.version}, not {wanted.version}"
    if existing.commit != wanted.commit:
        return f"horae.lock pins commit {existing.commit}, not {wanted.commit}"
    have = existing.model_dump(mode="json")["permissions"]
    want = wanted.model_dump(mode="json")["permissions"]
    if have != want:
        fields = sorted(key for key in {*have, *want} if have.get(key, []) != want.get(key, []))
        return (
            "horae.lock approves different permissions "
            f"({', '.join(fields)} differ from what is asked)"
        )
    return None


__all__ = [
    "DIRECT_INSTALLS",
    "INSTALLABLE_TIERS",
    "INTENT_MEANINGS",
    "PERSONAL_NOTE",
    "describe",
    "lock_mismatch",
    "parse_grants",
    "require_installable",
    "with_grants",
]
