# SPDX-License-Identifier: Apache-2.0
"""The JSON Schemas in schemas/ match the pydantic models and accept the documented examples."""

import copy
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from pydantic import TypeAdapter
from pydantic import ValidationError as ModelError

from horae.contracts import (
    INTENT_TYPES,
    Hostname,
    HttpsUrl,
    Lockfile,
    Pipeline,
    PluginManifest,
    RunRecord,
    url_host,
)
from horae.contracts import export as export_module
from horae.contracts.export import BASE_ID, SCHEMAS, SchemaSpec, generate, main, stale
from horae.contracts.types import _unique

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
EXAMPLES = SCHEMA_DIR / "examples"
SPECS = {spec.name: spec for spec in SCHEMAS}


def _ecma_pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[ValidationError]:
    """`pattern` with ECMA-262 semantics: `$` matches only at the very end of the string.

    Python's `re` (used by `jsonschema`) also matches `$` before a trailing newline."""
    if validator.is_type(instance, "string") and not re.search(
        re.sub(r"(?<!\\)\$", r"\\Z", pattern), instance
    ):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


EcmaValidator = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator, {"pattern": _ecma_pattern}
)


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _schema(spec: SchemaSpec) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((SCHEMA_DIR / spec.path).read_text(encoding="utf-8"))
    return schema


def _schema_errors(spec: SchemaSpec, data: Any) -> list[str]:
    validator = EcmaValidator(_schema(spec), format_checker=FormatChecker())
    return [e.message for e in validator.iter_errors(data)]


def _adapter(spec: SchemaSpec) -> TypeAdapter[Any]:
    return TypeAdapter(spec.target)


def _model_results(spec: SchemaSpec, data: Any) -> tuple[bool, bool]:
    """Whether `validate_python(data)` and `validate_json(json.dumps(data))` accept `data`."""
    adapter = _adapter(spec)
    validators_: list[Callable[[], Any]] = [
        lambda: adapter.validate_python(data),
        lambda: adapter.validate_json(json.dumps(data)),
    ]
    results = []
    for validate in validators_:
        try:
            validate()
        except ModelError:
            results.append(False)
        else:
            results.append(True)
    return results[0], results[1]


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
    (tmp_path / "old" / "schema.json").write_text("{}")
    (tmp_path / "examples" / "x" / "valid").mkdir(parents=True)
    (tmp_path / "examples" / "x" / "valid" / "a.json").write_text("{}")
    assert stale(tmp_path) == [Path("old/schema.json")]


def test_export_writes_lf_bytes(tmp_path: Path) -> None:
    main(["--out", str(tmp_path)])
    data = (tmp_path / "pipeline" / "v1.json").read_bytes()
    assert b"\r\n" not in data
    assert data.endswith(b"}\n")
    crlf = data.replace(b"\n", b"\r\n")
    (tmp_path / "pipeline" / "v1.json").write_bytes(crlf)
    assert stale(tmp_path) == [Path("pipeline/v1.json")]


def test_export_default_out_is_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert export_module.default_out() == SCHEMA_DIR
    monkeypatch.chdir(tmp_path)
    assert main(["--check"]) == 0
    monkeypatch.setattr(export_module, "default_out", lambda: None)
    with pytest.raises(SystemExit):
        main(["--check"])


def test_gitattributes_pins_lf() -> None:
    assert "schemas/** text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


# --- the schemas themselves -----------------------------------------------------------------


@pytest.mark.parametrize("spec", SCHEMAS, ids=lambda s: s.name)
def test_schema_is_valid_and_versioned(spec: SchemaSpec) -> None:
    schema = _schema(spec)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{BASE_ID}/{spec.name}/v{spec.version}.json"
    assert re.fullmatch(
        r"https://raw\.githubusercontent\.com/horae-dev/horae/main/schemas/[a-z._/-]+/v[0-9]+\.json",
        schema["$id"],
    )


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("spec", SCHEMAS, ids=lambda s: s.name)
def test_schema_uses_portable_keywords(spec: SchemaSpec) -> None:
    for key, value in _walk(_schema(spec)):
        assert key != "discriminator", "OpenAPI keyword in a JSON Schema"
        if key == "pattern":
            assert not re.search(r"\\[sSdDwW]", value), f"shorthand class in {value!r}"
            assert "(?" not in value, f"look-around or flags in {value!r}"


