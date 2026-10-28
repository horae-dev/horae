# SPDX-License-Identifier: Apache-2.0
"""Safe YAML parsing with positions, and pydantic errors mapped to file and line."""

import os
from pathlib import Path

import pytest
import yaml

from horae.config import Problem, ProblemsError, documents, parse_yaml, read_yaml, validate
from horae.contracts import InstanceConfig, Pipeline

F = Path("f.yaml")
FAKE_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"  # synthetic, not a real key


def _problems(text: str) -> list[Problem]:
    with pytest.raises(ProblemsError) as excinfo:
        parse_yaml(text, F)
    return excinfo.value.problems


def _invalid(text: str, model: type[InstanceConfig] | type[Pipeline]) -> list[str]:
    with pytest.raises(ProblemsError) as excinfo:
        validate(parse_yaml(text, F), model)
    return [str(p) for p in excinfo.value.problems]


def test_positions_point_at_keys_values_and_items() -> None:
    doc = parse_yaml("a: 1\nb:\n  - x\n  - {c: 2}\n", F)
    assert doc.data == {"a": 1, "b": ["x", {"c": 2}]}
    assert doc.position(("a",)) == documents.Position(1, 1)
    assert doc.position(("a",), value=True) == documents.Position(1, 4)
    assert doc.position(("b", 1)) == documents.Position(4, 5)
    assert doc.position(("b", 1, "c")) == documents.Position(4, 6)
    assert doc.position(("b", 1, "c"), value=True) == documents.Position(4, 9)
    assert doc.position(("b", 1, "missing")) == documents.Position(4, 5)
    assert doc.position(("b", 1, "missing"), value=True) == documents.Position(4, 5)
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
    assert problem.message == f"nested deeper than {documents.MAX_DEPTH} levels"
    # The root mapping plus 63 sequences is exactly MAX_DEPTH levels.
    depth = documents.MAX_DEPTH - 1
    assert parse_yaml("a: " + "[" * depth + "]" * depth, F).data is not None


@pytest.mark.parametrize(
    ("text", "column"),
    [("[" * 5000, documents.MAX_DEPTH + 1), ("{a: " * 5000, 4 * documents.MAX_DEPTH + 1)],
)
def test_very_deep_flow_nesting_is_a_precise_problem(text: str, column: int) -> None:
    # Without the check while composing, PyYAML recurses until RecursionError.
    [problem] = _problems(text)
    assert (problem.line, problem.column) == (1, column)
    assert problem.message == f"nested deeper than {documents.MAX_DEPTH} levels"


def test_recursion_error_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    def deep(self: object) -> None:
        raise RecursionError

    monkeypatch.setattr(yaml.SafeLoader, "get_single_node", deep)
    [problem] = _problems("a: 1")
    assert str(problem) == f"f.yaml: nested deeper than {documents.MAX_DEPTH} levels"


def test_duplicate_key_that_looks_like_a_value_is_not_echoed() -> None:
    [problem] = _problems(f"{FAKE_KEY}: 1\n{FAKE_KEY}: 2\n")
    assert problem.message == "duplicate key <key at line 2> (first defined at line 1)"


def test_read_yaml_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"a: 1\nb: \xff\n")
    with pytest.raises(ProblemsError, match=r"bad.yaml:2: not valid UTF-8") as excinfo:
        read_yaml(bad)
    assert excinfo.value.__context__ is None
    with pytest.raises(ProblemsError, match=r"absent.yaml: file not found"):
        read_yaml(tmp_path / "absent.yaml")
    with pytest.raises(ProblemsError, match="not a regular file"):
        read_yaml(tmp_path)
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (documents.MAX_FILE_BYTES + 1))
    with pytest.raises(ProblemsError, match="larger than"):
        read_yaml(big)
    exact = tmp_path / "exact.yaml"
    exact.write_bytes(b"#" * documents.MAX_FILE_BYTES)
    assert read_yaml(exact).data is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_read_yaml_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "horae.yaml"
    os.mkfifo(fifo)
    with pytest.raises(ProblemsError, match="not a regular file"):
        read_yaml(fifo)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_read_yaml_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(ProblemsError, match=r"cannot be read \(Permission denied\)"):
            read_yaml(path)
    finally:
        path.chmod(0o600)


