# SPDX-License-Identifier: Apache-2.0
"""Which sink applies an `/act` change (#38, schemas decision 53 F).

A button's change is a `task.update_status` the core builds itself; it is applied by the
instance's **one** `mode: tasks` `act` entry, as decision 30 routes a `task.update_status`
(rule 1 of `horae.engine.executor`: two such entries would make every one ambiguous). The
entry is taken from the instance's pipelines with its parameters as written: an entry whose
parameters hold a template (`{{`) cannot be rendered outside a run and is not used. Zero or
several distinct entries: there is no route, `/act` answers 503 and no button is minted.
Whether the entry's plugin may apply `task.update_status` is checked by `horae serve` with the
plugin registry (`route_permitted`) and again by the plugin (the SDK refuses an undeclared
type).
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.pipeline import ActStep, Pipeline, call_parts


@dataclass(frozen=True)
class ActRoute:
    """The `act` entry applying `/act` changes: its tool and trusted parameters."""

    tool: str
    params: dict[str, Any] = field(default_factory=dict)


def _templated(value: Any) -> bool:
    return "{{" in json.dumps(value, ensure_ascii=False)


def tasks_route(pipelines: Mapping[str, Pipeline]) -> ActRoute | None:
    """The instance's one `mode: tasks` entry (see the module docstring), or `None`."""
    found: dict[str, ActRoute] = {}
    for pipeline in pipelines.values():
        for step in pipeline.steps:
            if not isinstance(step, ActStep):
                continue
            for call in step.act:
                ref, params, _ = call_parts(call)
                if params.get("mode") != "tasks":
                    continue
                key = json.dumps([ref, params], sort_keys=True, separators=(",", ":"))
                found[key] = ActRoute(ref, json.loads(json.dumps(params)))
    if len(found) != 1:
        return None
    (route,) = found.values()
    return None if _templated(route.params) else route


def route_permitted(route: ActRoute, intents_of: Callable[[str], frozenset[str]]) -> bool:
    """Whether the plugin serving `route.tool` may apply `task.update_status`
    (`intents_of(tool)`: its approved `permissions.intents`; any error: no)."""
    try:
        return "task.update_status" in intents_of(route.tool)
    except Exception:
        return False


__all__ = ["ActRoute", "route_permitted", "tasks_route"]
