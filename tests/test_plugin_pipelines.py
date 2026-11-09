# SPDX-License-Identifier: Apache-2.0
"""`horae plugin add` installs a pack's pipelines (#133, schemas decision 71), over local
fixture repositories (no network)."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.config import load_instance
from horae.marketplace import approval
from horae.marketplace.install import INSTALL_RECORD
from marketplace_fixtures import (
    PLUGIN_URL,
    A,
    LocalClient,
    LocalInstaller,
    entry,
    git,
    plugin_manifest,
    plugin_repo,
    publish,
)

runner = CliRunner()

PIPELINE = """\
# A synthetic pipeline.
name: {name}
trigger: {{ cron: "*/10 * * * *" }}
steps:
  - collect: [demo.fetch]
  - notify:
      - demo.alert: {{}}
"""


def _manifest(pipelines: list[str]) -> str:
    data = json.loads(plugin_manifest("demo-pack"))
    data.update(provides={"pipelines": pipelines}, permissions={})
    del data["runtime"]
    return json.dumps(data)


GOOD = {f"pipelines/{n}.yaml": PIPELINE.format(name=n) for n in ("weekly-review", "nightly-check")}


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a repository chosen by the test: `pack["publish"](manifest, files)`."""
    state: dict[str, Any] = {}

    def make(manifest: str, files: dict[str, str], *, links: dict[str, str] | None = None) -> None:
        root = tmp_path / "plugin-demo-pack"
        _, commit = plugin_repo(root, name="demo-pack", manifest=manifest, files=files)
        for path, target in (links or {}).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).symlink_to(target)
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "link")
            commit = git(root, "rev-parse", "HEAD")
            git(root, "tag", "-f", "v1.0.0")
        index = publish(
            tmp_path / "index",
            [entry("demo-pack", {"1.0.0": commit}, repo=PLUGIN_URL, tier="first-party")],
        )
        monkeypatch.setattr(
            cli_plugin,
            "make_client",
            lambda cache_dir, urls: LocalClient(cache_dir, urls, {A: index}),
        )
        monkeypatch.setattr(
            cli_plugin, "make_installer", lambda where: LocalInstaller(where, {PLUGIN_URL: root})
        )

    state["publish"] = make
    monkeypatch.setattr(cli_plugin, "interactive", lambda: True)
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({A}))
    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: None)
    return state


@pytest.fixture
def inst(instance_dir: Path) -> Path:
    config = instance_dir / "horae.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"\nmarketplaces: [{A}]\n", encoding="utf-8"
    )
    return instance_dir


def _add(instance: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app, ["plugin", "add", "demo-pack", *args, "-i", str(instance)], input="y\n"
    )
    return result.exit_code, result.stdout


def _nothing_left(instance: Path) -> None:
    plugins = instance / "plugins"
    assert not plugins.exists() or not list(plugins.iterdir())
    names = {p.name for p in (instance / "pipelines").iterdir()}
    assert names == {"daily-brief.yaml", "remind.yaml"}  # the fixture's own


def _refused(inst: Path, match: str) -> None:
    code, out = _add(inst, "--json")
    assert code == 3, out
    error = json.loads(out[out.index("{") :])["error"]
    assert error["kind"] == "install_refused"
    assert match in error["message"], error["message"]
    _nothing_left(inst)


def test_a_packs_pipelines_are_installed_in_its_own_directory(
    inst: Path, pack: dict[str, Any]
) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    code, out = _add(inst, "--json")
    assert code == 0, out
    report = json.loads(out[out.index("{") :])
    expected = ["pipelines/demo-pack/nightly-check.yaml", "pipelines/demo-pack/weekly-review.yaml"]
    assert report["pipelines"] == expected
    directory = inst / "pipelines" / "demo-pack"
    assert sorted(p.name for p in directory.iterdir()) == [
        "nightly-check.yaml",
        "weekly-review.yaml",
    ]
    assert (directory / "weekly-review.yaml").read_text(encoding="utf-8") == GOOD[
        "pipelines/weekly-review.yaml"
    ]
    record = json.loads((inst / "plugins" / "demo-pack" / INSTALL_RECORD).read_text("utf-8"))
    assert record["pipelines"] == expected
    # The instance loader reads them, next to its own.
    loaded = load_instance(inst)
    assert {"weekly-review", "nightly-check", "remind", "daily-brief"} == set(loaded.pipelines)
    assert loaded.pipeline_files["weekly-review"] == directory / "weekly-review.yaml"
    assert not [p for p in (inst / "pipelines").iterdir() if p.name.startswith(".")]


def test_the_text_output_lists_the_pipelines(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    code, out = _add(inst)
    assert code == 0, out
    assert "pipelines/demo-pack/weekly-review.yaml" in out


def test_a_plugin_without_pipelines_installs_none(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](plugin_manifest("demo-pack"), {})
    code, out = _add(inst, "--json")
    assert code == 0, out
    assert json.loads(out[out.index("{") :])["pipelines"] == []
    assert not (inst / "pipelines" / "demo-pack").exists()


def test_one_invalid_pipeline_fails_the_whole_install(inst: Path, pack: dict[str, Any]) -> None:
    files = {**GOOD, "pipelines/nightly-check.yaml": "name: nightly-check\nsteps: nope\n"}
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), files)
    _refused(inst, "pipelines/nightly-check.yaml is not a valid pipeline")


