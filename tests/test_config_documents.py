# SPDX-License-Identifier: Apache-2.0
"""Safe YAML parsing with positions, and pydantic errors mapped to file and line."""

from pathlib import Path

import pytest
import yaml

from horae.config import Problem, ProblemsError, documents, parse_yaml, read_yaml, validate
from horae.contracts import InstanceConfig, Pipeline

F = Path("f.yaml")


def _problems(text: str) -> list[Problem]:
    with pytest.raises(ProblemsError) as excinfo:
        parse_yaml(text, F)
    return excinfo.value.problems


def _invalid(text: str, model: type[InstanceConfig] | type[Pipeline]) -> list[str]:
    with pytest.raises(ProblemsError) as excinfo:
        validate(parse_yaml(text, F), model)
    return [str(p) for p in excinfo.value.problems]


def test_positions_point_at_keys_and_items() -> None:
    doc = parse_yaml("a: 1\nb:\n  - x\n  - {c: 2}\n", F)
    assert doc.data == {"a": 1, "b": ["x", {"c": 2}]}
    assert doc.position(("a",)) == documents.Position(1, 1)
    assert doc.position(("b", 1)) == documents.Position(4, 5)
    assert doc.position(("b", 1, "c")) == documents.Position(4, 6)
    assert doc.position(("b", 1, "missing")) == documents.Position(4, 5)
    assert doc.position(("nope",)) == documents.Position(1, 1)


def test_empty_document() -> None:
    doc = parse_yaml("# only a comment\n", F)
    assert doc.data is None
    assert validate(doc, InstanceConfig) == InstanceConfig()


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        ("a: 1\nb: 2\na: 3\n", 3, "duplicate key 'a' (first defined at line 1)"),
        ("a: &x [1]\nb: *x\n", 2, "anchors and aliases are not supported"),
        ("a: {x: 1}\nb:\n  <<: {y: 2}\n", 3, "merge keys (<<) are not supported"),
        ("? [a, b]\n: 1\n", 1, "mapping keys must be plain values"),
        ("a: !!python/object/apply:os.system [echo]\n", 1, "could not determine a constructor"),
        ("a: !custom 1\n", 1, "could not determine a constructor"),
        ("a: [1, 2\nb: 3\n", 2, "expected ',' or ']'"),
        ("a: 1\n---\nb: 2\n", 2, "expected a single document"),
    ],
)
def test_unsafe_or_broken_yaml_is_rejected_with_its_line(
    text: str, line: int, message: str
) -> None:
    [problem] = _problems(text)
    assert problem.file == "f.yaml"
    assert problem.line == line
    assert message in problem.message


def test_deep_nesting_is_rejected() -> None:
    [problem] = _problems("a: " + "[" * (documents.MAX_DEPTH + 2) + "]" * (documents.MAX_DEPTH + 2))
    assert "nested deeper" in problem.message


def test_read_yaml_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"a: 1\nb: \xff\n")
    with pytest.raises(ProblemsError, match=r"bad.yaml:2: not valid UTF-8"):
        read_yaml(bad)
    with pytest.raises(ProblemsError, match="cannot be read"):
        read_yaml(tmp_path / "absent.yaml")
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (documents.MAX_FILE_BYTES + 1))
    with pytest.raises(ProblemsError, match="larger than"):
        read_yaml(big)


def test_problem_rendering() -> None:
    assert str(Problem("x.yaml", 3, 5, "boom")) == "x.yaml:3:5: boom"
    assert str(Problem("x.yaml", 3, None, "boom")) == "x.yaml:3: boom"
    assert str(Problem("x.yaml", None, None, "boom")) == "x.yaml: boom"
    assert Problem("x.yaml", 3, 5, "boom").to_dict() == {
        "file": "x.yaml",
        "line": 3,
        "column": 5,
        "severity": "error",
        "message": "boom",
    }


def test_marked_error_without_mark() -> None:
    class Unmarked(yaml.MarkedYAMLError):
        pass

    def boom(self: object) -> None:
        raise Unmarked(problem="odd")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yaml.SafeLoader, "get_single_node", boom)
        assert [str(p) for p in _problems("a: 1")] == ["f.yaml: odd"]

    def plain(self: object) -> None:
        raise yaml.YAMLError("plain")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yaml.SafeLoader, "get_single_node", plain)
        assert [str(p) for p in _problems("a: 1")] == ["f.yaml: invalid YAML: plain"]


# --- validation errors -----------------------------------------------------------------------


def test_unknown_key_points_at_the_key() -> None:
    assert _invalid(
        "timezone: UTC\nmodels:\n  x: {backend: ollama, model: m, colour: red}\n", InstanceConfig
    ) == ["f.yaml:3:34: models.x.colour: Extra inputs are not permitted"]


def test_missing_key_points_at_the_parent() -> None:
    assert _invalid("models:\n  default:\n    backend: anthropic\n", InstanceConfig) == [
        "f.yaml:2:3: models.default.model: missing required key 'model'"
    ]


def test_wrong_type_points_at_the_value() -> None:
    assert _invalid("scheduler:\n  mode: cron\n", InstanceConfig) == [
        "f.yaml:2:3: scheduler.mode: Input should be 'builtin' or 'external'"
    ]


def test_not_a_mapping() -> None:
    assert _invalid("- a\n- b\n", InstanceConfig) == [
        "f.yaml:1:1: (document): Input should be a valid dictionary or instance of InstanceConfig"
    ]


def test_union_reports_only_the_member_that_matches() -> None:
    text = (
        "name: p\n"
        "steps:\n"
        "  - collect: [a.b]\n"
        "  - llm:\n"
        "      skill: x/y\n"
        "      model: default\n"
        "      budget: { usd: 0.05 }\n"
    )
    assert _invalid(text, Pipeline) == [
        "f.yaml:7:7: steps[1].llm.budget.tokens_out: missing required key 'tokens_out'"
    ]


def test_secret_ref_union() -> None:
    assert _invalid("secrets:\n  refs:\n    A_KEY: { file: ../x }\n", InstanceConfig) == [
        "f.yaml:3:14: secrets.refs.A_KEY.file: String should match pattern "
        "'^[A-Za-z0-9_-][A-Za-z0-9._-]*$'"
    ]


def test_bad_map_key_points_at_the_key() -> None:
    problems = _invalid("models:\n  Bad Alias: { backend: ollama, model: m }\n", InstanceConfig)
    assert problems == [
        "f.yaml:2:3: models.Bad Alias: String should match pattern '^[a-z][a-z0-9._-]*$'"
    ]


def test_plugin_call_union() -> None:
    problems = _invalid("name: p\nsteps:\n  - collect:\n      - 42\n", Pipeline)
    assert problems
    assert all(p.startswith("f.yaml:4:9: steps[0].collect[0]") for p in problems)


def test_model_validator_error_points_at_the_document() -> None:
    text = (
        "name: p\nsteps:\n"
        "  - llm: {skill: x/y, model: m, budget: {usd: 1, tokens_out: 1}}\n"
        "  - llm: {skill: x/y, model: m, budget: {usd: 1, tokens_out: 1}}\n"
    )
    problems = _invalid(text, Pipeline)
    assert any("at most one `llm` step" in p for p in problems)
