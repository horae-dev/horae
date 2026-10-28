# SPDX-License-Identifier: Apache-2.0
"""The input view: the allow-list of fields of the step input that the model sees."""

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.skill import view_steps


@dataclass
class _Node:
    whole: bool = False
    keys: dict[str, "_Node"] = field(default_factory=dict)
    items: "_Node | None" = None


class _Missing:
    """Nothing selected (distinct from a selected `null`)."""


_MISSING = _Missing()


class InputView:
    """Applies validated `ViewPath`s (`SkillManifest.input_view`) to the step input.

    `apply(data)` returns a new value holding exactly the declared fields:

    - a path selects the whole value it ends on (`tasks` selects all of `tasks`);
    - `name` keeps that key of an object; `[]` maps over every element of an array;
    - an object keeps only the declared keys that are present (an object with none of them
      becomes `{}`); an array keeps its order, and drops an element of the wrong shape (a
      string where `tasks[].title` expects objects);
    - a value of the wrong shape (an array where a path expects an object, or the reverse)
      selects nothing; when the root itself has the wrong shape, the view is `null`.

    The result is deterministic: object keys are written in sorted order and the input is
    never shared with the result (selected values are copied). Undeclared fields never reach
    it, at any depth."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = tuple(sorted(set(paths)))
        root = _Node()
        for path in self.paths:
            node = root
            for step in view_steps(path):
                if step == "[]":
                    if node.items is None:
                        node.items = _Node()
                    node = node.items
                else:
                    node = node.keys.setdefault(step, _Node())
            node.whole = True
        self._root = root

    def apply(self, data: Any) -> Any:
        selected = _select(data, self._root)
        return None if isinstance(selected, _Missing) else selected


def _select(value: Any, node: _Node) -> Any:
    if node.whole:
        return copy.deepcopy(value)
    if node.items is not None:
        if not isinstance(value, list):
            return _MISSING
        kept = (_select(item, node.items) for item in value)
        return [item for item in kept if not isinstance(item, _Missing)]
    if not isinstance(value, dict):
        return _MISSING
    out: dict[str, Any] = {}
    for key in sorted(node.keys):
        if key in value:
            selected = _select(value[key], node.keys[key])
            if not isinstance(selected, _Missing):
                out[key] = selected
    return out
