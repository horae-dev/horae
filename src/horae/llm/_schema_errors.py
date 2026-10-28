# SPDX-License-Identifier: Apache-2.0
"""The schema errors of an LLM output, as the repair retry may see them (#31, decision 44).

`schema_errors(output, schema)` validates `output` against the skill's **full** output schema
(JSON Schema draft 2020-12, the skill's own file: not the provider's transformed copy) and
checks the reserved top-level `intents` property, then describes each failure **only** by
where it is (a JSON Pointer), the keyword that failed and a short description written by
Horae. Nothing of the output itself is ever part of an error: no value, no excerpt and no key
the schema does not declare (such a path segment is written `*`), so that the repair prompt
carries no text the model (or the data it was shown) chose: a second-order injection through
the error messages is not possible. Numbers in a description come from the schema (trusted:
the skill's author wrote it), never from the output.

The errors are de-duplicated, sorted (deterministic for a given output and schema, so a
replay sends the same repair) and capped: at most `MAX_ERRORS` of at most `MAX_ERROR_CHARS`
characters each.

This module is also the validation **worker** (`main`): `horae.llm.validate` runs it as a
script in a short-lived child process (`python -I <this file>`), which it kills on a timeout.
It therefore imports nothing but the standard library and `jsonschema`, and nothing of
`horae`, so the child starts fast and reads no instance state."""

import itertools
import json
import math
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

INTENTS_KEY = "intents"
"""The reserved top-level output property (`horae.llm.base.INTENTS_KEY`)."""
MAX_ERRORS = 16
"""Most errors kept (the last one says how many more there were)."""
MAX_ERROR_CHARS = 200
"""Longest error kept."""
MAX_SCANNED = 1000
"""Most validation failures examined before sorting (the validator stops there)."""
MAX_POINTER_CHARS = 120
"""Longest JSON Pointer kept in an error (a longer one is cut, ending with `/...`)."""

SCHEMA_ERROR = "/: schema: the output schema cannot be used for validation"
"""The error when the schema itself is unusable (a skill bug: the loader checks schemas)."""

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})


