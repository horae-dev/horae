# SPDX-License-Identifier: Apache-2.0
"""The output JSON Schema of a skill: draft 2020-12, self-contained and bounded.

`check_output_schema(schema)` returns the schema's **size bound** in UTF-8 **bytes**: no
document the schema accepts is longer, serialised as compact JSON
(`json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()`), or raises
`SchemaProblem`. The bound is a worst case, so it is sound whatever the characters are: a string
of `maxLength` code points counts `6 * maxLength + 2` (every code point escaped as `\\uXXXX`,
which is more than its 1 to 4 UTF-8 bytes); a number counts the characters of its largest
extreme (see below); keys, `const` and `enum` values count their exact serialisation. #31
validates the backend's parsed output against the schema and re-serialises it in this form,
so the bound holds for what it stores. Rules, beyond the draft 2020-12 metaschema:

- `$schema`, when present, is the draft 2020-12 dialect; the root is `"type": "object"` (what
  the structured-output modes of the backends, #28, accept);
- self-contained: `$ref` is local (`#` or `#/...`), and `$id`, `$anchor`, `$dynamicRef`,
  `$dynamicAnchor`, `$recursiveRef`, `$recursiveAnchor` and `$vocabulary` are refused, so
  nothing is ever fetched or re-based; a `$ref` cycle (a recursive schema) is refused, and so is
  a chain of more than `MAX_REF_HOPS` `$ref`s;
- no regular expressions: `pattern`, `patternProperties`, `propertyNames` and
  `"format": "regex"` are refused (a regex chosen by a plugin author is a ReDoS risk; #31 must
  still run validation in a worker it can kill on a timeout);
- bounded: every subschema a value is checked against has a `type`, `enum`, `const`, `$ref`
  or an `allOf` / `anyOf` / `oneOf` (`true` and `{}` accept anything); each `type` is bounded
  on its own: a `string` has `maxLength`, an `array` has `maxItems` (and `items` for the
  elements past its `prefixItems`), an `object` has `additionalProperties: false`, and an
  `integer` or a `number` has a finite lower (`minimum` / `exclusiveMinimum`) and upper
  (`maximum` / `exclusiveMaximum`) bound: it counts the digits of the larger extreme (an
  `integer` may be written `3.0`: two more), and at least `MAX_FLOAT_REPR` for a `number` or
  an extreme of 10^15 or more (the longest `repr` of a finite float); `true` / `false` 5,
  `null` 4;
- the bound is at most `MAX_OUTPUT_BYTES`, and the schema nests at most `MAX_SCHEMA_DEPTH`
  levels of JSON.

A value satisfies every keyword of its schema, so a schema's bound is the smallest of what
its `type`, `enum` / `const`, `$ref` and combinators allow (`anyOf` / `oneOf`: the largest
branch; `allOf`: the smallest of its bounded branches). A keyword that does not bound
(`allOf` of unbounded branches beside a bounded `type`) is skipped; the subschema is refused
only when none bounds it.

**Intents** (#28). The LLM proposes intents in a reserved top-level property `intents` of its
output, which the backends extract. `proposes_intents=True` (the skill declares `intents`)
requires the root to declare `properties.intents` as a bounded `array`; `False` refuses it.
"""

import json
import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from horae.llm.base import INTENTS_KEY

DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_OUTPUT_BYTES = 256 * 1024
"""Largest size bound of an output schema, in UTF-8 bytes of compact JSON."""
MAX_SCHEMA_DEPTH = 64
"""Deepest JSON nesting of an output schema file (like `horae.config` YAML documents)."""
MAX_REF_HOPS = 32
"""Longest chain of `$ref`s followed while bounding."""
MAX_FLOAT_REPR = 25
"""More than the longest `repr` of a finite float (`-1.7976931348623157e+308`, 24)."""
INTENTS_PROPERTY = INTENTS_KEY
"""The reserved top-level output property that carries the LLM's proposed intents: the one
`horae.llm.ModelBackends` extracts into `LlmResponse.intents` (#28)."""

