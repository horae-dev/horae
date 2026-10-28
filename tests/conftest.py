# SPDX-License-Identifier: Apache-2.0
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from horae.secrets import redact

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ANTHROPIC_KEY = "sentinel-value-for-anthropic"  # synthetic, never a real key
GITHUB_TOKEN = "sentinel-value-for-github"  # noqa: S105 (synthetic)


@pytest.fixture(autouse=True)
def _fresh_secret_registry() -> Iterator[None]:
    """Each test starts with an empty process registry of resolved secrets (#53): a process
    never forgets a secret, but one test's secrets must not redact another test's data."""
    redact.forget_all()
    yield
    redact.forget_all()


@pytest.fixture
def secret_values() -> tuple[str, str]:
    """The synthetic secret values `with_secrets` provides, to assert they never leak."""
    return ANTHROPIC_KEY, GITHUB_TOKEN


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    """A copy of `tests/fixtures/instance` that a test may change."""
    root = tmp_path / "instance"
    shutil.copytree(FIXTURES / "instance", root)
    return root


@pytest.fixture
def with_secrets(instance_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The instance with every secret it needs, as files in its secrets directory: one through
    an explicit `{file: ...}` reference, one found by its name (`secrets/GITHUB_TOKEN`).

    `GITHUB_TOKEN` is also set in the environment, to a different value, to prove that a
    secret without an explicit `{env: ...}` reference is never read from there."""
    secrets = instance_dir / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(ANTHROPIC_KEY + "\n", encoding="utf-8")
    (secrets / "GITHUB_TOKEN").write_text(GITHUB_TOKEN, encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-environment-never-read")
    return instance_dir
