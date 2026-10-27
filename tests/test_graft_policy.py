# SPDX-License-Identifier: Apache-2.0
"""Validate graft.yaml against the vendored Graft schema (no network at test time).

Schema source: https://raw.githubusercontent.com/mmornati/graft/main/schema/graft.v1.schema.json
vendored from mmornati/graft commit 3f40dd101286bb95c4b356eaf10a9e163bf1903d.
To refresh, re-download it and update this SHA.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "tests" / "fixtures" / "graft.v1.schema.json"


def _policy() -> Any:
    return yaml.safe_load((ROOT / "graft.yaml").read_text())


def test_graft_yaml_matches_schema() -> None:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = [
        f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(_policy())
    ]
    assert not errors, errors


def test_graft_yaml_declares_sensitive_paths_and_rules() -> None:
    policy = _policy()
    assert policy["version"] == 1
    assert policy["sensitive_paths"]
    assert policy["rules"]
