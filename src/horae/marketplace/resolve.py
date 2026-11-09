# SPDX-License-Identifier: Apache-2.0
"""Resolve `name[@version]` across the indexes (#43)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from horae.contracts.marketplace import MarketplacePlugin, Tier
from horae.contracts.types import PluginName, SemVer
from horae.marketplace.errors import (
    ConflictError,
    InvalidReference,
    UnknownPlugin,
    UnknownVersion,
)
from horae.marketplace.index import LoadedIndex
from horae.marketplace.semver import highest, sorted_desc

_name: TypeAdapter[str] = TypeAdapter(PluginName)
_version: TypeAdapter[str] = TypeAdapter(SemVer)


@dataclass(frozen=True)
class Resolved:
    """A plugin version, resolved to the repository and commit that pin it."""

    name: str
    version: str
    repo: str
    sha: str
    tier: Tier
    index: str
    """URL of the index that resolved it."""


@dataclass(frozen=True)
class Found:
    """A plugin of an index, for `search`."""

    name: str
    repo: str
    tier: Tier
    versions: tuple[str, ...]
    """Highest first."""
    index: str


def parse_ref(text: str) -> tuple[str, str | None]:
    """`name[@version]` -> `(name, version or None)`; `InvalidReference` if malformed."""
    name, at, version = text.partition("@")
    try:
        _name.validate_python(name)
        if at:
            _version.validate_python(version)
    except ValidationError:
        raise InvalidReference(
            f"{text!r} is not a plugin reference: expected name or name@version "
            "(lowercase name; semantic version such as 0.1.0)"
        ) from None
    return name, (version if at else None)


def _same_repo(left: str, right: str) -> bool:
    def norm(url: str) -> str:
        return re.sub(r"\.git$", "", url.rstrip("/"))

    return norm(left) == norm(right)


def resolve(
    ref: str,
    indexes: Sequence[LoadedIndex],
    *,
    pins: Mapping[str, str] | None = None,
    allow_prerelease: bool = False,
) -> Resolved:
    """Resolve `ref` over `indexes` (highest priority first).

    - A name listed by several indexes under the **same** repository (a mirror) comes from
      the highest-priority one. Under **different** repositories it is a `ConflictError`
      naming every index, unless `pins[name]` names the index to use.
    - Without a version, the highest semantic version; pre-releases only with
      `allow_prerelease`. A version named explicitly may be a pre-release.
    """
    name, wanted = parse_ref(ref)
    pin = (pins or {}).get(name)
    if pin is not None and pin not in {i.url for i in indexes}:
        raise UnknownPlugin(f"horae.yaml pins {name!r} to {pin}, which is not a marketplace")
    listings = [
        (loaded, plugin)
        for loaded in indexes
        for plugin in loaded.index.plugins
        if plugin.name == name and (pin is None or loaded.url == pin)
    ]
    if not listings:
        searched = ", ".join(i.url for i in indexes if pin is None or i.url == pin)
        raise UnknownPlugin(f"no marketplace lists plugin {name!r} (searched: {searched})")
    if len(listings) > 1 and not all(_same_repo(listings[0][1].repo, p.repo) for _, p in listings):
        raise ConflictError(name, [(loaded.url, plugin.repo) for loaded, plugin in listings])
    loaded, plugin = listings[0]
    version = wanted if wanted is not None else _pick(plugin, allow_prerelease)
    sha = plugin.versions.get(version)
    if sha is None:
        raise UnknownVersion(
            f"plugin {name!r} has no version {version} in {loaded.url}; "
            f"available: {_available(plugin)}"
        )
    return Resolved(name, version, plugin.repo, sha, plugin.tier, loaded.url)


def _available(plugin: MarketplacePlugin) -> str:
    return ", ".join(sorted_desc(plugin.versions))


def _pick(plugin: MarketplacePlugin, allow_prerelease: bool) -> str:
    chosen = highest(plugin.versions, allow_prerelease=allow_prerelease)
    if chosen is None:
        raise UnknownVersion(
            f"plugin {plugin.name!r} has only pre-releases ({_available(plugin)}): "
            "name one (name@version) or pass --pre"
        )
    return chosen


def search(indexes: Sequence[LoadedIndex], query: str = "") -> list[Found]:
    """Every plugin whose name contains `query` (case-insensitive), by index priority, then
    name. A name listed by several indexes appears once per index."""
    needle = query.lower()
    return [
        Found(
            plugin.name,
            plugin.repo,
            plugin.tier,
            tuple(sorted_desc(plugin.versions)),
            loaded.url,
        )
        for loaded in indexes
        for plugin in sorted(loaded.index.plugins, key=lambda p: p.name)
        if needle in plugin.name
    ]
