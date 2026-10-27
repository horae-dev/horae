# SPDX-License-Identifier: Apache-2.0
"""The instance lockfile, `horae.lock` (PLAN.md §4).

`horae plugin add` writes one entry per plugin: name, version, the pinned commit and the
permissions the user approved. `upgrade` compares new permissions against these.

Serializing a `Lockfile` is canonical: plugins are sorted by name and every permission list
is sorted, so that a diff of two lockfiles shows only real changes. Reading accepts any order.
"""

import re
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    Strict,
    StringConstraints,
    field_serializer,
)

from horae.contracts.plugin import Permissions
from horae.contracts.types import (
    HOST,
    MAX_ITEMS,
    CommitSha,
    Contract,
    JsonSchemaExtra,
    PluginName,
    SchemaVersion1,
    SemVer,
)

_REPO_DOT_SEGMENT = r"/\.\.?(/|$)"


def _check_repo(url: str) -> str:
    if re.search(_REPO_DOT_SEGMENT, url) or re.match(r"^https://([^/]*\.)?localhost/", url):
        raise ValueError(f"{url!r} has a `.` or `..` segment or a localhost host")
    return url


GitRepoUrl = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^https://{HOST}(/[A-Za-z0-9._-]+)+$", max_length=512),
    AfterValidator(_check_repo),
    JsonSchemaExtra(
        {
            "not": {
                "anyOf": [
                    {"pattern": _REPO_DOT_SEGMENT},
                    {"pattern": r"^https://([^/]*\.)?localhost/"},
                ]
            }
        }
    ),
]
"""An `https://` git URL, e.g. `https://github.com/horae-dev/plugin-github` (`.git` optional).
Host name only (no port, user info or IP literal); path segments of `A-Z a-z 0-9 . _ -`,
none of them `.` or `..`; no query or fragment."""


def _sorted_lists(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted_lists(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted(value)
    return value


class LockedPlugin(Contract):
    """One installed plugin, pinned to a commit."""

    version: SemVer
    """Installed plugin version."""
    repo: GitRepoUrl
    """Git URL the plugin was cloned from."""
    commit: CommitSha
    """The commit checked out; the tag for `version` must point to it."""
    permissions: Permissions
    """The permissions the user approved at install or upgrade time (sorted when written)."""

    @field_serializer("permissions", mode="wrap")
    def _canonical_permissions(
        self, value: Permissions, handler: SerializerFunctionWrapHandler
    ) -> Any:
        return _sorted_lists(handler(value))


class Lockfile(Contract):
    """The `horae.lock` file of an instance."""

    schema_version: SchemaVersion1
    """Version of this lockfile format."""
    plugins: Annotated[dict[PluginName, LockedPlugin], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """Installed plugins, by name (sorted when written)."""

    @field_serializer("plugins", mode="wrap")
    def _sorted_plugins(
        self, value: dict[str, LockedPlugin], handler: SerializerFunctionWrapHandler
    ) -> Any:
        return dict(sorted(handler(value).items()))
