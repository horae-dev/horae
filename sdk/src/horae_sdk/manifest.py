# SPDX-License-Identifier: Apache-2.0
"""`plugin.yaml` helpers (`schemas/plugin-manifest/v1.json`, `horae.contracts.PluginManifest`).

`manifest(...)` builds the document as a `dict` and checks the rules that are easy to get
wrong (`ManifestError`, a `ValueError`): names and refs, the version, the sink operation names
a plugin may not provide (`<ns>.read`, `<ns>.apply`, `<ns>.restore`, `<ns>.restore_status`,
`<ns>.parent_exists`, `<ns>.identity` of its own sink and notifier namespaces), the
permissions. The core's loader is the judge; the SDK's tests validate what this module writes
against the generated JSON Schema and the pydantic model.

`Plugin.manifest()` fills `provides` from the plugin's registered tools and sinks, and
`provides.personal_fields` from the `personal_fields` given when registering a source or a
transform (horae-sdk 0.7, schemas decision 68): the view paths of the tool's output that hold
personal data (`children[].first_name`), which the core de-identifies before any cloud model
whatever the skill declares. A free-text field that may mention personal data (a mail body)
is declared scan-only, `free_text("messages[].body")` (horae-sdk 0.9, #165).

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
from horae_sdk.protocol import local_endpoint

SINK_OPERATIONS = ("read", "apply", "restore", "restore_status", "parent_exists", "identity")
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
_VIEW_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_VIEW_PATH = re.compile(rf"({_VIEW_NAME}|\[\])(\.{_VIEW_NAME}|\[\])*")
MAX_PERSONAL_FIELDS = 64
"""Most personal fields one tool declares (the core's `MAX_PERSONAL_FIELDS`)."""
FREE_TEXT_FIELD = "free_text:"
"""The prefix of a scan-only personal field (horae-sdk 0.9, the core's `FREE_TEXT_FIELD`):
`free_text:messages[].body` is free text that may mention personal data. The core scans it
for the other personal values instead of treating its whole content as one."""


def free_text(path: str) -> str:
    """`path` declared scan-only: `free_text("messages[].body")` is
    `free_text:messages[].body`."""
    return FREE_TEXT_FIELD + path


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
    network_local: Sequence[str] = ()
    """Local-network endpoints, `host:port` (`127.0.0.1:1143`, `[::1]:1143`,
    `homeassistant.local:8123`; decision 66). Plain `http` is allowed on them only."""
    secrets: Sequence[str] = ()
    optional_secrets: Sequence[str] = ()
    """Injected when they resolve; the plugin still starts without them."""
    read: Sequence[str] = ()
    write: Sequence[str] = ()

    def check(self) -> None:
        """`ManifestError` when a value would not validate."""
        rules = (
            ("intents", self.intents, lambda v: v in INTENT_TYPES),
            ("network", self.network, lambda v: bool(_HOST.fullmatch(v)) and len(v) <= 253),
            ("network_local", self.network_local, lambda v: local_endpoint(v) is not None),
            ("secrets", self.secrets, lambda v: bool(_SECRET.fullmatch(v))),
            (
                "optional_secrets",
                self.optional_secrets,
                lambda v: bool(_SECRET.fullmatch(v)),
            ),
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
        for what, names in (("secrets", self.secrets), ("optional_secrets", self.optional_secrets)):
            if any(name.startswith("HORAE_") for name in names):
                raise ManifestError(f"permissions.{what}: HORAE_* names are reserved")
        if set(self.secrets) & set(self.optional_secrets):
            raise ManifestError("permissions: a secret is in both secrets and optional_secrets")

    def to_dict(self) -> dict[str, Any]:
        """The `permissions` mapping, empty lists left out (they are the default)."""
        data: dict[str, Any] = {}
        for key in ("network", "network_local", "secrets", "optional_secrets"):
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


def _steps(path: str) -> tuple[str, ...]:
    steps: list[str] = []
    for part in path.split("."):
        name, _, rest = part.partition("[]")
        if name:
            steps.append(name)
        if part.endswith("[]"):
            steps.extend("[]" for _ in range(1 + rest.count("[]")))
    return tuple(steps)


def _personal_fields(
    declared: Mapping[str, Sequence[str]], tools: set[str]
) -> dict[str, list[str]]:
    """`provides.personal_fields`, checked: each key a source or transform of the plugin,
    each value 1 to `MAX_PERSONAL_FIELDS` distinct view paths (a scan-only one prefixed by
    `free_text:`), none under another and none disagreeing with another on a value's shape,
    prefix or not."""
    checked: dict[str, list[str]] = {}
    for ref, paths in declared.items():
        if ref not in tools:
            raise ManifestError(f"provides.personal_fields: {ref!r} is not a source or transform")
        if isinstance(paths, str):
            raise ManifestError(f"provides.personal_fields.{ref} must be a list, not a string")
        listed = list(paths)
        if not 1 <= len(listed) <= MAX_PERSONAL_FIELDS:
            raise ManifestError(f"provides.personal_fields.{ref}: 1 to {MAX_PERSONAL_FIELDS} paths")
        for path in listed:
            bare = path.removeprefix(FREE_TEXT_FIELD) if _is_str(path) else path
            if not (_is_str(bare) and len(bare) <= 256 and _VIEW_PATH.fullmatch(bare)):
                raise ManifestError(f"provides.personal_fields.{ref}: {path!r} is not a path")
        if len(set(listed)) != len(listed):
            raise ManifestError(f"provides.personal_fields.{ref} lists a path twice")
        steps = [_steps(path.removeprefix(FREE_TEXT_FIELD)) for path in listed]
        for one in steps:
            for other in steps:
                common = 0
                while common < min(len(one), len(other)) and one[common] == other[common]:
                    common += 1
                if one != other and common == len(one):
                    raise ManifestError(f"provides.personal_fields.{ref}: overlapping paths")
                if (
                    common < len(one)
                    and common < len(other)
                    and (one[common] == "[]") != (other[common] == "[]")
                ):
                    raise ManifestError(
                        f"provides.personal_fields.{ref}: two paths disagree on a value's shape"
                    )
        checked[ref] = listed
    return checked


def manifest(
    *,
    name: str,
    version: str,
    provides: Mapping[str, Iterable[str]],
    permissions: Permissions | None = None,
    personal_fields: Mapping[str, Sequence[str]] | None = None,
    command: Sequence[str] | None = None,
    horae: str = DEFAULT_HORAE,
    description: str | None = None,
    license: str | None = None,
    homepage: str | None = None,
) -> dict[str, Any]:
    """A `plugin.yaml` document (see the module docstring). `command` is the `mcp-stdio`
    argument list; without one the manifest has no runtime (a skill or pipeline pack).
    `personal_fields` (`{<source or transform ref>: [view paths]}`, horae-sdk 0.7; a
    scan-only path prefixed by `free_text:`, 0.9) becomes `provides.personal_fields`, left
    out when empty."""
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
    personal = _personal_fields(personal_fields or {}, {*listed["sources"], *listed["transforms"]})
    if personal:
        data["provides"]["personal_fields"] = personal
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
    "MAX_PERSONAL_FIELDS",
    "SINK_OPERATIONS",
    "TOOL_KINDS",
    "ManifestError",
    "Permissions",
    "dump_manifest",
    "manifest",
    "write_manifest",
]
