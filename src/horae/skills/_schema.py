# SPDX-License-Identifier: Apache-2.0
"""The output JSON Schema of a skill: draft 2020-12, self-contained and bounded.

`check_output_schema(schema)` returns the schema's **size bound**, the length in characters
of the longest compact JSON document it accepts (keys, quotes, commas and brackets
included; a string counts its `maxLength` in code points, as if no character needed
escaping), or raises `SchemaProblem`. Rules, beyond the draft 2020-12 metaschema:

- `$schema`, when present, is the draft 2020-12 dialect; the root is `"type": "object"` (what
  the structured-output modes of the backends, #28, accept);
- self-contained: `$ref` is local (`#` or `#/...`), and `$id`, `$anchor`, `$dynamicRef`,
  `$dynamicAnchor`, `$recursiveRef`, `$recursiveAnchor` and `$vocabulary` are refused, so
  nothing is ever fetched or re-based; a `$ref` cycle (a recursive schema) is unbounded;
- bounded: every subschema a value is checked against has a `type`, `enum`, `const`, `$ref`
  or an `allOf` / `anyOf` / `oneOf` (`true` and `{}` accept anything); each `type` is bounded
  on its own: a `string` has `maxLength`, an `array` has `maxItems` (and `items` for the
  elements past its `prefixItems`), an `object` has `additionalProperties: false` (no
  `patternProperties`). A number counts 32 characters, `true` / `false` 5, `null` 4;
- the bound is at most `MAX_OUTPUT_CHARS`, and the schema nests at most `MAX_SCHEMA_DEPTH`
  levels of JSON.

A value satisfies every keyword of its schema, so a schema's bound is the smallest of what
its `type`, `enum` / `const`, `$ref` and combinators allow (`anyOf` / `oneOf`: the largest
branch; `allOf`: the smallest).
"""

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_OUTPUT_CHARS = 256 * 1024
"""Largest size bound of an output schema, in characters of compact JSON."""
MAX_SCHEMA_DEPTH = 64
"""Deepest JSON nesting of an output schema file (like `horae.config` YAML documents)."""

_FORBIDDEN = (
    "$id",
    "$anchor",
    "$dynamicRef",
    "$dynamicAnchor",
    "$recursiveRef",
    "$recursiveAnchor",
    "$vocabulary",
    "patternProperties",
)
_ONE = (
    "additionalProperties",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
_MAP = ("properties", "$defs", "definitions", "dependentSchemas")
_LIST = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCALAR = {"integer": 32, "number": 32, "boolean": 5, "null": 4}


class SchemaProblem(ValueError):
    """The output schema is invalid, not self-contained or not bounded."""


def depth(value: Any) -> int:
    """How deeply `value` (parsed JSON) nests arrays and objects (iterative)."""
    deepest = 0
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict | list):
            level += 1
            deepest = max(deepest, level)
            if level > MAX_SCHEMA_DEPTH:
                return level
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, level) for child in children)
    return deepest


