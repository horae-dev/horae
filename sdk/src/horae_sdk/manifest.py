# SPDX-License-Identifier: Apache-2.0
"""`plugin.yaml` helpers (`schemas/plugin-manifest/v1.json`, `horae.contracts.PluginManifest`).

`manifest(...)` builds the document as a `dict` and checks the rules that are easy to get
wrong (`ManifestError`, a `ValueError`): names and refs, the version, the sink operation names
a plugin may not provide (`<ns>.read`, `<ns>.apply`, `<ns>.restore`, `<ns>.parent_exists`,
`<ns>.identity` of its own sink and notifier namespaces), the permissions. The core's loader
is the judge; the SDK's tests validate what this module writes against the generated JSON
Schema and the pydantic model.

`Plugin.manifest()` fills `provides` from the plugin's registered tools and sinks.

The runtime is `mcp-stdio` with an argument list (no shell). The host resolves `argv[0]`: an
absolute path is kept, a bare name is looked up on the child's fixed `PATH`
(`/usr/local/bin:/usr/bin:/bin`), and the child's working directory is an empty directory.
Until M4 installs plugins into their own environments, pin the command to an absolute
interpreter that has `horae-sdk` installed and an absolute script path (or `-m <module>`).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from horae_sdk.intents import INTENT_TYPES

SINK_OPERATIONS = ("read", "apply", "restore", "parent_exists", "identity")
"""The operations the core calls on a sink or notifier namespace (`horae.contracts.plugin`)."""
TOOL_KINDS = ("sources", "transforms", "sinks", "notifiers")
DEFAULT_HORAE = ">=0"
"""Compatible core versions when none is given (the core is not released yet: `0.0.0`)."""

_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_REF = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*")
_SKILL = re.compile(r"[a-z][a-z0-9-]*/[a-z][a-z0-9_-]*")
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
)
_SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
_ABSOLUTE = re.compile(r"(/[^/\\\x00-\x1f\x7f]{1,255})+")


def _is_str(value: object) -> bool:
    """Checked at run time too: a plugin's own code may not be type-checked."""
    return isinstance(value, str)


class ManifestError(ValueError):
    """The manifest would not be valid (the message says why)."""


@dataclass(frozen=True)
class Permissions:
    """What the plugin may touch (`permissions` in `plugin.yaml`). Empty means no access.

    `intents` are the intents the plugin may propose or, for a sink, apply."""

    intents: Sequence[str] = ()
    network: Sequence[str] = ()
    secrets: Sequence[str] = ()
    read: Sequence[str] = ()
    write: Sequence[str] = ()

    def check(self) -> None:
        """`ManifestError` when a value would not validate."""
        rules = (
            ("intents", self.intents, lambda v: v in INTENT_TYPES),
            ("network", self.network, lambda v: bool(_HOST.fullmatch(v)) and len(v) <= 253),
            ("secrets", self.secrets, lambda v: bool(_SECRET.fullmatch(v))),
            ("paths.read", self.read, _absolute),
            ("paths.write", self.write, _absolute),
        )
        for what, values, valid in rules:
            if isinstance(values, str):
                raise ManifestError(f"permissions.{what} must be a list, not a string")
            for value in values:
                if not (_is_str(value) and valid(value)):
                    raise ManifestError(f"permissions.{what}: {value!r} is not valid")
            if len(set(values)) != len(values):
                raise ManifestError(f"permissions.{what} lists a value twice")
        if any(name.startswith("HORAE_") for name in self.secrets):
            raise ManifestError("permissions.secrets: HORAE_* names are reserved")

    def to_dict(self) -> dict[str, Any]:
        """The `permissions` mapping, empty lists left out (they are the default)."""
        data: dict[str, Any] = {}
        for key in ("network", "secrets"):
            if getattr(self, key):
                data[key] = list(getattr(self, key))
        paths = {key: list(getattr(self, key)) for key in ("read", "write") if getattr(self, key)}
        if paths:
            data["paths"] = paths
        if self.intents:
            data["intents"] = list(self.intents)
        return data


