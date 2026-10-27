# SPDX-License-Identifier: Apache-2.0
"""Shared base model and constrained types for the Horae contracts.

The patterns avoid look-around and use ASCII classes (`[0-9]` rather than `\\d`) so that
pydantic (Rust regex) and JSON Schema validators (ECMA-262 regex) accept exactly the same
strings.
"""

from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

T = TypeVar("T")


class Contract(BaseModel):
    """Base for every contract model: unknown keys are rejected, no type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, use_attribute_docstrings=True)


def _unique[I](items: list[I]) -> list[I]:
    seen: list[I] = []
    for item in items:
        if item in seen:
            raise ValueError(f"duplicate item {item!r}")
        seen.append(item)
    return items


UniqueList = Annotated[
    list[T], AfterValidator(_unique), Field(json_schema_extra={"uniqueItems": True})
]
"""A list whose items must be unique (`uniqueItems` in JSON Schema)."""

PLUGIN_NAME = r"[a-z][a-z0-9-]*"

PluginName = Annotated[str, StringConstraints(pattern=rf"^{PLUGIN_NAME}$", max_length=64)]
"""A plugin name, e.g. `github` or `mcp-brief`."""

PluginRef = Annotated[
    str, StringConstraints(pattern=rf"^{PLUGIN_NAME}\.[a-z][a-z0-9_]*$", max_length=128)
]
"""A tool provided by a plugin, `<plugin>.<tool>`, e.g. `github.review_requests`."""

SkillRef = Annotated[
    str, StringConstraints(pattern=rf"^{PLUGIN_NAME}/[a-z][a-z0-9_-]*$", max_length=128)
]
"""A skill provided by a plugin, `<plugin>/<skill>`, e.g. `daily-brief/rank-and-word`."""

PipelineName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)]
"""A pipeline name, e.g. `daily-brief`."""

SemVer = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
        )
    ),
]
"""A semantic version, e.g. `0.1.0`."""

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
"""A full git commit id: 40 (SHA-1) or 64 (SHA-256) lowercase hex characters."""

Hostname = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$",
        max_length=253,
    ),
]
"""A lowercase DNS host name, without scheme, port or wildcard, e.g. `api.github.com`."""

SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=128)]
"""An environment-variable style secret name, e.g. `GITHUB_TOKEN`."""

HttpUrl = Annotated[str, StringConstraints(pattern=r"^https?://[^\s]+$", max_length=2048)]
"""An absolute `http` or `https` URL (kept as a plain string, never normalised)."""

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
"""A non-empty string."""