def test_read_error_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("a: 1\n", encoding="utf-8")

    def fail(fd: int, size: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", fail)
    with pytest.raises(ProblemsError, match=r"cannot be read \(Input/output error\)"):
        read_yaml(path)


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
    text = "models:\n  x: {backend: ollama, model: m, base_url: 'http://o', colour: red}\n"
    assert _invalid(text, InstanceConfig) == ["f.yaml:2:56: models.x.colour: unknown key"]


def test_missing_key_points_at_the_parent_key() -> None:
    assert _invalid("models:\n  default:\n    backend: anthropic\n", InstanceConfig) == [
        "f.yaml:2:3: models.default.model: missing required key 'model'"
    ]


def test_wrong_value_points_at_the_value() -> None:
    assert _invalid("scheduler:\n  mode: cron\n", InstanceConfig) == [
        "f.yaml:2:9: scheduler.mode: Input should be 'builtin' or 'external'"
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("- a\n- b\n", "f.yaml:1:1: (document): expected a mapping"),
        ("scheduler: 3\n", "f.yaml:1:12: scheduler: expected a mapping"),
        ("models: [a]\n", "f.yaml:1:9: models: expected a mapping"),
        ("models:\n  x: claude\n", "f.yaml:2:6: models.x: expected a mapping"),
        ("timezone: 3\n", "f.yaml:1:11: timezone: expected a string"),
        ("schema_version: true\n", "f.yaml:1:17: schema_version: expected an integer"),
        ("schema_version: 2\n", "f.yaml:1:17: schema_version: Input should be 1"),
    ],
)
def test_type_errors_say_what_was_expected(text: str, problem: str) -> None:
    assert _invalid(text, InstanceConfig) == [problem]


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


_KINDS = "(expected one of: collect, transform, llm, act, notify)"


@pytest.mark.parametrize(
    ("step", "problem"),
    [
        ("  - colect: [a.b]\n", f"f.yaml:3:5: steps[0]: unknown step kind 'colect' {_KINDS}"),
        ("  - collect\n", "f.yaml:3:5: steps[0]: expected a mapping"),
        ("  - 42\n", "f.yaml:3:5: steps[0]: expected a mapping"),
        (
            "  - {}\n",
            "f.yaml:3:5: steps[0]: expected exactly one step kind: "
            "collect, transform, llm, act, notify",
        ),
        (
            f"  - {FAKE_KEY}: [a.b]\n",
            f"f.yaml:3:5: steps[0]: unknown step kind <key at line 3> {_KINDS}",
        ),
        (
            "  - collect:\n      - 42\n",
            "f.yaml:4:9: steps[0].collect[0]: expected a string or a mapping",
        ),
        (
            "  - collect: [Bad]\n",
            "f.yaml:3:15: steps[0].collect[0]: "
            "must be a plugin tool, <namespace>.<tool> (e.g. tasks.normalize)",
        ),
    ],
)
def test_step_union_gives_one_problem(step: str, problem: str) -> None:
    assert _invalid("name: p\nsteps:\n" + step, Pipeline) == [problem]


