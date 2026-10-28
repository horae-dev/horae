# SPDX-License-Identifier: Apache-2.0
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ANTHROPIC_KEY = "sentinel-value-for-anthropic"  # synthetic, never a real key
GITHUB_TOKEN = "sentinel-value-for-github"  # noqa: S105 (synthetic)


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
    """The instance with every secret it needs: one in a file, one in the environment."""
    secrets = instance_dir / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(ANTHROPIC_KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    return instance_dir