def _absolute(path: str) -> bool:
    segments = path.split("/")[1:]
    return bool(_ABSOLUTE.fullmatch(path)) and not any(s in (".", "..") for s in segments)


def _refs(kind: str, refs: Iterable[str], pattern: re.Pattern[str]) -> list[str]:
    listed = list(refs)
    for ref in listed:
        if not (_is_str(ref) and len(ref) <= 128 and pattern.fullmatch(ref)):
            raise ManifestError(f"provides.{kind}: {ref!r} is not a valid ref")
    if len(set(listed)) != len(listed):
        raise ManifestError(f"provides.{kind} lists a ref twice")
    return listed


def manifest(
    *,
    name: str,
    version: str,
    provides: Mapping[str, Iterable[str]],
    permissions: Permissions | None = None,
    command: Sequence[str] | None = None,
    horae: str = DEFAULT_HORAE,
    description: str | None = None,
    license: str | None = None,
    homepage: str | None = None,
) -> dict[str, Any]:
    """A `plugin.yaml` document (see the module docstring). `command` is the `mcp-stdio`
    argument list; without one the manifest has no runtime (a skill or pipeline pack)."""
    if not (_is_str(name) and _NAME.fullmatch(name)):
        raise ManifestError(f"name {name!r} must match ^[a-z][a-z0-9-]*$ (at most 64)")
    if not (_is_str(version) and _SEMVER.fullmatch(version)):
        raise ManifestError(f"version {version!r} is not a semantic version")
    unknown = sorted(set(provides) - {*TOOL_KINDS, "skills", "pipelines"})
    if unknown:
        raise ManifestError(f"provides: unknown kind {', '.join(unknown)}")
    listed: dict[str, list[str]] = {}
    for kind in TOOL_KINDS:
        listed[kind] = _refs(kind, provides.get(kind, ()), _REF)
    listed["skills"] = _refs("skills", provides.get("skills", ()), _SKILL)
    listed["pipelines"] = _refs("pipelines", provides.get("pipelines", ()), _NAME)
    namespaces = {ref.split(".", 1)[0] for kind in ("sinks", "notifiers") for ref in listed[kind]}
    reserved = {f"{ns}.{op}" for ns in namespaces for op in SINK_OPERATIONS}
    clash = sorted(reserved & {ref for kind in TOOL_KINDS for ref in listed[kind]})
    if clash:
        raise ManifestError(f"{', '.join(clash)}: a sink operation cannot be provided")
    permissions = permissions or Permissions()
    permissions.check()
    data: dict[str, Any] = {"name": name, "version": version, "horae": horae}
    for key, value in (("description", description), ("license", license)):
        if value is not None:
            data[key] = value
    if homepage is not None:
        if not homepage.startswith("https://"):
            raise ManifestError("homepage must be an https:// URL")
        data["homepage"] = homepage
    data["provides"] = {kind: refs for kind, refs in listed.items() if refs}
    data["permissions"] = permissions.to_dict()
    if command is not None:
        argv = list(command)
        if not argv or not all(_is_str(arg) and arg and "\x00" not in arg for arg in argv):
            raise ManifestError("runtime.command must be a non-empty list of arguments")
        data["runtime"] = {"protocol": "mcp-stdio", "command": argv}
    return data


def dump_manifest(data: Mapping[str, Any]) -> str:
    """The manifest as YAML, keys in the order given."""
    return yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)


def write_manifest(path: Path, data: Mapping[str, Any]) -> None:
    """Write `dump_manifest(data)` to `path` (`plugin.yaml`)."""
    path.write_text(dump_manifest(data), encoding="utf-8")


__all__ = [
    "DEFAULT_HORAE",
    "SINK_OPERATIONS",
    "TOOL_KINDS",
    "ManifestError",
    "Permissions",
    "dump_manifest",
    "manifest",
    "write_manifest",
]
