# SPDX-License-Identifier: Apache-2.0
"""A `PluginRegistry` built from `horae.lock` alone, until the plugin host (#24) loads manifests.

`horae.lock` pins each plugin's version and commit and the permissions the user approved,
which is what replay needs to validate and route intents and to compute the LLM cache key. It
does not hold the manifest's `provides` or how a tool namespace maps to a plugin, so:

- a namespace resolves to the locked plugin **of the same name** (`github.review_requests` to
  plugin `github`, `daily-brief/rank` to plugin `daily-brief`); a namespace that is not a
  plugin name (e.g. `coach.brief` from plugin `mcp-brief`) does not resolve (`unresolved`);
- the plugin is taken to provide every tool and skill of its namespace that the pipeline uses,
  in the kind of step that uses it (the lock cannot say otherwise).
"""

from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import (
    ActStep,
    CollectStep,
    LlmStep,
    NotifyStep,
    Pipeline,
    TransformStep,
)
from horae.contracts.plugin import PluginManifest, Provides
from horae.engine.ports import ResolvedPlugin, UnknownPlugin

_KINDS = (
    (CollectStep, "collect", "sources"),
    (TransformStep, "transform", "transforms"),
    (ActStep, "act", "sinks"),
    (NotifyStep, "notify", "notifiers"),
)


def _refs(pipeline: Pipeline) -> dict[str, set[str]]:
    """The tools (by `provides` field) and skills the pipeline uses."""
    used: dict[str, set[str]] = {
        "sources": set(),
        "transforms": set(),
        "sinks": set(),
        "notifiers": set(),
        "skills": set(),
    }
    for step in pipeline.steps:
        if isinstance(step, LlmStep):
            used["skills"].add(step.llm.skill)
            continue
        for model, attribute, provides in _KINDS:
            if isinstance(step, model):
                for call in getattr(step, attribute):
                    used[provides].add(call if isinstance(call, str) else next(iter(call)))
    return used


def _namespace(ref: str, separator: str) -> str:
    return ref.split(separator, 1)[0]


class LockRegistry:
    """Resolves the pipeline's tools and skills to the plugins pinned in `horae.lock`."""

    def __init__(self, lock: Lockfile, pipeline: Pipeline) -> None:
        used = _refs(pipeline)
        self._plugins: dict[str, ResolvedPlugin] = {}
        for name, locked in lock.plugins.items():
            provides = {
                field: sorted(
                    ref
                    for ref in refs
                    if _namespace(ref, "/" if field == "skills" else ".") == name
                )
                for field, refs in used.items()
            }
            manifest = PluginManifest(
                name=name,
                version=locked.version,
                horae=">=0",
                provides=Provides.model_validate(provides),
                permissions=locked.permissions,
            )
            self._plugins[name] = ResolvedPlugin(manifest, locked.commit)
        self.unresolved: tuple[str, ...] = tuple(
            sorted(
                ref
                for field, refs in used.items()
                for ref in refs
                if _namespace(ref, "/" if field == "skills" else ".") not in self._plugins
            )
        )
        """Tools and skills of the pipeline that no locked plugin name matches."""

    def _get(self, namespace: str) -> ResolvedPlugin:
        try:
            return self._plugins[namespace]
        except KeyError:
            raise UnknownPlugin(namespace) from None

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        return self._get(_namespace(ref, "."))

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        return self._get(_namespace(skill, "/"))
