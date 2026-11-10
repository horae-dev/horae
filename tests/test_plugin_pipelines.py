# SPDX-License-Identifier: Apache-2.0
"""`horae plugin add` installs a pack's pipelines (#133, schemas decision 71), over local
fixture repositories (no network)."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.config import InstanceError, load_instance, loader
from horae.contracts.lockfile import LockedPlugin
from horae.contracts.plugin import PluginManifest
from horae.marketplace import approval
from horae.marketplace.approval import lock_mismatch
from horae.marketplace.errors import InstallRefused
from horae.marketplace.install import INSTALL_RECORD
from horae.marketplace.pipelines import PackPipelines
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


def _enable(instance: Path) -> None:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("plugins:\n", "plugins:\n  demo-pack: {}\n", 1), "utf-8")


def _disable(instance: Path) -> None:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("  demo-pack: {}\n", ""), encoding="utf-8")


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
    # Not enabled yet: the loader skips them (point 3).
    assert set(load_instance(inst).pipelines) == {"remind", "daily-brief"}
    _enable(inst)
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


def test_the_same_name_in_two_places_is_a_loader_problem(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review"]), {k: v for k, v in GOOD.items() if "weekly" in k})
    assert _add(inst)[0] == 0
    _enable(inst)
    mine = inst / "pipelines" / "weekly-review.yaml"
    mine.write_text(PIPELINE.format(name="weekly-review"), encoding="utf-8")
    with pytest.raises(InstanceError, match="defined twice"):
        load_instance(inst)


# --- review: the approval shows the pipelines, the lock pins them, only an enabled pack runs --


def test_the_approval_prompt_lists_each_pipeline_and_warns_about_other_plugins(
    inst: Path, pack: dict[str, Any]
) -> None:
    files = {
        **GOOD,
        "pipelines/nightly-check.yaml": GOOD["pipelines/nightly-check.yaml"].replace(
            "- collect: [demo.fetch]", "- collect: [demo-pack.own, github.review_requests]"
        ),
    }
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), files)
    code, out = _add(inst)
    assert code == 0, out
    assert "weekly-review: scheduled, cron */10 * * * *" in out
    assert "tools: demo-pack.own, demo.alert, github.review_requests" in out
    assert "warning: calls plugins other than demo-pack (demo, github)" in out
    assert "warning: calls plugins other than demo-pack (demo);" in out  # weekly-review
    assert out.index("weekly-review:") < out.index("Approve these permissions")


def test_a_pipeline_without_a_trigger_and_with_a_skill_is_described(
    inst: Path, pack: dict[str, Any]
) -> None:
    text = (
        "name: weekly-review\nsteps:\n"
        "  - llm:\n      skill: demo-pack/word\n      model: default\n"
        "      budget: { usd: 0.01, tokens_out: 100 }\n"
    )
    pack["publish"](_manifest(["weekly-review"]), {"pipelines/weekly-review.yaml": text})
    code, out = _add(inst)
    assert code == 0, out
    assert "weekly-review: manual (horae run) or external trigger only" in out
    assert "skills: demo-pack/word" in out
    assert "warning" not in out


def test_the_lock_records_a_digest_per_pipeline(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    assert _add(inst)[0] == 0
    lock = yaml.safe_load((inst / "horae.lock").read_text(encoding="utf-8"))
    expected = {
        name: hashlib.sha256(GOOD[f"pipelines/{name}.yaml"].encode()).hexdigest()
        for name in ("weekly-review", "nightly-check")
    }
    assert lock["plugins"]["demo-pack"]["pipelines"] == dict(sorted(expected.items()))
    assert "pipelines" not in lock["plugins"]["github"]


def test_yes_refuses_changed_pipelines() -> None:
    base = {
        "version": "1.0.0",
        "repo": "https://example.org/x/plugin-demo",
        "commit": "a" * 40,
        "permissions": {},
        "pipelines": {"weekly-review": "b" * 64},
    }
    old, same = LockedPlugin.model_validate(base), LockedPlugin.model_validate(base)
    assert lock_mismatch(old, same) is None
    for changed in (
        {"weekly-review": "c" * 64},
        {"weekly-review": "b" * 64, "extra": "d" * 64},
        {},
    ):
        new = LockedPlugin.model_validate({**base, "pipelines": changed})
        assert "different pipelines" in (lock_mismatch(old, new) or "")


def test_yes_reinstalls_the_same_pipelines(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    assert _add(inst)[0] == 0
    import shutil

    shutil.rmtree(inst / "plugins" / "demo-pack")
    shutil.rmtree(inst / "pipelines" / "demo-pack")
    code, out = _add(inst, "--yes")
    assert code == 0, out


def test_a_pack_runs_only_when_enabled_and_locked(inst: Path, pack: dict[str, Any]) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    assert _add(inst)[0] == 0
    assert "weekly-review" not in load_instance(inst).pipelines  # installed, not enabled
    _enable(inst)
    assert {"weekly-review", "nightly-check"} <= set(load_instance(inst).pipelines)
    _disable(inst)  # disabling deactivates them
    assert "weekly-review" not in load_instance(inst).pipelines


def test_a_pack_directory_without_a_lock_entry_is_not_loaded(
    inst: Path, pack: dict[str, Any]
) -> None:
    directory = inst / "pipelines" / "ghost-pack"
    directory.mkdir()
    (directory / "ghost.yaml").write_text(PIPELINE.format(name="ghost"), encoding="utf-8")
    config = inst / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    # Not listed in horae.lock: even listed in plugins:, nothing of it may load.
    assert "ghost" not in load_instance(inst).pipelines
    config.write_text(text.replace("plugins:\n", "plugins:\n  ghost-pack: {}\n", 1), "utf-8")
    with pytest.raises(InstanceError, match="not installed"):
        load_instance(inst)


def test_a_pipeline_edited_after_approval_is_refused_by_the_loader(
    inst: Path, pack: dict[str, Any]
) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    assert _add(inst)[0] == 0
    _enable(inst)
    edited = inst / "pipelines" / "demo-pack" / "weekly-review.yaml"
    edited.write_text(edited.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    with pytest.raises(InstanceError, match="approves"):
        load_instance(inst)


def test_a_file_swapped_after_the_hash_check_is_not_loaded(
    inst: Path, pack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pack["publish"](_manifest(["weekly-review", "nightly-check"]), GOOD)
    assert _add(inst)[0] == 0
    _enable(inst)
    target = inst / "pipelines" / "demo-pack" / "weekly-review.yaml"
    real = loader.sha256_bytes
    swapped = PIPELINE.format(name="weekly-review").replace("*/10", "*/1")

    def swap_after_check(raw: bytes) -> str:
        digest = real(raw)
        if b"name: weekly-review" in raw:
            target.write_text(swapped, encoding="utf-8")  # replaced once it has been hashed
        return digest

    monkeypatch.setattr(loader, "sha256_bytes", swap_after_check)
    pipeline = load_instance(inst).pipelines["weekly-review"]
    assert pipeline.trigger is not None
    assert pipeline.trigger.cron == "*/10 * * * *"  # the verified bytes, not the swapped file
    assert target.read_text(encoding="utf-8") == swapped


# --- review fixes: structure not names, params in the prompt, old locks ---------------------


def test_a_plugin_named_pipelines_is_refused(inst: Path, tmp_path: Path) -> None:
    data = json.loads(_manifest(["weekly-review"]))
    data["name"] = "pipelines"
    tree = tmp_path / "tree"
    (tree / "pipelines").mkdir(parents=True)
    (tree / "pipelines" / "weekly-review.yaml").write_text(
        PIPELINE.format(name="weekly-review"), encoding="utf-8"
    )
    with pytest.raises(InstallRefused, match="reserved plugin name"):
        PackPipelines(inst)(tree, PluginManifest.model_validate(data))
    assert not (inst / "pipelines" / "pipelines").exists()


def test_a_directory_named_pipelines_gets_every_gate(inst: Path) -> None:
    sneaky = inst / "pipelines" / "pipelines"
    sneaky.mkdir()
    (sneaky / "ghost.yaml").write_text(PIPELINE.format(name="ghost"), encoding="utf-8")
    assert "ghost" not in load_instance(inst).pipelines  # not enabled, not locked: skipped
    config = inst / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("plugins:\n", "plugins:\n  pipelines: {}\n", 1), "utf-8")
    with pytest.raises(InstanceError, match="not installed"):
        load_instance(inst)


def test_the_prompt_shows_call_parameters_model_and_budget(
    inst: Path, pack: dict[str, Any]
) -> None:
    text = (
        "name: weekly-review\n"
        "steps:\n"
        "  - llm:\n      skill: demo-pack/word\n      model: cloud\n"
        "      budget: { usd: 0.05, tokens_out: 900 }\n"
        "  - act:\n"
        "      - demo.write: { path: 'Daily/{{date}}.md', mode: replace-owned }\n"
        "  - notify:\n"
        "      - demo.push: { topic: alerts, link_prefix: 'https://example.org/' }\n"
    )
    pack["publish"](_manifest(["weekly-review"]), {"pipelines/weekly-review.yaml": text})
    code, out = _add(inst)
    assert code == 0, out
    assert "llm demo-pack/word model=cloud budget=$0.05/900 tokens out" in out
    assert '"mode": "replace-owned"' in out
    assert '"path": "Daily/{{date}}.md"' in out
    assert '"topic": "alerts"' in out
    assert '"link_prefix": "https://example.org/"' in out


def test_a_directory_whose_lock_entry_approves_no_pipeline_is_ignored(
    inst: Path, caplog: pytest.LogCaptureFixture
) -> None:
    old = inst / "pipelines" / "github"
    old.mkdir()
    (old / "stray.yaml").write_text(PIPELINE.format(name="stray"), encoding="utf-8")
    with caplog.at_level("WARNING", logger="horae.config"):
        loaded = load_instance(inst)  # `github` is enabled and locked, with no pipelines
    assert "stray" not in loaded.pipelines
    assert any("approves no pipeline" in r.message for r in caplog.records)


@pytest.mark.parametrize("native", [True, False], ids=["renameat2", "mkdir-fallback"])
def test_a_directory_appearing_before_the_placement_is_refused_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    from horae.marketplace import pipelines as mod

    if not native:
        monkeypatch.setattr(mod, "_renameat2", lambda: None)
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    hook = PackPipelines(tmp_path)
    hook.plugin, hook.files = "demo-pack", {"weekly-review": b"name: weekly-review\n"}
    hook.target = pipelines / "demo-pack"
    real = tempfile.mkdtemp

    def racing(*args: Any, **kwargs: Any) -> str:
        scratch: str = real(*args, **kwargs)
        (pipelines / "demo-pack").mkdir()  # appears after the check, before the placement
        return scratch

    monkeypatch.setattr(tempfile, "mkdtemp", racing)
    with pytest.raises(InstallRefused, match="appeared"):
        hook.write()
    assert (pipelines / "demo-pack").is_dir()
    assert list((pipelines / "demo-pack").iterdir()) == []  # the intruder is untouched
    assert [p.name for p in pipelines.iterdir()] == ["demo-pack"]  # no scratch left
    assert hook.written is None


@pytest.mark.parametrize("native", [True, False], ids=["renameat2", "mkdir-fallback"])
def test_rename_noreplace_moves_a_directory_to_a_free_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    from horae.marketplace import pipelines as mod

    if not native:
        monkeypatch.setattr(mod, "_renameat2", lambda: None)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.yaml").write_text("x", encoding="utf-8")
    mod.rename_noreplace(tmp_path / "a", tmp_path / "b")
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "b" / "x.yaml").read_text(encoding="utf-8") == "x"


def test_a_fallback_move_that_fails_partway_puts_every_file_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.marketplace import pipelines as mod

    monkeypatch.setattr(mod, "_renameat2", lambda: None)
    src = tmp_path / "scratch"
    src.mkdir()
    for name in ("a.yaml", "b.yaml", "c.yaml"):
        (src / name).write_text(name, encoding="utf-8")
    bystander = tmp_path / "keep.txt"
    bystander.write_text("mine", encoding="utf-8")
    real = os.rename
    calls: list[str] = []

    def flaky(a: Any, b: Any, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        calls.append(a)
        if len(calls) == 3:  # the third move fails; the later ones (put back) go through
            raise OSError(5, "I/O error")
        real(a, b, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(OSError, match="I/O error"):
        mod.rename_noreplace(src, tmp_path / "target")
    assert sorted(p.name for p in src.iterdir()) == ["a.yaml", "b.yaml", "c.yaml"]
    assert (src / "a.yaml").read_text(encoding="utf-8") == "a.yaml"
    assert not (tmp_path / "target").exists()
    assert bystander.read_text(encoding="utf-8") == "mine"