def test_one_schema_per_intent() -> None:
    assert {f"intents/{t}" for t in INTENT_TYPES} <= SPECS.keys()
    union = _schema(SPECS["intent"])
    tags = {
        union["$defs"][ref["$ref"].split("/")[-1]]["properties"]["type"]["const"]
        for ref in union["oneOf"]
    }
    assert tags == set(INTENT_TYPES)


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
    data = _load(path)
    assert _schema_errors(spec, data) == []
    assert _model_results(spec, data) == (True, True)
    if path.suffix == ".json":
        _adapter(spec).validate_json(path.read_bytes())


@pytest.mark.parametrize(("name", "path"), _examples("invalid"))
def test_invalid_example(name: str, path: Path) -> None:
    spec = SPECS[name]
    data = _load(path)
    assert _schema_errors(spec, data), "the JSON Schema accepted an invalid example"
    assert _model_results(spec, data) == (False, False)


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


# --- rules checked by both, on single-field mutations ---------------------------------------

Mutation = Callable[[Any], None]


def _set(*keys: str | int, value: Any) -> Mutation:
    def mutate(doc: Any) -> None:
        node = doc
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value

    return mutate


BASES = {
    "pipeline": "pipeline/valid/daily-brief.yaml",
    "plugin-manifest": "plugin-manifest/valid/github.yaml",
    "remote": "plugin-manifest/valid/remote-coach.yaml",
    "intents/note.create": "intents/note.create/valid/daily-note.json",
    "intents/task.create": "intents/task.create/valid/with-due-time.json",
    "intents/notify.push": "intents/notify.push/valid/digest.json",
    "intents/http.post": "intents/http.post/valid/webhook.json",
    "lockfile": "lockfile/valid/horae.lock.yaml",
    "run-record": "run-record/valid/ok.json",
}

LLM = ("steps", 2, "llm")
GH = ("plugins", "github")