def _limit(value: Any) -> str | None:
    """A schema number for a description, or `None` when it is not a plain finite one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = repr(value)
    return text if len(text) <= 32 else None


def _types(value: Any) -> str | None:
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names or len(names) > len(_TYPES):
        return None
    if not all(isinstance(name, str) and name in _TYPES for name in names):
        return None
    return " or ".join(sorted(set(names)))


def _bounded(template: str, value: Any) -> str:
    """`template` with the schema number `value`, or a generic wording without it."""
    limit = _limit(value)
    if limit is None:
        return template.split(" {", 1)[0] + " the schema's limit"
    return template.format(limit)


def _describe(error: ValidationError) -> tuple[str, str]:
    """`(keyword, description)` of one failure, from the keyword and the schema only."""
    keyword: Any = error.validator  # `None` for a `false` schema, whatever the stubs say
    value = error.validator_value
    if keyword is None:  # a `false` schema
        return "false", "no value is allowed here"
    simple = {
        "additionalProperties": "the object has properties the schema does not declare",
        "unevaluatedProperties": "the object has properties the schema does not declare",
        "unevaluatedItems": "the array has items the schema does not declare",
        "enum": "the value is not one of the allowed values",
        "const": "the value is not the allowed constant",
        "uniqueItems": "the array items are not unique",
        "anyOf": "the value matches none of the allowed alternatives",
        "oneOf": "the value does not match exactly one alternative",
        "not": "the value matches a schema it must not match",
        "contains": "the array has too few items matching `contains`",
        "minContains": "the array has too few items matching `contains`",
        "maxContains": "the array has too many items matching `contains`",
        "dependentRequired": "a property that another property requires is missing",
        "items": "the array has items the schema does not allow",
    }
    bounded = {
        "maxLength": "the string is longer than {} characters",
        "minLength": "the string is shorter than {} characters",
        "maxItems": "the array has more than {} items",
        "minItems": "the array has fewer than {} items",
        "maxProperties": "the object has more than {} properties",
        "minProperties": "the object has fewer than {} properties",
        "maximum": "the number is above {}",
        "minimum": "the number is below {}",
        "exclusiveMaximum": "the number is not below {}",
        "exclusiveMinimum": "the number is not above {}",
        "multipleOf": "the number is not a multiple of {}",
    }
    if keyword in simple:
        return str(keyword), simple[str(keyword)]
    if keyword in bounded:
        return str(keyword), _bounded(bounded[str(keyword)], value)
    if keyword == "type":
        names = _types(value)
        return "type", "the value has the wrong type" if names is None else f"expected {names}"
    return "invalid", "the value does not match the schema"


def _declared(schema: Any) -> frozenset[str]:
    """Every property name the schema declares (`properties` and `required`, anywhere)."""
    names: set[str] = set()
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(key for key in properties if isinstance(key, str))
            required = node.get("required")
            if isinstance(required, list):
                names.update(key for key in required if isinstance(key, str))
            stack.extend(node.values())
    return frozenset(name for name in names if _NAME.fullmatch(name))


def _pointer(path: Any, declared: frozenset[str]) -> str:
    """A JSON Pointer to the failing value: array indexes and declared property names only
    (anything else is `*`); `/` is the root."""
    parts: list[str] = []
    for segment in path:
        if isinstance(segment, int) and not isinstance(segment, bool):
            parts.append(str(segment))
        elif isinstance(segment, str) and segment in declared:
            parts.append(segment)
        else:
            parts.append("*")
    pointer = "/" + "/".join(parts)
    if len(pointer) > MAX_POINTER_CHARS:
        pointer = pointer[: MAX_POINTER_CHARS - 4].rsplit("/", 1)[0] + "/..."
    return pointer


def _line(pointer: str, keyword: str, description: str) -> str:
    return f"{pointer}: {keyword}: {description}"[:MAX_ERROR_CHARS]


def _required(error: ValidationError, pointer: str, declared: frozenset[str]) -> Iterator[str]:
    """One error per missing required property, named only when the schema declares it."""
    instance, names = error.instance, error.validator_value
    if not isinstance(instance, dict) or not isinstance(names, list):
        yield _line(pointer, "required", "a required property is missing")
        return
    for name in names:
        if isinstance(name, str) and name not in instance:
            shown = name if name in declared else "*"
            yield _line(pointer, "required", f"missing required property {shown}")


def _intents(output: Any) -> Iterator[str]:
    """The reserved `intents` property, when present, is an array of objects (decision 38 D)."""
    if not isinstance(output, dict) or INTENTS_KEY not in output:
        return
    intents = output[INTENTS_KEY]
    if not isinstance(intents, list):
        yield _line(f"/{INTENTS_KEY}", "type", "the reserved intents property must be an array")
        return
    for index, intent in enumerate(intents[:MAX_SCANNED]):
        if not isinstance(intent, dict):
            yield _line(f"/{INTENTS_KEY}/{index}", "type", "an intent must be an object")


def _failures(output: Any, schema: Mapping[str, Any], declared: frozenset[str]) -> Iterator[str]:
    validator = Draft202012Validator(dict(schema))
    for error in itertools.islice(validator.iter_errors(output), MAX_SCANNED):
        pointer = _pointer(error.absolute_path, declared)
        if error.validator == "required":
            yield from _required(error, pointer, declared)
            continue
        keyword, description = _describe(error)
        yield _line(pointer, keyword, description)


def schema_errors(output: Any, schema: Mapping[str, Any]) -> list[str]:
    """The repair errors of `output` against `schema` (empty when it is valid): see the
    module docstring. Formats are annotations (draft 2020-12's default), as in every
    validator Horae ships with."""
    declared = _declared(schema)
    try:
        Draft202012Validator.check_schema(dict(schema))
        found = set(_failures(output, schema, declared))
    except (SchemaError, RecursionError, TypeError, ValueError):
        return [SCHEMA_ERROR]
    found.update(_intents(output))
    errors = sorted(found)
    if len(errors) > MAX_ERRORS:
        more = len(errors) - (MAX_ERRORS - 1)
        errors = [*errors[: MAX_ERRORS - 1], f"/: more: {more} more errors not shown"]
    return errors


def _cpu_limit(seconds: int) -> None:
    """A CPU-time ceiling for this process (a backstop: the parent kills it on its timeout)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    except (ImportError, ValueError, OSError):  # pragma: no cover (platform specific)
        return


def main(stdin: BinaryIO, stdout: BinaryIO, limit_cpu: Callable[[int], None] = _cpu_limit) -> int:
    """The worker: reads `{"output", "schema", "cpu_s"}` as JSON from `stdin`, sets the CPU
    ceiling of **this process** (`limit_cpu`: only ever in the child), and writes the errors
    as a JSON array to `stdout`. Exit status 0, or 2 for a malformed request."""
    try:
        request = json.loads(stdin.read())
        output, schema, cpu_s = request["output"], request["schema"], request["cpu_s"]
    except (ValueError, KeyError, TypeError):
        return 2
    if not isinstance(schema, dict) or not isinstance(cpu_s, int) or cpu_s < 1:
        return 2
    limit_cpu(cpu_s)
    stdout.write(json.dumps(schema_errors(output, schema)).encode("ascii"))
    stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover (run in the child process)
    sys.exit(main(sys.stdin.buffer, sys.stdout.buffer))