_FORBIDDEN = (
    "$id",
    "$anchor",
    "$dynamicRef",
    "$dynamicAnchor",
    "$recursiveRef",
    "$recursiveAnchor",
    "$vocabulary",
    "pattern",
    "patternProperties",
    "propertyNames",
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
_SCALAR = {"boolean": 5, "null": 4}
_PLAIN_FLOAT = 10**15
"""Below this magnitude an integral float's `repr` is its digits and `.0`."""


class SchemaProblem(ValueError):
    """The output schema is invalid, not self-contained or not bounded."""


class _Unbounded(SchemaProblem):
    """A subschema that does not bound its value (skipped when another keyword does)."""


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


def _number_bytes(value: int | float) -> int:
    """The longest serialisation of a JSON number equal to `value` (`1` and `1.0` are equal
    for `const` / `enum`)."""
    if isinstance(value, float):
        size = len(repr(value))
        if value.is_integer():
            size = max(size, len(str(int(value))))
        return size
    size = len(str(value))
    if abs(value) < _PLAIN_FLOAT:
        return size + 2  # `3.0`
    return max(size, MAX_FLOAT_REPR)


def _value_bytes(value: Any) -> int:
    """The longest compact UTF-8 serialisation of a JSON value equal to `value`."""
    if isinstance(value, bool) or value is None:
        return len(json.dumps(value))
    if isinstance(value, int | float):
        return _number_bytes(value)
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=False).encode())
    if isinstance(value, list):
        return 2 + max(len(value) - 1, 0) + sum(_value_bytes(item) for item in value)
    if isinstance(value, dict):
        items = [_value_bytes(key) + 1 + _value_bytes(item) for key, item in value.items()]
        return 2 + max(len(items) - 1, 0) + sum(items)
    raise SchemaProblem(f"not a JSON value: {type(value).__name__}")  # pragma: no cover


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
        if node.get("format") == "regex":
            raise SchemaProblem(f'{where}: `"format": "regex"` is not allowed')
        ref = node.get("$ref")
        if ref is not None and not (ref == "#" or ref.startswith("#/")):
            raise SchemaProblem(f"{where}: `$ref` must be local (`#/...`), not {ref!r}")
        pending.extend(_subschemas(node, where))


def _extreme(node: Mapping[str, Any], inclusive: str, exclusive: str) -> int | float | None:
    """The tightest of the inclusive and exclusive bound on one side (`None`: neither)."""
    values: list[int | float] = [node[key] for key in (inclusive, exclusive) if key in node]
    if not values:
        return None
    return max(values) if inclusive == "minimum" else min(values)


