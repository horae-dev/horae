# SPDX-License-Identifier: Apache-2.0
"""Permission approval at install (#46, schemas decision 70).

- `require_installable(name, tier, index, runtime=...)`: a `first-party` plugin of the default
  marketplace installs under either runtime; any other plugin (another tier, `first-party`
  claimed by an index the user added, or a direct install from a git URL, #48, which has no
  tier and passes `None`) installs only when its effective runtime in `horae.yaml` is
  `container` (#191, schemas decision 79, docs/security.md T04). One check for every source.
- `origin(tier, index)`: what the `horae.lock` entry records of where a plugin came from
  (`tier` and `index`, only for a plugin the default marketplace does not vouch for).
- `describe_origin(...)`: the approval lines naming the source, the tier and the runtime.
- `with_grants(asked, hosts, local)`: what the manifest asks for, plus the hosts and
  local-network endpoints the user adds at approval (`--allow-host`, `--allow-local`). A lock
  may grant more than a manifest declares (the plugin host refuses only less, decision 66);
  this is how a user approves their own school or Home Assistant host.
- `describe(...)`: the approval text, grouped and labelled plainly.
- `lock_mismatch(existing, wanted)`: why `--yes` cannot approve `wanted` from the lock entry
  already there, or `None` when the entry is for the same source, repository, version and
  commit with identical permissions and pipelines.
"""

from collections.abc import Sequence

from pydantic import TypeAdapter, ValidationError

from horae.contracts.instance import DEFAULT_MARKETPLACE, PluginRuntime
from horae.contracts.intents import IntentType
from horae.contracts.lockfile import LockedPlugin, Tier, UserGrants
from horae.contracts.plugin import FREE_TEXT_FIELD, Permissions
from horae.contracts.types import Hostname, LocalEndpoint
from horae.marketplace.errors import InvalidReference, TierRefused

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
SCAN_ONLY_NOTE = (
    "these free-text fields (`free_text:`) are scanned for the other personal values before "
    "cloud models, but are not de-identified as values themselves"
)

_host: TypeAdapter[str] = TypeAdapter(Hostname)
_local: TypeAdapter[str] = TypeAdapter(LocalEndpoint)


def first_party(tier: Tier | None, index: str | None) -> bool:
    """Whether `tier`, claimed by the index `index`, is first-party: `first-party` counts only
    from `FIRST_PARTY_INDEXES`. `None` is a direct install (#48), never first-party."""
    return tier == "first-party" and index in FIRST_PARTY_INDEXES


def origin(tier: Tier | None, index: str | None) -> tuple[Tier | None, str | None]:
    """The `(tier, index)` a marketplace install records in `horae.lock`: both `None` for a
    first-party plugin (so its entry is written exactly as before #191), else as resolved."""
    if tier is None or index is None or first_party(tier, index):
        return None, None
    return tier, index


def what_it_is(tier: Tier | None, index: str | None) -> str:
    """A plugin's origin in words, for a refusal or the approval prompt."""
    if tier is None:
        return "a direct install from a git URL (no marketplace tier)"
    if tier == "first-party" and index not in FIRST_PARTY_INDEXES:
        return f"tier 'first-party' claimed by {index}, which is not the Horae marketplace"
    if first_party(tier, index):
        return "a first-party plugin of the Horae marketplace"
    return f"tier {tier!r} from {index}"


def container_only(name: str) -> str:
    """How to let a plugin that is not first-party install and run (one wording everywhere)."""
    return (
        f"it may only run in a container: set `plugins.{name}.runtime: container` (or "
        "`plugin_defaults.runtime: container`) in horae.yaml and make sure Docker is available "
        "(docs/operations/plugin-containers.md)"
    )


def require_installable(
    name: str, tier: Tier | None, index: str | None, *, runtime: PluginRuntime
) -> None:
    """`TierRefused` unless `tier`, claimed by the index `index`, may be installed with the
    effective `runtime` of the plugin in `horae.yaml` (#191). `None` is a direct install
    (#48). A first-party plugin installs under either runtime; anything else needs
    `container` (the process runtime has no isolation, docs/security.md T04)."""
    if first_party(tier, index) or runtime == "container":
        return
    raise TierRefused(
        f"refusing to install {name}: {what_it_is(tier, index)} is not first-party, and "
        f"horae.yaml runs it with `runtime: {runtime}`, which has no isolation "
        f"(docs/security.md T04); {container_only(name)}"
    )