@pytest.mark.parametrize(
    ("ref", "problem"),
    [
        (
            "{ value: x }",
            "f.yaml:3:12: secrets.refs.A_KEY: unknown secret source 'value' "
            "(expected one of: env, file)",
        ),
        ("env", "f.yaml:3:12: secrets.refs.A_KEY: expected a mapping"),
        (
            "{ file: ../x }",
            "f.yaml:3:20: secrets.refs.A_KEY.file: "
            "must be a single file name of letters, digits, ., _ or -, not starting with .",
        ),
    ],
)
def test_secret_ref_union_gives_one_problem(ref: str, problem: str) -> None:
    assert _invalid(f"secrets:\n  refs:\n    A_KEY: {ref}\n", InstanceConfig) == [problem]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: P\nsteps: [{collect: [a.b]}]\n", "must match [a-z][a-z0-9-]*"),
        ("name: p\ntrigger: {cron: daily}\nsteps: [{collect: [a.b]}]\n", "5-field cron"),
        (
            "name: p\ntrigger: {cron: '* * * * *', timezone: 'a b'}\nsteps: [{collect: [a.b]}]\n",
            "must be an IANA time zone name",
        ),
        (
            "name: p\nsteps: [{llm: {skill: nope, model: m, budget: {usd: 1, tokens_out: 1}}}]\n",
            "must be a skill, <namespace>/<skill>",
        ),
    ],
)
def test_pipeline_patterns_are_explained(text: str, message: str) -> None:
    [problem] = _invalid(text, Pipeline)
    assert message in problem
    assert "^" not in problem


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            "models:\n  local: { backend: ollama, model: m, "
            "base_url: 'http://ollama.example.org' }\n",
            "f.yaml:2:49: models.local.base_url: must be an https URL, or an http URL to a "
            "loopback, private or single-label host, without credentials or fragment",
        ),
        (
            "models:\n  Bad Alias: { backend: claude-cli, model: m }\n",
            "f.yaml:2:3: models.<key at line 2>: must match [a-z][a-z0-9._-]*: a lowercase "
            "letter, then lowercase letters, digits, ., _ or -",
        ),
        (
            "secrets:\n  refs:\n    A: { env: 1X }\n",
            "f.yaml:3:15: secrets.refs.A.env: must be an environment variable name: letters, "
            "digits and _, not starting with a digit",
        ),
        (
            'state_dir: "a\\tb"\n',
            "f.yaml:1:12: state_dir: must be a path without control characters",
        ),
        (
            "models:\n  x: { backend: claude-cli, model: 'a b' }\n",
            "f.yaml:2:36: models.x.model: must be one word: no spaces or control characters",
        ),
    ],
)
def test_instance_patterns_are_explained(text: str, problem: str) -> None:
    assert _invalid(text, InstanceConfig) == [problem]


def test_no_message_shows_a_regular_expression() -> None:
    assert all("^" not in m and "$" not in m for m in documents.PATTERN_MESSAGES.values())


def test_unknown_pattern_has_a_generic_message() -> None:
    error = {"type": "string_pattern_mismatch", "ctx": {"pattern": "^x$"}, "msg": "raw ^x$"}
    assert documents._message(error) == "has an invalid format"


def test_keys_that_look_like_values_are_never_echoed() -> None:
    text = (
        f"models:\n  {FAKE_KEY}: {{ backend: nope, model: m }}\n"
        f"secrets:\n  refs:\n    {FAKE_KEY}: {{ env: X }}\n"
    )
    problems = _invalid(text, InstanceConfig)
    assert problems == [
        "f.yaml:2:3: models.<key at line 2>: must match [a-z][a-z0-9._-]*: a lowercase letter, "
        "then lowercase letters, digits, ., _ or -",
        "f.yaml:2:65: models.<key at line 2>.backend: Input should be 'claude-cli', "
        "'anthropic', 'openai-compatible' or 'ollama'",
        "f.yaml:5:5: secrets.refs.<key at line 5>: must match [A-Z][A-Z0-9_]*: an upper-case "
        "letter, then upper-case letters, digits or _",
    ]
    assert all("sk-ant" not in p for p in problems)


def test_unsafe_secret_name_key_is_not_echoed() -> None:
    assert _invalid("secrets:\n  refs:\n    LD_PRELOAD: { env: X }\n", InstanceConfig) == [
        "f.yaml:3:5: secrets.refs.<key at line 3>: reserved or unsafe as a secret name"
    ]


def test_unknown_key_that_looks_like_a_value_is_masked() -> None:
    assert _invalid(f"{FAKE_KEY}: 1\n", InstanceConfig) == [
        "f.yaml:1:1: <key at line 1>: unknown key"
    ]


def test_model_validator_error_points_at_the_document() -> None:
    text = (
        "name: p\nsteps:\n"
        "  - llm: {skill: x/y, model: m, budget: {usd: 1, tokens_out: 1}}\n"
        "  - llm: {skill: x/y, model: m, budget: {usd: 1, tokens_out: 1}}\n"
    )
    assert _invalid(text, Pipeline) == [
        "f.yaml:1:1: (document): a pipeline has at most one `llm` step"
    ]