def test_a_pipeline_named_differently_from_its_file_is_refused(
    inst: Path, pack: dict[str, Any]
) -> None:
    files = {**GOOD, "pipelines/nightly-check.yaml": PIPELINE.format(name="other")}
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), files)
    _refused(inst, "defines pipeline 'other'")


def test_the_files_must_be_the_declared_pipelines(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review"]), GOOD)
    _refused(inst, "must be the same")


def test_a_declared_pipeline_without_a_file_is_refused(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](
        _manifest(["weekly-review", "nightly-check"]),
        {"pipelines/weekly-review.yaml": GOOD["pipelines/weekly-review.yaml"]},
    )
    _refused(inst, "must be the same")


@pytest.mark.parametrize(
    "path", ["pipelines/sub/weekly-review.yaml", "pipelines/weekly-review.yml"]
)
def test_only_plain_yaml_files_are_accepted(inst: Path, pack: dict[str, Any], path: str) -> None:
    pack["publish"](_manifest(["weekly-review"]), {path: PIPELINE.format(name="weekly-review")})
    code, out = _add(inst, "--json")
    assert code == 3, out
    _nothing_left(inst)


def test_a_symlinked_pipeline_is_refused(inst: Path, pack: dict[str, Any], tmp_path: Path) -> None:
    pack["publish"](
        _manifest(["weekly-review"]),
        {"notes.txt": "x"},
        links={"pipelines/weekly-review.yaml": "../notes.txt"},
    )
    _refused(inst, "symlink")


def test_a_symlink_pointing_outside_is_refused(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](
        _manifest(["weekly-review"]), {}, links={"pipelines/weekly-review.yaml": "../../etc/hosts"}
    )
    code, out = _add(inst, "--json")
    assert code == 3, out
    _nothing_left(inst)


def test_an_existing_pack_directory_is_never_overwritten(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    mine = inst / "pipelines" / "demo-pack"
    mine.mkdir()
    mine_text = PIPELINE.format(name="weekly-review").replace("*/10", "*/20")
    (mine / "weekly-review.yaml").write_text(mine_text, encoding="utf-8")
    code, out = _add(inst, "--json")
    assert code == 3, out
    assert "refusing to overwrite" in out
    assert (mine / "weekly-review.yaml").read_text(encoding="utf-8") == mine_text
    assert not (inst / "plugins" / "demo-pack").exists()


def test_a_pipeline_the_user_already_has_is_never_shadowed(
    inst: Path, pack: dict[str, Any]
) -> None:
    mine = inst / "pipelines" / "weekly-review.yaml"
    mine.write_text(PIPELINE.format(name="weekly-review").replace("*/10", "*/20"), "utf-8")
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    code, out = _add(inst, "--json")
    assert code == 3, out
    assert "already defines pipeline(s) weekly-review" in out
    assert "*/20" in mine.read_text(encoding="utf-8")
    assert not (inst / "plugins" / "demo-pack").exists()
    assert not (inst / "pipelines" / "demo-pack").exists()


def test_a_failed_lock_write_removes_the_pipelines(
    inst: Path, pack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    real = os.replace

    def fail(src: Any, dst: Any) -> None:
        if Path(dst).name == "horae.lock":
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr("horae.marketplace.lock.os.replace", fail)
    code, out = _add(inst, "--json")
    assert code == 1, out
    _nothing_left(inst)


def test_declining_the_permissions_installs_no_pipeline(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    result = runner.invoke(app, ["plugin", "add", "demo-pack", "-i", str(inst)], input="n\n")
    assert result.exit_code == 3
    _nothing_left(inst)


def test_the_loader_ignores_dot_directories_and_symlinked_directories(
    instance_dir: Path, tmp_path: Path
) -> None:
    pipelines = instance_dir / "pipelines"
    (pipelines / ".install-x").mkdir()
    (pipelines / ".install-x" / "ghost.yaml").write_text("name: ghost\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "other.yaml").write_text(PIPELINE.format(name="other"), encoding="utf-8")
    (pipelines / "linked").symlink_to(outside)
    assert set(load_instance(instance_dir).pipelines) == {"daily-brief", "remind"}


def test_the_same_name_in_two_places_is_a_loader_problem(instance_dir: Path) -> None:
    pack_dir = instance_dir / "pipelines" / "some-pack"
    pack_dir.mkdir()
    (pack_dir / "remind.yaml").write_text(PIPELINE.format(name="remind"), encoding="utf-8")
    from horae.config import InstanceError

    with pytest.raises(InstanceError, match="defined twice"):
        load_instance(instance_dir)
