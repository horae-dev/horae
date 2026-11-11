# SPDX-License-Identifier: Apache-2.0
"""A marketplace index, `marketplace.json` (PLAN.md §4, #43).

The index is the published format of `horae-dev/marketplace` (its
`schema/marketplace.schema.json`): a list of plugins, each with its git repository, the released
versions pinned to a full commit SHA, and a tier. The name, version, repository and commit
patterns are the lockfile's (`horae.contracts.lockfile`), the commit included: a 40-hex SHA-1
or a 64-hex SHA-256 (#153).

An index is untrusted data: the core validates every index it fetches with this contract
(strict, unknown keys rejected) before using any of it.
"""

from typing import Annotated, Self

from pydantic import Field, model_validator

from horae.contracts.lockfile import GitRepoUrl, Tier
from horae.contracts.types import CommitSha, Contract, PluginName, SemVer

MAX_INDEX_PLUGINS = 1024
MAX_PLUGIN_VERSIONS = 512

IndexCommit = CommitSha
"""The full commit id a released version is pinned to: 40 hex (SHA-1) or 64 hex (SHA-256)."""


class MarketplacePlugin(Contract):
    """One plugin of an index."""

    name: PluginName
    """The name used in `horae plugin add <name>@<version>`."""
    repo: GitRepoUrl
    """https git URL of the plugin repository."""
    versions: Annotated[
        dict[SemVer, IndexCommit],
        Field(min_length=1, max_length=MAX_PLUGIN_VERSIONS),
    ]
    """Released versions: semver -> the commit the tag `v<version>` points to."""
    tier: Tier


class MarketplaceIndex(Contract):
    """A `marketplace.json` file."""

    plugins: Annotated[list[MarketplacePlugin], Field(max_length=MAX_INDEX_PLUGINS)]
    """One entry per plugin name (names are unique)."""

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        seen: set[str] = set()
        for plugin in self.plugins:
            if plugin.name in seen:
                raise ValueError(f"duplicate plugin name {plugin.name!r}")
            seen.add(plugin.name)
        return self