def _compact(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _subschemas(node: Mapping[str, Any], where: str) -> list[tuple[Any, str]]:
    found: list[tuple[Any, str]] = []
    for key in _ONE:
        if key in node:
            found.append((node[key], f"{where}/{key}"))
    for key in _MAP:
        if isinstance(node.get(key), dict):
            found.extend((v, f"{where}/{key}/{k}") for k, v in node[key].items())
    for key in _LIST:
        if isinstance(node.get(key), list):
            found.extend((v, f"{where}/{key}/{i}") for i, v in enumerate(node[key]))
    return found


def _self_contained(schema: Any) -> None:
    pending: list[tuple[Any, str]] = [(schema, "#")]
    while pending:
        node, where = pending.pop()
        if not isinstance(node, dict):
            continue
        for key in _FORBIDDEN:
            if key in node:
                raise SchemaProblem(f"{where}: `{key}` is not allowed in a skill output schema")
        ref = node.get("$ref")
        if ref is not None and not (ref == "#" or ref.startswith("#/")):
            raise SchemaProblem(f"{where}: `$ref` must be local (`#/...`), not {ref!r}")
        pending.extend(_subschemas(node, where))


class _Bounder:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.refs: dict[str, int] = {}

    def bound(self, node: Any, where: str, stack: tuple[str, ...]) -> int:
        if node is True:
            raise SchemaProblem(f"{where}: `true` accepts any value (unbounded)")
        if node is False:
            return 0
        candidates: list[int] = []
        if "const" in node:
            candidates.append(_compact(node["const"]))
        if "enum" in node:
            candidates.append(max((_compact(value) for value in node["enum"]), default=0))
        if "$ref" in node:
            candidates.append(self._ref(node["$ref"], where, stack))
        for key in ("anyOf", "oneOf"):
            if key in node:
                candidates.append(max(self._branches(node, key, where, stack)))
        if "allOf" in node:
            candidates.append(min(self._branches(node, "allOf", where, stack)))
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            candidates.append(max(self._typed(node, kind, where, stack) for kind in types))
        if not candidates:
            raise SchemaProblem(
                f"{where}: accepts any value; give it a `type`, `enum`, `const` or `$ref`"
            )
        return min(candidates)

    def _branches(
        self, node: dict[str, Any], key: str, where: str, stack: tuple[str, ...]
    ) -> list[int]:
        return [self.bound(b, f"{where}/{key}/{i}", stack) for i, b in enumerate(node[key])]

    def _typed(self, node: dict[str, Any], kind: str, where: str, stack: tuple[str, ...]) -> int:
        if kind in _SCALAR:
            return _SCALAR[kind]
        if kind == "string":
            if "maxLength" not in node:
                raise SchemaProblem(f"{where}: a string needs `maxLength`")
            return int(node["maxLength"]) + 2
        if kind == "array":
            if "maxItems" not in node:
                raise SchemaProblem(f"{where}: an array needs `maxItems`")
            count = int(node["maxItems"])
            prefix = node.get("prefixItems", [])
            total = 2 + count
            for index, item in enumerate(prefix[:count]):
                total += self.bound(item, f"{where}/prefixItems/{index}", stack)
            rest = count - len(prefix)
            if rest > 0:
                if "items" not in node:
                    raise SchemaProblem(f"{where}: an array needs `items`")
                total += rest * self.bound(node["items"], f"{where}/items", stack)
            return total
        # "object" (the metaschema allows no other type name)
        if node.get("additionalProperties") is not False:
            raise SchemaProblem(f"{where}: an object needs `additionalProperties: false`")
        total = 2
        for key, value in node.get("properties", {}).items():
            total += _compact(key) + 2 + self.bound(value, f"{where}/properties/{key}", stack)
        return total

    def _ref(self, ref: str, where: str, stack: tuple[str, ...]) -> int:
        if ref in stack:
            raise SchemaProblem(f"{where}: `$ref` {ref!r} is recursive (unbounded)")
        if ref not in self.refs:
            self.refs[ref] = self.bound(self._resolve(ref, where), ref, (*stack, ref))
        return self.refs[ref]

    def _resolve(self, ref: str, where: str) -> Any:
        target: Any = self.root
        for raw in ref[2:].split("/") if ref != "#" else []:
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                target = target[int(part)]
            else:
                raise SchemaProblem(f"{where}: `$ref` {ref!r} points to nothing")
        if not isinstance(target, dict | bool):
            raise SchemaProblem(f"{where}: `$ref` {ref!r} is not a schema")
        return target


def check_output_schema(schema: Any) -> int:
    """The size bound of `schema` (see the module docstring); raises `SchemaProblem`."""
    if not isinstance(schema, dict):
        raise SchemaProblem("the output schema must be a JSON object")
    if depth(schema) > MAX_SCHEMA_DEPTH:
        raise SchemaProblem(f"the output schema nests deeper than {MAX_SCHEMA_DEPTH} levels")
    dialect = schema.get("$schema")
    if dialect is not None and dialect != DIALECT:
        raise SchemaProblem(f"`$schema` must be {DIALECT}")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        place = "/".join(str(part) for part in error.path)
        raise SchemaProblem(
            f"not a valid draft 2020-12 schema (at #/{place}): {error.message[:200]}"
        ) from None
    _self_contained(schema)
    if schema.get("type") != "object":
        raise SchemaProblem('the output schema\'s root must be `"type": "object"`')
    size = _Bounder(schema).bound(schema, "#", ())
    if size > MAX_OUTPUT_CHARS:
        raise SchemaProblem(
            f"the output schema accepts documents of up to {size} characters "
            f"(at most {MAX_OUTPUT_CHARS})"
        )
    return size