REJECTED: list[tuple[str, Mutation]] = [
    # no type coercion, no bool for int
    ("pipeline", _set(*LLM, "budget", "tokens_out", value="2000")),
    ("pipeline", _set(*LLM, "budget", "tokens_out", value=True)),
    ("pipeline", _set(*LLM, "budget", "usd", value=True)),
    ("pipeline", _set(*LLM, "budget", "usd", value="0.05")),
    ("pipeline", _set(*LLM, "model", value=1)),
    ("lockfile", _set("schema_version", value=True)),
    ("run-record", _set("schema_version", value=True)),
    ("run-record", _set("dry_run", value=1)),
    ("run-record", _set("llm", 0, "cached", value="false")),
    ("run-record", _set("changes", 0, "intent", value=False)),
    # cron
    ("pipeline", _set("trigger", "cron", value="30\t5 * * *")),
    ("pipeline", _set("trigger", "cron", value=" 30 5 * * *")),
    ("pipeline", _set("trigger", "cron", value="30 5 * * *\n")),
    ("pipeline", _set("trigger", "timezone", value="Europe/../Paris")),
    # one LLM step
    ("pipeline", lambda d: d["steps"].append(copy.deepcopy(d["steps"][2]))),
    # permissions
    ("plugin-manifest", _set("permissions", "network", value=["api.github.com", "api.github.com"])),
    ("plugin-manifest", _set("permissions", "network", value=["10.0.0.1"])),
    ("plugin-manifest", _set("permissions", "network", value=["2130706433"])),
    ("plugin-manifest", _set("permissions", "network", value=["[::1]"])),
    ("plugin-manifest", _set("permissions", "network", value=["api.localhost"])),
    ("plugin-manifest", _set("permissions", "network", value=["API.github.com"])),
    ("plugin-manifest", _set("permissions", "network", value=["*.github.com"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["lower_case"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["_PRIVATE"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["HORAE"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["DYLD_INSERT_LIBRARIES"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["PYTHONPATH"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["PATH"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["HOME"])),
    ("plugin-manifest", _set("permissions", "paths", value={"exec": ["/bin"]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault/"]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault//x"]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault/./x"]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault/.."]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault\x00"]})),
    ("plugin-manifest", _set("permissions", "network", value=[f"h{i}" for i in range(257)])),
    ("plugin-manifest", _set("homepage", value="http://example.org")),
    ("plugin-manifest", _set("description", value="two\nlines")),
    ("plugin-manifest", _set("license", value="(no)")),
    ("plugin-manifest", _set("horae", value=">=0.1,\t<0.2")),
    # https URLs
    ("remote", _set("runtime", "url", value="https://coach.example.org:0/mcp")),
    ("remote", _set("runtime", "url", value="https://coach.example.org:65536/mcp")),
    ("remote", _set("runtime", "url", value="https://Coach.example.org/mcp")),
    ("intents/http.post", _set("url", value="https://hooks.example.org/a b")),
    ("intents/http.post", _set("url", value="https://hooks.example.org/\x7f")),
    ("intents/http.post", _set("url", value="https://x.localhost/")),
    ("intents/http.post", _set("url", value="HTTPS://hooks.example.org/")),
    # notes and tasks
    ("intents/note.create", _set("path", value="")),
    ("intents/note.create", _set("path", value="Daily//x.md")),
    ("intents/note.create", _set("path", value="Daily/")),
    ("intents/note.create", _set("path", value="./x.md")),
    ("intents/note.create", _set("path", value="Daily/.hidden.md")),
    ("intents/note.create", _set("path", value="Daily/x\x01.md")),
    ("intents/note.create", _set("path", value="Daily/x.md\n")),
    ("intents/note.create", _set("path", value="a" * 256)),
    ("intents/note.create", _set("path", value="/".join(["a" * 200] * 6))),
    ("intents/note.create", _set("content", value="x" * (1024 * 1024 + 1))),
    ("intents/task.create", _set("title", value="")),
    ("intents/task.create", _set("title", value="x" * 501)),
    ("intents/task.create", _set("title", value="tab\tinside")),
    ("intents/task.create", _set("fingerprint", value="has space")),
    ("intents/task.create", _set("fingerprint", value="x" * 129)),
    ("intents/task.create", _set("due", value="2026-10-28T00:00:00Z")),
    ("intents/task.create", _set("due", value="20261028")),
    ("intents/task.create", _set("due", value=20261028)),
    ("intents/task.create", _set("due", value="2026-02-30")),
    ("intents/notify.push", _set("body", value="x" * 4097)),
    ("intents/notify.push", _set("link", value="obsidian://")),
    ("intents/notify.push", _set("link", value="data:text/html,x")),
    # lockfile
    ("lockfile", _set(*GH, "repo", value="https://github.com/horae-dev/plugin-github?x=1")),
    ("lockfile", _set(*GH, "repo", value="https://github.com:22/horae-dev/plugin-github")),
    ("lockfile", _set(*GH, "repo", value="https://user@github.com/horae-dev/plugin-github")),
    ("lockfile", _set(*GH, "repo", value="https://github.com/")),
    ("lockfile", _set(*GH, "repo", value="https://github.com/./x")),
    ("lockfile", _set(*GH, "repo", value="https://localhost/x")),
    ("lockfile", _set(*GH, "repo", value="https://github.com/" + "a" * 500)),
    ("lockfile", lambda d: d["plugins"].update({"Bad Name": d["plugins"]["github"]})),
    # run record
    ("run-record", _set("id", value="2026-10-27T05:30:00Z-daily-brief-01")),
    ("run-record", _set("pipeline_digest", value="SHA256:abc")),
    ("run-record", _set("context", "now", value="2026-10-27T06:30:00")),
    ("run-record", _set("context", "now", value="2026-10-27 06:30:00Z")),
    ("run-record", _set("context", "now", value="2026-10-27T06:30Z")),
    ("run-record", _set("context", "now", value="2026-10-27t06:30:00z")),
    ("run-record", _set("context", "now", value=1761543000)),
    ("run-record", _set("context", "timezone", value="Europe Paris")),
    ("run-record", _set("context", "vars", value={"bad-name": "x"})),
    ("run-record", _set("steps", 0, "status", value="partial")),
    ("run-record", _set("steps", 0, "stderr", value="x" * 65537)),
    ("run-record", _set("steps", 0, "duration_ms", value=-1)),
    ("run-record", _set("violations", value=[{"kind": "other", "detail": "x"}])),
    ("run-record", _set("llm", 0, "attempt", value=0)),
    ("run-record", _set("plugins", "Github", value={"version": "0.1.0"})),
]


ACCEPTED: list[tuple[str, Mutation]] = [
    ("pipeline", lambda d: d.pop("trigger")),
    ("pipeline", _set("trigger", "timezone", value="America/Argentina/Buenos_Aires")),
    ("pipeline", _set("trigger", "cron", value="0 7 * JAN-MAR MON-FRI")),
    ("pipeline", _set(*LLM, "budget", "usd", value=1)),
    ("plugin-manifest", _set("permissions", "network", value=["coach", "x1.example"])),
    ("plugin-manifest", _set("permissions", "secrets", value=["MY_HOME", "PATHS_TOKEN"])),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/home/me/.config"]})),
    ("plugin-manifest", _set("permissions", "paths", value={"read": ["/vault/My Notes"]})),
    ("intents/http.post", _set("url", value="https://hooks.example.org")),
    ("intents/http.post", _set("url", value="https://hooks.example.org:65535?x=1")),
    ("intents/http.post", _set("url", value="https://hooks.example.org/a@b/%20")),
    ("intents/note.create", _set("path", value="Projects/Q4 plan (draft).md")),
    ("intents/note.create", _set("path", value="Daily/x..md")),
    ("intents/note.create", _set("content", value="")),
    ("intents/task.create", _set("title", value="Pay the 📅 bill")),
    ("lockfile", _set(*GH, "repo", value="https://git.example.org/a/b/c")),
    ("run-record", _set("context", "now", value="2026-10-27T06:30:00.123456-03:00")),
    ("run-record", lambda d: d.pop("lock_digest")),
]


def _cases(cases: list[tuple[str, Mutation]]) -> list[Any]:
    return [pytest.param(base, mutate, id=f"{base}-{i}") for i, (base, mutate) in enumerate(cases)]


def _mutated(base: str, mutate: Mutation) -> tuple[SchemaSpec, Any]:
    path = BASES[base]
    doc = _load(EXAMPLES / path)
    mutate(doc)
    return SPECS[path.rsplit("/", 2)[0]], doc


@pytest.mark.parametrize(("base", "mutate"), _cases(REJECTED))
def test_rejected_by_both(base: str, mutate: Mutation) -> None:
    spec, doc = _mutated(base, mutate)
    assert _schema_errors(spec, doc), "the JSON Schema accepted it"
    assert _model_results(spec, doc) == (False, False)


@pytest.mark.parametrize(("base", "mutate"), _cases(ACCEPTED))
def test_accepted_by_both(base: str, mutate: Mutation) -> None:
    spec, doc = _mutated(base, mutate)
    assert _schema_errors(spec, doc) == []
    assert _model_results(spec, doc) == (True, True)


# --- documented differences between the models and the JSON Schema --------------------------


@pytest.mark.parametrize(
    ("base", "mutate"),
    [
        pytest.param("run-record", _set("changes", 0, "intent", value=1), id="intent-index"),
        pytest.param(
            "run-record", _set("finished_at", value="2026-10-27T05:29:59Z"), id="finished-early"
        ),
        pytest.param(
            "run-record",
            _set("steps", 0, "finished_at", value="2026-10-27T05:29:00Z"),
            id="step-finished-early",
        ),
        pytest.param(
            "remote", _set("runtime", "url", value="https://other.example.org/mcp"), id="host"
        ),
    ],
)
def test_rules_only_the_models_check(base: str, mutate: Mutation) -> None:
    spec, doc = _mutated(base, mutate)
    assert _schema_errors(spec, doc) == []
    assert _model_results(spec, doc) == (False, False)


def test_integral_float_for_int_only_the_models_reject() -> None:
    spec, doc = _mutated("pipeline", _set(*LLM, "budget", "tokens_out", value=2000.0))
    assert _schema_errors(spec, doc) == []  # JSON Schema `integer` accepts 2000.0
    assert _model_results(spec, doc) == (False, False)


def test_python_jsonschema_trailing_newline_quirk() -> None:
    spec, doc = _mutated("intents/task.create", _set("title", value="Buy milk\n"))
    assert _schema_errors(spec, doc)
    plain = Draft202012Validator(_schema(spec))
    assert plain.is_valid(doc)  # Python `re`: `$` matches before a final newline
    assert _model_results(spec, doc) == (False, False)


def test_formats_need_a_format_checker() -> None:
    doc = _load(EXAMPLES / "run-record/invalid/bad-datetime.json")
    schema = _schema(SPECS["run-record"])
    assert EcmaValidator(schema).is_valid(doc)
    assert not EcmaValidator(schema, format_checker=FormatChecker()).is_valid(doc)


# --- serialization and helpers ---------------------------------------------------------------


def test_lockfile_serializes_canonically() -> None:
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                name: {
                    "version": "0.1.0",
                    "repo": f"https://github.com/horae-dev/plugin-{name}",
                    "commit": "3f2c9e1a7b4d5c6e8f9012345678abcdef012345",
                    "permissions": {
                        "network": ["b.example.org", "a.example.org"],
                        "secrets": ["Z_TOKEN", "A_TOKEN"],
                        "paths": {"read": ["/z", "/a"]},
                        "intents": ["task.create", "note.create"],
                    },
                }
                for name in ("zeta", "alpha")
            },
        }
    )
    data = lock.model_dump(mode="json")
    assert list(data["plugins"]) == ["alpha", "zeta"]
    perms = data["plugins"]["alpha"]["permissions"]
    assert perms["network"] == ["a.example.org", "b.example.org"]
    assert perms["secrets"] == ["A_TOKEN", "Z_TOKEN"]
    assert perms["paths"] == {"read": ["/a", "/z"], "write": []}
    assert perms["intents"] == ["note.create", "task.create"]
    assert lock.plugins["zeta"].permissions.network == ["b.example.org", "a.example.org"]
    assert json.loads(lock.model_dump_json()) == data


def test_unique_handles_hashable_and_unhashable_items() -> None:
    assert _unique(["a", "b"]) == ["a", "b"]
    with pytest.raises(ValueError, match="duplicate"):
        _unique(["a", "a"])
    assert _unique([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]
    with pytest.raises(ValueError, match="duplicate"):
        _unique([{"a": 1}, {"a": 1}])


def test_url_host() -> None:
    assert url_host("https://coach.example.org:8443/mcp?x") == "coach.example.org"
    with pytest.raises(ValueError, match="https"):
        url_host("http://coach.example.org/")


_HOSTNAME: TypeAdapter[str] = TypeAdapter(Hostname)
_HTTPS_URL: TypeAdapter[str] = TypeAdapter(HttpsUrl)


@pytest.mark.parametrize(
    "host",
    [
        "xn--bcher-kva.example",  # bücher
        "xn--nxasmq6b.example",  # greek
        "xn--55qx5d.example",  # CJK
        "api.xn--bcher-kva.example",
    ],
)
def test_valid_a_labels_are_accepted(host: str) -> None:
    assert _HOSTNAME.validate_python(host) == host
    assert _HTTPS_URL.validate_python(f"https://{host}:8443/x") == f"https://{host}:8443/x"


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("xn--a.example", id="not-punycode"),
        pytest.param("xn--ls8h.example", id="emoji-idna2003-only"),
        pytest.param("xn--zz.example", id="incomplete"),
        pytest.param("xn--g6h.example", id="symbol-idna2003-only"),
        pytest.param("a.xn--a", id="last-label"),
        pytest.param("xn--zca.example", id="does-not-round-trip"),  # ß, mapped to ss,
    ],
)
def test_invalid_a_labels_are_rejected(host: str) -> None:
    with pytest.raises(ModelError, match="IDNA"):
        _HOSTNAME.validate_python(host)
    with pytest.raises(ModelError, match="IDNA"):
        _HTTPS_URL.validate_python(f"https://{host}/")


def test_python_date_objects_are_accepted() -> None:
    import datetime

    record = _load(EXAMPLES / "run-record/valid/ok.json")
    record["started_at"] = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
    RunRecord.model_validate(record)
    record["started_at"] = datetime.date(2026, 10, 27)
    with pytest.raises(ModelError):
        RunRecord.model_validate(record)
