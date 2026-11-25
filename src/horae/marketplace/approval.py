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
- `parse_grants(...)`: the `--allow-host`, `--allow-local`, `--allow-path` and
  `--allow-read-path` values, validated, as a `UserGrants`.
- `with_grants(asked, grants)`: what the manifest asks for, plus the hosts, local-network
  endpoints and paths the user adds at approval. A lock may grant more than a manifest
  declares (the plugin host refuses only less, decision 66); this is how a user approves their
  own school or Home Assistant host, or the vault of a sink whose manifest asks for no path
  (#205).
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
from horae.contracts.types import AbsolutePath, Hostname, LocalEndpoint
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
_path: TypeAdapter[str] = TypeAdapter(AbsolutePath)


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


_PATH_SHAPE = "a normalised absolute path such as /srv/vault (no `.`, `..`, `//` or trailing /)"


def parse_grants(
    hosts: Sequence[str],
    local: Sequence[str],
    paths: Sequence[str] = (),
    read_paths: Sequence[str] = (),
) -> UserGrants:
    """Validate `--allow-host` values as `Hostname`, `--allow-local` values as
    `LocalEndpoint` (`host:port`) and `--allow-path` (read and write) / `--allow-read-path`
    (read only) values as `AbsolutePath`, or raise `InvalidReference` (a usage error). A path
    given with both flags is granted read and write."""
    for value, adapter, flag, shape in (
        *(
            (v, _host, "--allow-host", "a lowercase DNS name such as api.example.org")
            for v in hosts
        ),
        *(
            (v, _local, "--allow-local", "host:port on the local network, e.g. 192.168.1.10:8123")
            for v in local
        ),
        *((v, _path, "--allow-path", _PATH_SHAPE) for v in paths),
        *((v, _path, "--allow-read-path", _PATH_SHAPE) for v in read_paths),
    ):
        try:
            adapter.validate_python(value)
        except ValidationError:
            raise InvalidReference(f"{flag} {value!r}: expected {shape}") from None
    return UserGrants.model_validate(
        {
            "network": sorted(set(hosts)),
            "network_local": sorted(set(local)),
            "paths": {
                "read": sorted(set(read_paths) - set(paths)),
                "write": sorted(set(paths)),
            },
        }
    )


def with_grants(asked: Permissions, grants: UserGrants) -> Permissions:
    """`asked` plus the extra hosts, local endpoints and paths (validated, no duplicates)."""
    return Permissions.model_validate(
        {
            **asked.model_dump(mode="json"),
            "network": sorted({*asked.network, *grants.network}),
            "network_local": sorted({*asked.network_local, *grants.network_local}),
            "paths": {
                "read": sorted({*asked.paths.read, *grants.paths.read}),
                "write": sorted({*asked.paths.write, *grants.paths.write}),
            },
        }
    )


def merge_grants(first: UserGrants, second: UserGrants) -> UserGrants:
    """Both sets of grants (an upgrade: what the lock records plus the new flags)."""
    return UserGrants.model_validate(
        {
            "network": sorted({*first.network, *second.network}),
            "network_local": sorted({*first.network_local, *second.network_local}),
            "paths": {
                "read": sorted({*first.paths.read, *second.paths.read}),
                "write": sorted({*first.paths.write, *second.paths.write}),
            },
        }
    )


def recorded_grants(grants: UserGrants) -> UserGrants | None:
    """The grants as the lock entry records them (`None` when there are none, so the field is
    left out of the lock)."""
    return grants if grants else None


def dropped_grants(existing: LockedPlugin, granted: Permissions) -> list[tuple[str, str]]:
    """The hosts, local endpoints and paths `existing` grants that `granted` does not, each with
    the flag that grants it again (a path dropped from `write` but kept in `read` counts)."""
    have, want = existing.permissions, granted
    return sorted(
        [
            *((h, "--allow-host") for h in set(have.network) - set(want.network)),
            *((e, "--allow-local") for e in set(have.network_local) - set(want.network_local)),
            *((p, "--allow-path") for p in set(have.paths.write) - set(want.paths.write)),
            *(
                (p, "--allow-read-path")
                for p in set(have.paths.read) - set(want.paths.read) - set(want.paths.write)
            ),
        ]
    )


def describe_dropped(dropped: Sequence[tuple[str, str]]) -> str:
    """The flags that would grant `dropped` again, for a note (values are paths, hosts and
    endpoints already validated by the lock, never secrets)."""
    return " ".join(f"{flag} {value}" for value, flag in dropped)


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
    _group(
        lines, "paths (read)", granted.paths.read, set(granted.paths.read) - set(asked.paths.read)
    )
    _group(
        lines,
        "paths (read and write)",
        granted.paths.write,
        set(granted.paths.write) - set(asked.paths.write),
    )
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
    "describe_dropped",
    "describe_origin",
    "dropped_grants",
    "first_party",
    "lock_mismatch",
    "merge_grants",
    "origin",
    "parse_grants",
    "recorded_grants",
    "require_installable",
    "what_it_is",
    "with_grants",
]
