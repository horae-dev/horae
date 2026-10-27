# SPDX-License-Identifier: Apache-2.0
"""Generate the JSON Schemas under ``schemas/`` from the pydantic models.

The models are the source of truth. Regenerate after changing them::

    uv run python -m horae.contracts.export            # write schemas/
    uv run python -m horae.contracts.export --check    # exit 1 if schemas/ is stale

The output depends on the pydantic version in ``uv.lock``: a pydantic upgrade may change the
bytes, and the drift test then asks for a regeneration.
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import CoreSchema

from horae.contracts.intents import INTENT_MODELS, Intent
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import Pipeline
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import RunRecord

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://raw.githubusercontent.com/horae-dev/horae/main/schemas"
"""Base of every ``$id``: the raw file on the default branch of this repository."""

EXAMPLES = "examples"
"""Directory under ``schemas/`` that holds examples, not generated schemas."""


def _postprocess(node: Any) -> Any:
    """Make the pydantic output plain, self-contained draft 2020-12.

    - Drop the OpenAPI ``discriminator`` keyword; ``oneOf`` with ``const`` tags remains.
    - A map whose keys have a pattern (``patternProperties``) rejects other keys, as the
      models do: pydantic leaves ``additionalProperties`` open there.
    """
    if isinstance(node, dict):
        out = {
            key: _postprocess(value)
            for key, value in node.items()
            if not (key == "discriminator" and "oneOf" in node)
        }
        if "patternProperties" in out and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_postprocess(item) for item in node]
    return node


class HoraeJsonSchema(GenerateJsonSchema):
    """Plain draft 2020-12 output: no OpenAPI-only keywords."""

    def generate(self, schema: CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        result: JsonSchemaValue = _postprocess(super().generate(schema, mode=mode))
        return result


@dataclass(frozen=True)
class SchemaSpec:
    """One published schema."""

    name: str
    """Path under ``schemas/`` and ``$id``, e.g. ``pipeline`` or ``intents/note.create``."""
    version: int
    target: Any
    """A model class or any type pydantic can build a ``TypeAdapter`` for."""
    title: str

    @property
    def id(self) -> str:
        return f"{BASE_ID}/{self.name}/v{self.version}.json"

    @property
    def path(self) -> Path:
        return Path(self.name) / f"v{self.version}.json"

    def schema(self) -> dict[str, Any]:
        body = TypeAdapter(self.target).json_schema(
            mode="validation", schema_generator=HoraeJsonSchema
        )
        body["title"] = self.title
        return {"$schema": DIALECT, "$id": self.id, **body}

    def render(self) -> bytes:
        return (json.dumps(self.schema(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


SCHEMAS: tuple[SchemaSpec, ...] = (
    SchemaSpec("pipeline", 1, Pipeline, "Horae pipeline"),
    SchemaSpec("plugin-manifest", 1, PluginManifest, "Horae plugin manifest (plugin.yaml)"),
    SchemaSpec("intent", 1, Intent, "Horae intent (any)"),
    *(
        SchemaSpec(f"intents/{name}", 1, model, f"Horae intent {name}")
        for name, model in INTENT_MODELS.items()
    ),
    SchemaSpec("lockfile", 1, Lockfile, "Horae lockfile (horae.lock)"),
    SchemaSpec("run-record", 1, RunRecord, "Horae run record"),
)


def generate() -> dict[Path, bytes]:
    """Every schema file, by path relative to ``schemas/``."""
    return {spec.path: spec.render() for spec in SCHEMAS}


def stale(out_dir: Path) -> list[Path]:
    """Files under ``out_dir`` that are missing, differ from the models, or are orphaned.

    Every ``*.json`` file outside ``examples/`` is expected to be a generated schema."""
    expected = generate()
    problems = [p for p, data in expected.items() if _read(out_dir / p) != data]
    present = {
        p.relative_to(out_dir)
        for p in out_dir.rglob("*.json")
        if p.relative_to(out_dir).parts[0] != EXAMPLES
    }
    return sorted([*problems, *(present - expected.keys())])


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def default_out() -> Path | None:
    """``schemas/`` of the source checkout this module lives in, if any (not the CWD)."""
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "schemas").is_dir():
        return root / "schemas"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: schemas/ at the repository root"
    )
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    args = parser.parse_args(argv)
    out: Path | None = args.out or default_out()
    if out is None:
        parser.error("--out is required outside a source checkout")
    if args.check:
        bad = stale(out)
        for path in bad:
            print(f"stale: {out / path}")
        if bad:
            print("run: uv run python -m horae.contracts.export")
        return 1 if bad else 0
    for path, data in generate().items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