class _Bounder:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.refs: dict[str, int | SchemaProblem] = {}

    def bound(self, node: Any, where: str, stack: tuple[str, ...]) -> int:
        if node is True:
            raise _Unbounded(f"{where}: `true` accepts any value (unbounded)")
        if node is False:
            return 0
        candidates: list[int] = []
        first: _Unbounded | None = None

        def consider(compute: Any, *args: Any) -> None:
            nonlocal first
            try:
                candidates.append(compute(*args))
            except _Unbounded as problem:
                first = first or problem

        if "const" in node:
            candidates.append(_value_bytes(node["const"]))
        if "enum" in node:
            candidates.append(max((_value_bytes(value) for value in node["enum"]), default=0))
        if "$ref" in node:
            consider(self._ref, node["$ref"], where, stack)
        for key in ("anyOf", "oneOf"):
            if key in node:
                consider(lambda k: max(self._branches(node, k, where, stack)), key)
        if "allOf" in node:
            consider(self._all_of, node, where, stack)
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            consider(lambda: max(self._typed(node, kind, where, stack) for kind in types))
        if candidates:
            return min(candidates)
        if first is not None:
            raise first
        raise _Unbounded(f"{where}: accepts any value; give it a `type`, `enum`, `const` or `$ref`")

    def _branches(
        self, node: dict[str, Any], key: str, where: str, stack: tuple[str, ...]
    ) -> list[int]:
        return [self.bound(b, f"{where}/{key}/{i}", stack) for i, b in enumerate(node[key])]

    def _all_of(self, node: dict[str, Any], where: str, stack: tuple[str, ...]) -> int:
        bounded: list[int] = []
        first: _Unbounded | None = None
        for index, branch in enumerate(node["allOf"]):
            try:
                bounded.append(self.bound(branch, f"{where}/allOf/{index}", stack))
            except _Unbounded as problem:
                first = first or problem
        if not bounded:
            raise first or _Unbounded(f"{where}/allOf: no branch is bounded")
        return min(bounded)

    def _numeric(self, node: dict[str, Any], kind: str, where: str) -> int:
        low = _extreme(node, "minimum", "exclusiveMinimum")
        high = _extreme(node, "maximum", "exclusiveMaximum")
        if low is None or high is None:
            raise _Unbounded(
                f"{where}: an {kind} needs a finite lower and upper bound "
                "(`minimum` / `exclusiveMinimum` and `maximum` / `exclusiveMaximum`), "
                f"else it is an unbounded {kind}"
            )
        extremes = [math.floor(low), math.ceil(high)]  # finite: the loader refuses inf
        digits = max(len(str(int(value))) for value in extremes)
        largest = max(abs(int(value)) for value in extremes)
        if kind == "number" or largest >= _PLAIN_FLOAT:
            return max(digits, MAX_FLOAT_REPR)
        return digits + 2  # an integer may be written `3.0`

    def _typed(self, node: dict[str, Any], kind: str, where: str, stack: tuple[str, ...]) -> int:
        if kind in _SCALAR:
            return _SCALAR[kind]
        if kind in ("integer", "number"):
            return self._numeric(node, kind, where)
        if kind == "string":
            if "maxLength" not in node:
                raise _Unbounded(f"{where}: a string needs `maxLength`")
            return 6 * int(node["maxLength"]) + 2
        if kind == "array":
            if "maxItems" not in node:
                raise _Unbounded(f"{where}: an array needs `maxItems`")
            count = int(node["maxItems"])
            prefix = node.get("prefixItems", [])
            total = 2 + max(count - 1, 0)
            for index, item in enumerate(prefix[:count]):
                total += self.bound(item, f"{where}/prefixItems/{index}", stack)
            rest = count - len(prefix)
            if rest > 0:
                if "items" not in node:
                    raise _Unbounded(f"{where}: an array needs `items`")
                total += rest * self.bound(node["items"], f"{where}/items", stack)
            return total
        # "object" (the metaschema allows no other type name)
        if node.get("additionalProperties") is not False:
            raise _Unbounded(f"{where}: an object needs `additionalProperties: false`")
        properties = node.get("properties", {})
        total = 2 + max(len(properties) - 1, 0)
        for key, value in properties.items():
            total += _value_bytes(key) + 1 + self.bound(value, f"{where}/properties/{key}", stack)
        return total

    def _ref(self, ref: str, where: str, stack: tuple[str, ...]) -> int:
        if ref in stack:
            raise SchemaProblem(f"{where}: `$ref` {ref!r} is recursive (unbounded)")
        if len(stack) >= MAX_REF_HOPS:
            raise SchemaProblem(f"{where}: more than {MAX_REF_HOPS} chained `$ref`s")
        if ref not in self.refs:
            try:
                self.refs[ref] = self.bound(self._resolve(ref, where), ref, (*stack, ref))
            except _Unbounded as problem:
                self.refs[ref] = problem
        found = self.refs[ref]
        if isinstance(found, SchemaProblem):
            raise found
        return found

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


def _intents_property(schema: dict[str, Any], proposes: bool) -> None:
    properties = schema.get("properties", {})
    declared = isinstance(properties, dict) and INTENTS_PROPERTY in properties
    if not proposes:
        if declared:
            raise SchemaProblem(
                f"#/properties/{INTENTS_PROPERTY}: reserved for proposed intents, and the "
                "skill declares no `intents`"
            )
        return
    node = properties.get(INTENTS_PROPERTY) if declared else None
    if not isinstance(node, dict) or node.get("type") != "array":
        raise SchemaProblem(
            f"the skill declares `intents`: the output schema needs a top-level "
            f'`properties.{INTENTS_PROPERTY}` of `"type": "array"` (where the LLM proposes them)'
        )


def check_output_schema(schema: Any, *, proposes_intents: bool | None = None) -> int:
    """The size bound of `schema` (see the module docstring); raises `SchemaProblem`, and
    only that (a recursion too deep or a value that cannot be encoded included).

    `proposes_intents`: whether the skill declares intents (the reserved `intents`
    property is then required, else refused); `None` skips that check."""
    try:
        return _check(schema, proposes_intents)
    except SchemaProblem:
        raise
    except RecursionError:
        raise SchemaProblem("the output schema nests too deeply (through its `$ref`s)") from None
    except (UnicodeError, OverflowError, ValueError) as error:
        message = f"the output schema cannot be checked ({type(error).__name__})"
        raise SchemaProblem(message) from None


def _check(schema: Any, proposes_intents: bool | None) -> int:
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
    if proposes_intents is not None:
        _intents_property(schema, proposes_intents)
    size = _Bounder(schema).bound(schema, "#", ())
    if size > MAX_OUTPUT_BYTES:
        raise SchemaProblem(
            f"the output schema accepts documents of up to {size} bytes "
            f"(at most {MAX_OUTPUT_BYTES})"
        )
    return size
