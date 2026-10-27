# SPDX-License-Identifier: Apache-2.0
"""The instance lockfile, `horae.lock` (PLAN.md §4).

`horae plugin add` writes one entry per plugin: name, version, the pinned commit and the
permissions the user approved. `upgrade` compares new permissions against these.
"""

from typing import Literal

from pydantic import Field

from horae.contracts._base import CommitSha, Contract, NonEmptyStr, PluginName, SemVer
from horae.contracts.plugin import Permissions


class LockedPlugin(Contract):
    """One installed plugin, pinned to a commit."""

    version: SemVer
    """Installed plugin version."""
    repo: NonEmptyStr
    """Git URL the plugin was cloned from."""
    commit: CommitSha
    """The commit checked out; the tag for `version` must point to it."""
    permissions: Permissions
    """The permissions the user approved at install or upgrade time."""


class Lockfile(Contract):
    """The `horae.lock` file of an instance."""

    schema_version: Literal[1]
    """Version of this lockfile format."""
    plugins: dict[PluginName, LockedPlugin] = Field(default_factory=dict)
    """Installed plugins, by name."""
