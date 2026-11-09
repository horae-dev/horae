# SPDX-License-Identifier: Apache-2.0
"""Reading a viewed input by input view path (schemas decision 67).

Shared by the `llm` step's `skip_when_empty` and the action guard's `task_fingerprint` rule:
both name fields of the **viewed** input (what the model was shown) with the same `ViewPath`
syntax as a skill's `input_view` (`[].data.items[].id`).
"""

from typing import Any

from horae.contracts.skill import view_steps


def values_at(viewed: Any, path: str) -> list[Any]:
    """Every value the view `path` reaches in `viewed` (`[]` maps over arrays). A key that is
    absent, or a value of the wrong shape, reaches nothing."""
    found: list[Any] = [viewed]
    for step in view_steps(path):
        if step == "[]":
            found = [item for value in found if isinstance(value, list) for item in value]
        else:
            found = [value[step] for value in found if isinstance(value, dict) and step in value]
    return found


def is_empty(value: Any) -> bool:
    """Whether `value` carries nothing: `null`, an empty string, an empty list or object, or a
    list or object whose members are all empty in this sense. A number or a boolean (`0`,
    `false`) is never empty, and neither is any non-empty string."""
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if item is None or item == "":
            continue
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        else:
            return False
    return True


def ids_at(viewed: Any, path: str) -> frozenset[str]:
    """The strings at view `path` in `viewed`: a string value, or the strings of a list value."""
    found: set[str] = set()
    for value in values_at(viewed, path):
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, list):
            found.update(item for item in value if isinstance(item, str))
    return frozenset(found)
