# SPDX-License-Identifier: Apache-2.0
"""The instance lockfile, `horae.lock` (PLAN.md §4).

`horae plugin add` writes one entry per plugin: name, version, the pinned commit and the
permissions the user approved. `upgrade` compares new permissions against these.

Serializing a `Lockfile` is canonical: plugins are sorted by name and every permission list
is sorted, so that a diff of two lockfiles shows only real changes. Reading accepts any order.
"""

import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    Strict,
    StringConstraints,
    field_serializer,
    model_serializer,
    model_validator,
)

from horae.contracts.plugin import Permissions, PersonalFields
from horae.contracts.types import (
    HOST,
    MAX_ITEMS,
    CommitSha,
    Contract,
    Hostname,
    JsonSchemaExtra,
    LocalEndpoint,
    PipelineName,
    PluginName,
    SchemaVersion1,
    SemVer,
    Sha256Hex,
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


class UserGrants(Contract):
    """The hosts and local endpoints the user added with `--allow-host` / `--allow-local`
    (decision 73 D), as opposed to the ones the manifest declares."""

    network: Annotated[list[Hostname], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    """Hosts added with `--allow-host` (sorted when written)."""
    network_local: Annotated[list[LocalEndpoint], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=list
    )
    """Endpoints added with `--allow-local` (sorted when written)."""

    def __bool__(self) -> bool:
        return bool(self.network or self.network_local)


class LockedPlugin(Contract):
    """One installed plugin, pinned to a commit."""

    version: SemVer
    """Installed plugin version."""
    repo: GitRepoUrl
    """Git URL the plugin was cloned from."""
    commit: CommitSha
    """The commit checked out. For a marketplace install the tag for `version` points to it;
    for a direct install (`source: direct`) it is the pinned SHA, no tag is checked."""
    permissions: Permissions
    """The permissions the user approved at install or upgrade time (sorted when written).
    `network_local` (decision 66) is left out when empty, so a lock without a local grant
    is written exactly as before and an older core still reads it."""

    personal_fields: PersonalFields = Field(default_factory=dict)
    """The personal fields of the plugin's sources and transforms (`provides.personal_fields`
    of its manifest, decision 68), recorded with the approved plugin: the core enforces the
    union of these and the installed manifest's, so an upgrade can never drop one silently.
    Left out when empty (paths sorted when written), so a lock without any is written exactly
    as before and an older core still reads it."""

    pipelines: Annotated[dict[PipelineName, Sha256Hex], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=dict
    )
    """For a pipeline pack (decision 72): the pipelines `horae plugin add` installed from it, by
    name, each with the SHA-256 of the file as approved. The approval prompt showed them; the
    loader (`horae.config.loader`) runs a pack's pipeline only if its file still has this
    digest, and `--yes` refuses a pack whose pipelines differ. Left out when empty (names
    sorted when written), so a lock without any is written exactly as before."""

    source: Literal["direct"] | None = None
    """Where the plugin came from (decision 73 A, settling decision 71 E): `direct` for a direct
    install from a git URL (#48, no marketplace tier: `commit` is the pinned SHA and no tag was
    checked); absent for a marketplace install, so a lock written before it reads the same.
    `horae plugin upgrade` reads it to apply the direct-install gate, and `--yes` compares it."""

    user_grants: UserGrants | None = None
    """What the user added with `--allow-host` / `--allow-local` when `add` or `upgrade`
    approved this entry (decision 73 D, #175). `horae plugin upgrade` carries over only these
    (never what it infers from the installed manifest, which is not verified). Always a subset
    of `permissions` (the loader refuses a lock that lists a grant it does not give). Left out
    when empty, so a lock without any, or written before it, is written exactly as before."""

    @model_validator(mode="after")
    def _grants_are_granted(self) -> Self:
        if self.user_grants is not None:
            extra = (set(self.user_grants.network) - set(self.permissions.network)) | (
                set(self.user_grants.network_local) - set(self.permissions.network_local)
            )
            if extra:
                raise ValueError(
                    f"user_grants lists {sorted(extra)}, which permissions do not grant"
                )
        return self

    @field_serializer("user_grants", mode="wrap")
    def _canonical_user_grants(
        self, value: UserGrants | None, handler: SerializerFunctionWrapHandler
    ) -> Any:
        data = handler(value)
        if isinstance(data, dict):
            data = {key: sorted(items) for key, items in data.items() if items}
        return data

    @field_serializer("permissions", mode="wrap")
    def _canonical_permissions(
        self, value: Permissions, handler: SerializerFunctionWrapHandler
    ) -> Any:
        data = _sorted_lists(handler(value))
        if isinstance(data, dict) and not data.get("network_local"):
            data.pop("network_local", None)
        return data

    @field_serializer("personal_fields", mode="wrap")
    def _canonical_personal_fields(
        self, value: dict[str, list[str]], handler: SerializerFunctionWrapHandler
    ) -> Any:
        data = handler(value)
        return {ref: sorted(paths) for ref, paths in sorted(data.items())}

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in ("personal_fields", "pipelines", "source", "user_grants"):
                if not data.get(key):
                    data.pop(key, None)
        return data

    @field_serializer("pipelines", mode="wrap")
    def _canonical_pipelines(
        self, value: dict[str, str], handler: SerializerFunctionWrapHandler
    ) -> Any:
        return dict(sorted(handler(value).items()))


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
