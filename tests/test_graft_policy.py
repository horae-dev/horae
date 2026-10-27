# SPDX-License-Identifier: Apache-2.0
"""Validate graft.yaml against the vendored Graft schema (no network at test time).

Schema source: https://raw.githubusercontent.com/mmornati/graft/main/schema/graft.v1.schema.json
vendored from mmornati/graft commit 3f40dd101286bb95c4b356eaf10a9e163bf1903d
(see tests/fixtures/README.md). To refresh, re-download it and update this SHA.

The schema uses ``\\p{Cc}`` in patterns, which Python's ``re`` rejects, so the ``pattern``
keyword is evaluated with the ``regex`` package instead.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import regex
import yaml
from jsonschema import Draft202012Validator, ValidationError, validators

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "tests" / "fixtures" / "graft.v1.schema.json"


def _pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not regex.search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


PolicyValidator = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator, {"pattern": _pattern}
)


def _policy() -> Any:
    return yaml.safe_load((ROOT / "graft.yaml").read_text())


def _errors(policy: Any) -> list[str]:
    validator = PolicyValidator(json.loads(SCHEMA.read_text()))
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(policy)]


def test_graft_yaml_matches_schema() -> None:
    errors = _errors(_policy())
    assert not errors, errors


def test_invalid_policy_fails_schema() -> None:
    policy = _policy()
    del policy["version"]
    assert _errors(policy)


def test_invalid_pattern_fails_schema() -> None:
    policy = _policy()
    policy["rules"][0]["name"] = "Not Valid!"
    assert _errors(policy)


def test_graft_yaml_declares_sensitive_paths_and_rules() -> None:
    policy = _policy()
    assert policy["version"] == 1
    assert policy["sensitive_paths"]
    assert policy["rules"]


def test_rule_names_are_unique() -> None:
    names = [rule["name"] for rule in _policy()["rules"]]
    assert len(names) == len(set(names))


def test_auto_approve_rules_do_not_match_on_author() -> None:
    for rule in _policy()["rules"]:
        if rule.get("approve") == "auto":
            assert "author" not in rule["match"], rule["name"]
