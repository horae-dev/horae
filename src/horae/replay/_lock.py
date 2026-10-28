# SPDX-License-Identifier: Apache-2.0
"""A `PluginRegistry` built from `horae.lock` and the run record, until the plugin host (#24).

`horae.lock` pins each plugin's version and commit and the permissions the user approved, which
is what replay needs to validate and route intents and to compute the LLM cache key. It does
not hold the manifest's `provides`, so which plugin serves a tool or skill comes from:

1. the run record: `RunRecord.plugins[<name>].provides` lists the refs the run resolved to that
   plugin (`coach.brief` to `mcp-brief`, `daily-brief/rank-and-word` to `pack-daily-brief`),
   used when that plugin is still in `horae.lock`;
2. otherwise the locked plugin **named after the namespace** (`github.review_requests` to
   plugin `github`); a ref that neither resolves is `unresolved`.

A plugin is taken to provide each ref resolved to it, in the kind of step that uses it (the
lock cannot say otherwise). Its permissions are the locked ones. Nothing is read but the two
documents given: no file, no process, no network.
"""

from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import (
    ActStep,
    CollectStep,
    LlmStep,
    NotifyStep,
    Pipeline,
    TransformStep,
    call_parts,
)
from horae.contracts.plugin import PluginManifest, Provides
from horae.contracts.run import RunRecord
from horae.engine.ports import ResolvedPlugin, UnknownPlugin

_KINDS = (
    (CollectStep, "collect", "sources"),
    (TransformStep, "transform", "transforms"),
    (ActStep, "act", "sinks"),
    (NotifyStep, "notify", "notifiers"),
)
_FIELDS = ("sources", "transforms", "sinks", "notifiers", "skills")


def pipeline_refs(pipeline: Pipeline | None) -> dict[str, set[str]]:
    """The tools (by `provides` field) and skills the pipeline uses."""
    used: dict[str, set[str]] = {field: set() for field in _FIELDS}
    for step in pipeline.steps if pipeline is not None else ():
        if isinstance(step, LlmStep):
            used["skills"].add(step.llm.skill)
            continue
        for model, attribute, provides in _KINDS:
            if isinstance(step, model):
                for call in getattr(step, attribute):
                    used[provides].add(call_parts(call)[0])
    return used


def _namespace(ref: str, field: str) -> str:
    return ref.split("/" if field == "skills" else ".", 1)[0]


class LockRegistry:
    """Resolves the pipeline's tools and skills to the plugins pinned in `horae.lock`, the way
    the run `record` resolved them when it says (see the module docstring)."""

    def __init__(
        self, lock: Lockfile, pipeline: Pipeline | None, record: RunRecord | None = None
    ) -> None:
        owners: dict[str, str] = {}
        if record is not None:
            for name, version in sorted(record.plugins.items()):
                for ref in version.provides or ():
                    owners.setdefault(ref, name)
        provided: dict[str, dict[str, list[str]]] = {
            name: {field: [] for field in _FIELDS} for name in lock.plugins
        }
        self._tools: dict[str, str] = {}
        self._skills: dict[str, str] = {}
        unresolved: list[str] = []
        for field, refs in pipeline_refs(pipeline).items():
            for ref in sorted(refs):
                name = owners.get(ref, "")
                if name not in lock.plugins:
                    name = _namespace(ref, field)
                if name not in lock.plugins:
                    unresolved.append(ref)
                    continue
                provided[name][field].append(ref)
                (self._skills if field == "skills" else self._tools)[ref] = name
        self._plugins: dict[str, ResolvedPlugin] = {}
        for name, locked in lock.plugins.items():
            manifest = PluginManifest(
                name=name,
                version=locked.version,
                horae=">=0",
                provides=Provides.model_validate(provided[name]),
                permissions=locked.permissions,
            )
            self._plugins[name] = ResolvedPlugin(manifest, locked.commit)
        self.unresolved: tuple[str, ...] = tuple(sorted(set(unresolved)))
        """Tools and skills of the pipeline that resolve to no locked plugin."""

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        try:
            return self._plugins[self._tools[ref]]
        except KeyError:
            raise UnknownPlugin(ref) from None

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        try:
            return self._plugins[self._skills[skill]]
        except KeyError:
            raise UnknownPlugin(skill) from None