def describe_origin(
    tier: Tier | None, index: str | None, repo: str, *, runtime: PluginRuntime
) -> list[str]:
    """The approval lines saying where a plugin comes from and how it will run."""
    lines = [
        f"source: {'direct install (pinned by its commit SHA)' if tier is None else 'marketplace'}"
        f" {repo}"
    ]
    if tier is not None:
        lines.append(f"tier: {tier} (index {index})")
    if first_party(tier, index):
        lines.append(f"runtime: {runtime} (first-party: either runtime is allowed)")
    else:
        lines.append(
            "runs in a container (runtime: container): not first-party, so horae.yaml must keep "
            "`runtime: container` for it; it is refused under the process runtime"
        )
        lines.append(
            "installed without running its build: only its dependencies are installed, from "
            "hashed wheels; its own project is never built (no setup.py, build hook or backend "
            "runs), its sources are imported from its tree"
        )
    return lines


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


def recorded_grants(hosts: Sequence[str], local: Sequence[str]) -> UserGrants | None:
    """The `--allow-host` / `--allow-local` values as the lock entry records them (`None` when
    there are none, so the field is left out of the lock)."""
    grants = UserGrants(network=sorted(set(hosts)), network_local=sorted(set(local)))
    return grants or None


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
    pipelines: Sequence[str] = (),
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
    if pipelines:
        lines.append("pipelines it installs (they run once the plugin is enabled in horae.yaml):")
        lines.extend(f"  {line}" for line in pipelines)
    if personal_fields:
        # #165: scan-only paths are listed apart, so a free-text field is never presented as
        # de-identified.
        personal = {
            ref: sorted(path for path in paths if not path.startswith(FREE_TEXT_FIELD))
            for ref, paths in sorted(personal_fields.items())
        }
        scanned = {
            ref: sorted(
                path.removeprefix(FREE_TEXT_FIELD)
                for path in paths
                if path.startswith(FREE_TEXT_FIELD)
            )
            for ref, paths in sorted(personal_fields.items())
        }
        lines.append("personal fields (information only, no approval needed):")
        for note, groups in ((PERSONAL_NOTE, personal), (SCAN_ONLY_NOTE, scanned)):
            if any(groups.values()):
                lines.append(f"  {note}:")
                lines.extend(
                    f"    - {ref}: {', '.join(paths)}" for ref, paths in groups.items() if paths
                )
    return lines


def lock_mismatch(existing: LockedPlugin | None, wanted: LockedPlugin) -> str | None:
    """Why the lock entry `existing` does not already approve `wanted` (for `--yes`), or
    `None` when it does: same repository, version and commit, identical permissions."""
    if existing is None:
        return "horae.lock has no entry for this plugin"
    if existing.repo != wanted.repo:
        return f"horae.lock pins repository {existing.repo}, not {wanted.repo}"
    if existing.source != wanted.source:
        have_source, want_source = existing.source or "marketplace", wanted.source or "marketplace"
        return f"horae.lock records a {have_source} install, not a {want_source} one"
    if (existing.tier, existing.index) != (wanted.tier, wanted.index):
        return "horae.lock records another tier or index for this plugin"
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
    if existing.pipelines != wanted.pipelines:
        changed = sorted(
            name
            for name in {*existing.pipelines, *wanted.pipelines}
            if existing.pipelines.get(name) != wanted.pipelines.get(name)
        )
        return f"horae.lock approves different pipelines ({', '.join(changed)} differ)"
    return None


__all__ = [
    "FIRST_PARTY_INDEXES",
    "INTENT_MEANINGS",
    "PERSONAL_NOTE",
    "container_only",
    "describe",
    "describe_origin",
    "first_party",
    "lock_mismatch",
    "origin",
    "parse_grants",
    "recorded_grants",
    "require_installable",
    "what_it_is",
    "with_grants",
]
