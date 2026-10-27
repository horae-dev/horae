# SPDX-License-Identifier: Apache-2.0
"""Generate the JSON Schemas under ``schemas/`` from the pydantic models.

The models are the source of truth. Regenerate after changing them::

    uv run python -m horae.contracts.export            # write schemas/
    uv run python -m horae.contracts.export --check    # exit 1 if schemas/ is stale
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from horae.contracts.intents import INTENT_MODELS, Intent
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import Pipeline
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import RunRecord

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://horae.dev/schemas"
"""Base of every ``$id``. An identifier only: nothing is served there (yet)."""


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
        body = TypeAdapter(self.target).json_schema(mode="validation")
        body["title"] = self.title
        return {"$schema": DIALECT, "$id": self.id, **body}

    def render(self) -> str:
        return json.dumps(self.schema(), indent=2, ensure_ascii=False) + "\n"


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


def generate() -> dict[Path, str]:
    """Every schema file, by path relative to ``schemas/``."""
    return {spec.path: spec.render() for spec in SCHEMAS}


def stale(out_dir: Path) -> list[Path]:
    """Files under ``out_dir`` that are missing, differ from the models, or are orphaned."""
    expected = generate()
    problems = [p for p, text in expected.items() if _read(out_dir / p) != text]
    orphans = {p.relative_to(out_dir) for p in out_dir.glob("**/v*.json")} - expected.keys()
    return sorted([*problems, *orphans])


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=Path("schemas"), help="default: schemas")
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    args = parser.parse_args(argv)
    out: Path = args.out
    if args.check:
        bad = stale(out)
        for path in bad:
            print(f"stale: {out / path}")
        if bad:
            print("run: uv run python -m horae.contracts.export")
        return 1 if bad else 0
    for path, text in generate().items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
