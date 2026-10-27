# SPDX-License-Identifier: Apache-2.0
"""The JSON Schemas in schemas/ match the pydantic models and accept the documented examples."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from horae.contracts import INTENT_TYPES, Pipeline, PluginManifest
from horae.contracts.export import BASE_ID, SCHEMAS, SchemaSpec, generate, main, stale

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
EXAMPLES = SCHEMA_DIR / "examples"
SPECS = {spec.name: spec for spec in SCHEMAS}


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _schema_errors(spec: SchemaSpec, data: Any) -> list[str]:
    schema = json.loads((SCHEMA_DIR / spec.path).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [e.message for e in validator.iter_errors(data)]


def _model_validate(spec: SchemaSpec, path: Path) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(spec.target)
    if path.suffix == ".json":
        adapter.validate_json(path.read_bytes())
    else:
        adapter.validate_python(_load(path))


def _examples(kind: str) -> list[Any]:
    return [
        pytest.param(name, path, id=f"{name}/{path.name}")
        for name in SPECS
        for path in sorted((EXAMPLES / name / kind).glob("*"))
    ]


# --- drift between models and committed schemas ------------------------------------------


def test_committed_schemas_match_models() -> None:
    assert stale(SCHEMA_DIR) == [], "run: uv run python -m horae.contracts.export"


def test_export_writes_and_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path), "--check"]) == 1
    assert "stale:" in capsys.readouterr().out

    assert main(["--out", str(tmp_path)]) == 0
    assert main(["--out", str(tmp_path), "--check"]) == 0
    assert {p.relative_to(tmp_path) for p in tmp_path.glob("**/*.json")} == set(generate())

    pipeline = tmp_path / "pipeline" / "v1.json"
    pipeline.write_text(pipeline.read_text().replace("Horae pipeline", "drifted"))
    assert main(["--out", str(tmp_path), "--check"]) == 1
    assert "pipeline/v1.json" in capsys.readouterr().out

    main(["--out", str(tmp_path)])
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "v1.json").write_text("{}")
    assert stale(tmp_path) == [Path("old/v1.json")]


# --- the schemas themselves -----------------------------------------------------------------


@pytest.mark.parametrize("spec", SCHEMAS, ids=lambda s: s.name)
def test_schema_is_valid_and_versioned(spec: SchemaSpec) -> None:
    schema = json.loads((SCHEMA_DIR / spec.path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{BASE_ID}/{spec.name}/v{spec.version}.json"
    assert re.fullmatch(r"https://horae\.dev/schemas/[a-z._/-]+/v[0-9]+\.json", schema["$id"])


def test_one_schema_per_intent() -> None:
    assert {f"intents/{t}" for t in INTENT_TYPES} <= SPECS.keys()
    union = json.loads((SCHEMA_DIR / "intent" / "v1.json").read_text(encoding="utf-8"))
    assert set(union["discriminator"]["mapping"]) == set(INTENT_TYPES)


@pytest.mark.parametrize("name", SPECS)
def test_every_schema_has_valid_and_invalid_examples(name: str) -> None:
    assert list((EXAMPLES / name / "valid").glob("*")), f"no valid example for {name}"
    assert list((EXAMPLES / name / "invalid").glob("*")), f"no invalid example for {name}"


def test_example_dirs_match_schemas() -> None:
    dirs = {p.parent.relative_to(EXAMPLES).as_posix() for p in EXAMPLES.glob("**/valid")}
    assert dirs == SPECS.keys()


# --- examples: the JSON Schema and the models agree --------------------------------------


@pytest.mark.parametrize(("name", "path"), _examples("valid"))
def test_valid_example(name: str, path: Path) -> None:
    spec = SPECS[name]
    assert _schema_errors(spec, _load(path)) == []
    _model_validate(spec, path)


@pytest.mark.parametrize(("name", "path"), _examples("invalid"))
def test_invalid_example(name: str, path: Path) -> None:
    spec = SPECS[name]
    assert _schema_errors(spec, _load(path)), "the JSON Schema accepted an invalid example"
    with pytest.raises(ValidationError):
        _model_validate(spec, path)


# --- the PLAN.md examples are kept verbatim ------------------------------------------------


def _plan_yaml_blocks() -> list[str]:
    plan = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    return re.findall(r"^```yaml\n(.*?)^```$", plan, flags=re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize(
    ("index", "example"),
    [(0, "pipeline/valid/daily-brief.yaml"), (1, "plugin-manifest/valid/github.yaml")],
)
def test_plan_examples_are_verbatim(index: int, example: str) -> None:
    assert (EXAMPLES / example).read_text(encoding="utf-8") == _plan_yaml_blocks()[index]


def test_plan_examples_parse_to_models() -> None:
    pipeline = Pipeline.model_validate(_load(EXAMPLES / "pipeline/valid/daily-brief.yaml"))
    assert [next(iter(step.model_fields_set)) for step in pipeline.steps] == [
        "collect",
        "transform",
        "llm",
        "act",
        "notify",
    ]
    manifest = PluginManifest.model_validate(_load(EXAMPLES / "plugin-manifest/valid/github.yaml"))
    assert manifest.permissions.secrets == ["GITHUB_TOKEN"]
    assert manifest.permissions.intents == []


# --- a few rules the examples do not cover --------------------------------------------------


@pytest.mark.parametrize(
    "permissions",
    [
        {"network": ["api.github.com", "api.github.com"]},
        {"secrets": ["lower_case"]},
        {"paths": {"exec": ["/bin"]}},
    ],
)
def test_bad_permissions_rejected_by_both(permissions: dict[str, Any]) -> None:
    doc = _load(EXAMPLES / "plugin-manifest/valid/github.yaml")
    doc["permissions"] = permissions
    assert _schema_errors(SPECS["plugin-manifest"], doc)
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(doc)


def test_no_type_coercion() -> None:
    doc = _load(EXAMPLES / "pipeline/valid/daily-brief.yaml")
    doc["steps"][2]["llm"]["budget"]["tokens_out"] = "2000"
    assert _schema_errors(SPECS["pipeline"], doc)
    with pytest.raises(ValidationError):
        Pipeline.model_validate(doc)
