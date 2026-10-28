# SPDX-License-Identifier: Apache-2.0
"""Canonical JSON and the pipeline digest (`RunRecord.pipeline_digest`).

**Canonical JSON** (`canonical_json`): UTF-8, object keys sorted, no insignificant whitespace
(`separators=(",", ":")`), non-ASCII characters written as themselves (`ensure_ascii=False`),
and `NaN`/`Infinity` rejected. Numbers use Python's shortest round-trip `repr`. This is close to
RFC 8785 (JCS) but not identical: JCS sorts keys by UTF-16 code units (Python sorts by code
points; they differ only for characters outside the BMP) and formats numbers like ECMAScript
(`1e-07` here, `1e-7` in JCS; a float `1.0` stays `1.0`). Both differences are stable, which is
all a digest needs.

Numbers keep their JSON type: the integer `1` and the float `1.0` are written differently and
so give different digests, as do `0.0` and `-0.0`. A pipeline that changes `days: 2` to
`days: 2.0` is therefore a different pipeline (a plugin may well treat them differently).

**This form is frozen as canonical JSON v1.** Stored digests (`RunRecord.pipeline_digest`,
`input_hash`) depend on it byte for byte; any change to it needs a new version, never an edit.

**Canonical pipeline** (`canonical_pipeline`): the validated `Pipeline` dumped in JSON mode,
with

- the `trigger` left out: the digest is **what runs**, not when it runs, so moving a pipeline
  to another schedule (or to an external trigger) keeps its digest,
- defaults filled in (`on_unchanged_input: skip` written or omitted gives the same digest),
- fields that are `None` left out (a new optional field does not change existing digests),
- every plugin call in the mapping form: a bare `tasks.normalize` becomes
  `{"tasks.normalize": {}}`, so both spellings of the same call digest the same.

Comments, key order and YAML formatting therefore never change the digest; any change to what
runs does. `pipeline_digest` is the SHA-256 of the canonical JSON of the canonical pipeline,
as 64 lowercase hex characters.
"""

import hashlib
import json
from typing import Any

from horae.contracts.pipeline import Pipeline

_CALL_KINDS = ("collect", "transform", "act", "notify")


def canonical_json(value: Any) -> bytes:
    """`value` (plain JSON data) as canonical JSON bytes."""
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode()


def sha256_hex(value: Any) -> str:
    """SHA-256 of `canonical_json(value)`, as lowercase hex."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_pipeline(pipeline: Pipeline) -> dict[str, Any]:
    """The canonical form of `pipeline` (see the module docstring)."""
    dumped: dict[str, Any] = pipeline.model_dump(
        mode="json", exclude_none=True, exclude={"trigger"}
    )
    for step in dumped["steps"]:
        for kind in _CALL_KINDS:
            if kind in step:
                step[kind] = [{call: {}} if isinstance(call, str) else call for call in step[kind]]
    return dumped


def pipeline_digest(pipeline: Pipeline) -> str:
    """`RunRecord.pipeline_digest`: SHA-256 of the canonical pipeline."""
    return sha256_hex(canonical_pipeline(pipeline))
