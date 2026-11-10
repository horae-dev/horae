# SPDX-License-Identifier: Apache-2.0
"""`horae plugin remove` and `horae plugin audit` (#47, schemas decision 73), and the upgrade
of a pipeline pack, over local fixture repositories (no network)."""

import copy
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae import cli_plugin_manage
from horae.contracts.lockfile import Lockfile
from horae.marketplace import lock as lock_module
from horae.marketplace.pipelines import delete_own, digest
from horae.store import Store
from upgrade_fixtures import (
    Market,
    add,
    as_json,
    cli,
    hidden,
    inst,  # noqa: F401
    lock,
    manifest,
    market,  # noqa: F401
    snapshot,
)

PIPELINE = """\
# A synthetic pipeline.
name: {name}
trigger: {{ cron: "*/10 * * * *" }}
steps:
  - collect: [demo.fetch]
"""


def _pack(version: str, names: list[str]) -> str:
    data = json.loads(manifest(version))
    data["provides"]["pipelines"] = names
    return json.dumps(data)


def _pipelines(where: Market, files: dict[str, str]) -> dict[str, str]:
    """`pipelines/*.yaml` of the next release: exactly `files` (others are deleted)."""
    shutil.rmtree(where.root / "pipelines", ignore_errors=True)
    return {f"pipelines/{name}.yaml": text for name, text in files.items()}


def _enable(instance: Path, name: str = "demo") -> None:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(f"{text}plugins:\n  {name}: {{}}\n", encoding="utf-8")


@pytest.fixture
def pack(inst: Path, market: Market) -> Path:  # noqa: F811
    """`demo` 1.0.0 as a pack with pipelines `alpha` and `beta`, installed."""
    files = _pipelines(market, {n: PIPELINE.format(name=n) for n in ("alpha", "beta")})
    market.release("1.0.0", _pack("1.0.0", ["alpha", "beta"]), files)
    add(inst)
    return inst / "pipelines" / "demo"


# --- remove ------------------------------------------------------------------------------------


def test_remove_cleans_the_tree_venv_pipelines_and_lock_entry(inst: Path, pack: Path) -> None:  # noqa: F811
    assert (inst / "plugins" / "demo" / ".venv" / "pyvenv.cfg").is_file()
    code, out, _ = cli(inst, "remove", "demo")
    assert code == 0, out
    assert not (inst / "plugins" / "demo").exists()  # tree and venv
    assert not pack.exists()
    assert "demo" not in lock(inst)["plugins"]
    assert hidden(inst) == []
    assert "  pipeline  pipelines/demo/alpha.yaml" in out
    assert "secret DEMO_TOKEN: pass --delete-secrets to delete it" in out
    assert "files it wrote under /srv/outbox" in out


def test_remove_reports_in_json(inst: Path, pack: Path) -> None:  # noqa: F811
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 0, out
    report = as_json(out)
    assert report["removed"] == {
        "tree": True,
        "lock_entry": True,
        "pipelines": ["pipelines/demo/alpha.yaml", "pipelines/demo/beta.yaml"],
        "secrets": [],
    }
    assert report["kept"]["data"] == ["/srv/outbox"]
    assert report["kept"]["run_history"] is True
    assert {s["name"] for s in report["kept"]["secrets"]} == {"DEMO_TOKEN", "DEMO_EXTRA"}


def test_an_enabled_plugin_is_not_removed(inst: Path, pack: Path) -> None:  # noqa: F811
    _enable(inst)
    before = snapshot(inst)
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 3
    error = as_json(out)["error"]
    assert error["kind"] == "plugin_enabled"
    assert "horae.yaml" in error["message"]
    assert snapshot(inst) == before


def test_a_plugin_that_is_not_installed_is_refused(inst: Path) -> None:  # noqa: F811
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (3, "not_installed")
    code, out, _ = cli(inst, "remove", "Not_A_Name", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (2, "usage")


def test_a_plugin_without_an_install_record_is_refused(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    (inst / "plugins" / "demo" / ".horae-install.json").unlink()  # like a built-in plugin
    before = snapshot(inst)
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (3, "not_installed")
    assert snapshot(inst) == before


def test_a_lock_entry_without_a_tree_is_removed(inst: Path, pack: Path) -> None:  # noqa: F811
    shutil.rmtree(inst / "plugins" / "demo")
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 0, out
    assert as_json(out)["removed"]["tree"] is False
    assert "demo" not in lock(inst)["plugins"]
    assert not pack.exists()  # checked against the locked digests


def test_a_failed_lock_write_removes_nothing(
    inst: Path,  # noqa: F811
    pack: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot(inst)

    def failing(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli_plugin_manage, "write_lock", failing)
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (1, "lock_write_failed")
    assert snapshot(inst) == before
    assert hidden(inst) == []


# --- remove: path safety -----------------------------------------------------------------------


def test_a_changed_or_foreign_pipeline_file_is_kept(inst: Path, pack: Path) -> None:  # noqa: F811
    (pack / "alpha.yaml").write_text(PIPELINE.format(name="alpha") + "# mine\n", "utf-8")
    (pack / "notes.txt").write_text("my notes\n", encoding="utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 0, out
    kept = as_json(out)["kept"]["pipelines"]
    assert kept == [str(pack / "alpha.yaml"), str(pack / "notes.txt"), str(pack)]
    assert as_json(out)["removed"]["pipelines"] == ["pipelines/demo/beta.yaml"]
    assert (pack / "alpha.yaml").read_text(encoding="utf-8").endswith("# mine\n")
    assert not (pack / "beta.yaml").exists()


def test_a_symlinked_pack_directory_is_never_followed(
    inst: Path,  # noqa: F811
    pack: Path,
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    shutil.move(pack, elsewhere)  # the very files, with their approved digests
    pack.symlink_to(elsewhere, target_is_directory=True)
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 0, out
    assert sorted(p.name for p in elsewhere.iterdir()) == ["alpha.yaml", "beta.yaml"]
    assert pack.is_symlink()
    assert as_json(out)["kept"]["pipelines"] == [str(pack)]


def test_a_symlinked_pipeline_file_is_never_followed(
    inst: Path,  # noqa: F811
    pack: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "precious.yaml"
    shutil.move(pack / "alpha.yaml", target)
    (pack / "alpha.yaml").symlink_to(target)
    code, _, _ = cli(inst, "remove", "demo")
    assert code == 0
    assert target.is_file()
    assert (pack / "alpha.yaml").is_symlink()


def test_a_tampered_record_cannot_name_other_files(inst: Path, pack: Path) -> None:  # noqa: F811
    path = inst / "plugins" / "demo" / ".horae-install.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pipelines"] = ["horae.yaml", "../horae.yaml", "pipelines/demo/alpha.yaml"]
    path.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert code == 0, out
    assert (inst / "horae.yaml").is_file()
    assert as_json(out)["removed"]["pipelines"] == ["pipelines/demo/alpha.yaml"]
    assert (pack / "beta.yaml").is_file()  # not recorded: kept
    assert not (pack / "alpha.yaml").exists()


def test_a_symlinked_tree_is_refused(inst: Path, market: Market, tmp_path: Path) -> None:  # noqa: F811
    add(inst)
    elsewhere = tmp_path / "tree"
    shutil.move(inst / "plugins" / "demo", elsewhere)
    (inst / "plugins" / "demo").symlink_to(elsewhere, target_is_directory=True)
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (3, "install_refused")
    assert (elsewhere / "plugin.yaml").is_file()
    assert "demo" in lock(inst)["plugins"]


def test_a_record_of_another_plugin_is_refused(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    path = inst / "plugins" / "demo" / ".horae-install.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"demo"', '"other"'), "utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--json")
    assert (code, as_json(out)["error"]["kind"]) == (3, "install_refused")
    assert (inst / "plugins" / "demo").is_dir()


def test_delete_own_deletes_only_approved_files_with_their_digest(tmp_path: Path) -> None:
    directory = tmp_path / "pack"
    directory.mkdir()
    (directory / "a.yaml").write_bytes(b"a")
    (directory / "b.yaml").write_bytes(b"b")
    (directory / "c.yaml").write_bytes(b"c")
    approved = {"a": digest(b"a"), "b": digest(b"other"), "d": digest(b"d")}
    kept = delete_own(directory, approved)
    assert kept == [str(directory / "b.yaml"), str(directory / "c.yaml"), str(directory)]
    assert delete_own(tmp_path / "absent", approved) == []
    (directory / "b.yaml").unlink()
    (directory / "c.yaml").unlink()
    assert delete_own(directory, approved) == []
    assert not directory.exists()


# --- remove: secrets ---------------------------------------------------------------------------


def _secrets(instance: Path) -> Path:
    directory = instance / "secrets"
    directory.mkdir(exist_ok=True)
    for name in ("DEMO_TOKEN", "DEMO_EXTRA", "anthropic_api_key"):
        (directory / name).write_text("synthetic-value-0123456789", encoding="utf-8")
    return directory


def test_secrets_are_kept_unless_asked(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    directory = _secrets(inst)
    code, _, _ = cli(inst, "remove", "demo")
    assert code == 0
    assert (directory / "DEMO_TOKEN").is_file()
    assert (directory / "DEMO_EXTRA").is_file()


def test_delete_secrets_deletes_only_unshared_regular_files(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    tmp_path: Path,
) -> None:
    add(inst)
    directory = _secrets(inst)
    precious = tmp_path / "precious"
    precious.write_text("synthetic-value-0123456789", encoding="utf-8")
    (directory / "DEMO_EXTRA").unlink()
    (directory / "DEMO_EXTRA").symlink_to(precious)
    # DEMO_TOKEN is also used by a model alias: kept
    config = inst / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("secret: ANTHROPIC_API_KEY", "secret: DEMO_TOKEN"), "utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    report = as_json(out)
    assert report["removed"]["secrets"] == []
    reasons = {s["name"]: s["reason"] for s in report["kept"]["secrets"]}
    assert reasons["DEMO_TOKEN"] == "another plugin or the configuration uses it"  # noqa: S105
    assert "not a regular file" in reasons["DEMO_EXTRA"]
    assert precious.is_file()
    assert "synthetic-value" not in out


def test_delete_secrets_deletes_an_unused_secret_file(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    directory = _secrets(inst)
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    assert as_json(out)["removed"]["secrets"] == ["DEMO_EXTRA", "DEMO_TOKEN"]
    assert not (directory / "DEMO_TOKEN").exists()
    assert (directory / "anthropic_api_key").is_file()  # another secret's file


def test_an_environment_secret_is_kept(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    config = inst / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(
        text.replace("  refs:\n", "  refs:\n    DEMO_TOKEN: { env: DEMO_TOKEN_VAR }\n"), "utf-8"
    )
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    reasons = {s["name"]: s["reason"] for s in as_json(out)["kept"]["secrets"]}
    assert reasons["DEMO_TOKEN"] == FROM_ENV


def _add_ref(instance: Path, line: str) -> None:
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("  refs:\n", f"  refs:\n    {line}\n"), "utf-8")


SHARED = "its file is shared with another secret"
FROM_ENV = "it is read from an environment variable"


def _kept(out: str) -> dict[str, str]:
    report = as_json(out)
    return {s["name"]: s["reason"] for s in report["kept"]["secrets"]}


def test_a_secret_declared_by_an_unlocked_plugin_is_kept(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    directory = _secrets(inst)
    other = inst / "plugins" / "other"
    other.mkdir()
    data = json.loads(manifest("1.0.0"))
    data.update(name="other", provides={"sources": ["other.fetch"]})
    data["permissions"] = {"secrets": ["DEMO_TOKEN"], "optional_secrets": ["DEMO_EXTRA"]}
    (other / "plugin.yaml").write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    assert set(_kept(out)) == {"DEMO_TOKEN", "DEMO_EXTRA"}
    assert (directory / "DEMO_TOKEN").is_file()


def test_an_unreadable_manifest_keeps_every_secret(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    _secrets(inst)
    (inst / "plugins" / "broken").mkdir()
    (inst / "plugins" / "broken" / "plugin.yaml").write_text("{not: valid", encoding="utf-8")
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    assert all("cannot tell whether broken uses it" in r for r in _kept(out).values())


def test_a_secret_named_in_secrets_refs_is_kept(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    directory = _secrets(inst)
    _add_ref(inst, "DEMO_TOKEN: { file: DEMO_TOKEN }")
    _add_ref(inst, "OTHER_KEY: { file: DEMO_EXTRA }")  # another secret reads DEMO_EXTRA's file
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    reasons = _kept(out)
    assert "secrets.refs" in reasons["DEMO_TOKEN"]
    assert reasons["DEMO_EXTRA"] == SHARED
    assert (directory / "DEMO_EXTRA").is_file()


def test_a_case_collision_keeps_the_secret(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    _secrets(inst)
    _add_ref(inst, "OTHER_KEY: { file: demo_token }")  # the same file on a case-blind disk
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    assert _kept(out)["DEMO_TOKEN"] == SHARED
    assert as_json(out)["removed"]["secrets"] == ["DEMO_EXTRA"]  # not shared: deleted


def test_an_alias_of_the_file_keeps_the_secret(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    directory = _secrets(inst)
    _add_ref(inst, "OTHER_KEY: { file: other_key }")
    (directory / "other_key").symlink_to(directory / "DEMO_TOKEN")  # reaches the same inode
    os.link(directory / "DEMO_EXTRA", directory / "extra_copy")  # a second hard link
    code, out, _ = cli(inst, "remove", "demo", "--delete-secrets", "--json")
    assert code == 0, out
    reasons = _kept(out)
    assert reasons["DEMO_TOKEN"] == SHARED
    assert reasons["DEMO_EXTRA"] == "its file has other hard links"
    assert (directory / "DEMO_TOKEN").is_file()


# --- upgrade of a pipeline pack ----------------------------------------------------------------


def test_a_pack_upgrade_replaces_its_pipelines_after_approval(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    pack: Path,
) -> None:
    changed = PIPELINE.format(name="beta").replace("*/10", "*/5")
    files = _pipelines(market, {"beta": changed, "gamma": PIPELINE.format(name="gamma")})
    market.release("1.1.0", _pack("1.1.0", ["beta", "gamma"]), files)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3, out  # a changed and a new pipeline widen
    assert sorted(p.name for p in pack.iterdir()) == ["alpha.yaml", "beta.yaml"]
    code, out, _ = cli(inst, "upgrade", "demo", answer="y\n")
    assert code == 0, out
    assert "    - alpha\n" in out
    assert "    ~ beta  (new: needs approval)" in out
    assert "    + gamma  (new: needs approval)" in out
    assert sorted(p.name for p in pack.iterdir()) == ["beta.yaml", "gamma.yaml"]
    assert (pack / "beta.yaml").read_text(encoding="utf-8") == changed
    assert lock(inst)["plugins"]["demo"]["pipelines"]["beta"] == digest(changed.encode())
    assert hidden(inst) == []


def test_a_pack_upgrade_dropping_every_pipeline_removes_the_directory(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    pack: Path,
) -> None:
    files = _pipelines(market, {})
    market.release("1.1.0", manifest("1.1.0"), files)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert not pack.exists()
    assert "pipelines" not in lock(inst)["plugins"]["demo"]
    assert hidden(inst) == []


def test_a_pack_upgrade_never_deletes_an_edited_pipeline(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    pack: Path,
) -> None:
    (pack / "alpha.yaml").write_text(PIPELINE.format(name="alpha") + "# mine\n", "utf-8")
    files = _pipelines(market, {"alpha": PIPELINE.format(name="alpha")})
    market.release("1.1.0", _pack("1.1.0", ["alpha"]), files)
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3
    error = as_json(out)["error"]
    assert error["kind"] == "install_refused"
    assert "changed after approval" in error["message"]
    assert snapshot(inst) == before


def test_a_failed_pack_upgrade_restores_the_old_pipelines(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    pack: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _pipelines(market, {"alpha": PIPELINE.format(name="alpha")})
    market.release("1.1.0", _pack("1.1.0", ["alpha"]), files)
    before = snapshot(inst)

    def failing(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli_plugin_manage, "write_lock", failing)
    code, _, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 1
    assert snapshot(inst) == before
    assert hidden(inst) == []


# --- audit -------------------------------------------------------------------------------------


def _audit(instance: Path) -> tuple[int, dict[str, Any]]:
    code, out, _ = cli(instance, "audit", "--json")
    report: dict[str, Any] = json.loads(out)
    return code, report


def _kinds(report: dict[str, Any]) -> set[str]:
    kinds = {d["kind"] for p in report["plugins"] for d in p["drift"]}
    return kinds | {d["kind"] for d in report["leftovers"]}


def test_audit_lists_plugins_in_json(inst: Path, pack: Path) -> None:  # noqa: F811
    _enable(inst)
    code, report = _audit(inst)
    assert code == 0, report
    assert report["drift"] is False
    (row,) = report["plugins"]
    assert row["name"] == "demo"
    assert row["version"] == row["installed_version"] == "1.0.0"
    assert (row["source"], row["tier"], row["enabled"]) == ("marketplace", "first-party", True)
    assert row["permissions"]["network"] == ["api.example.org"]
    assert row["permissions"]["secrets"] == ["DEMO_TOKEN"]
    assert row["pipelines"] == ["alpha", "beta"]
    assert row["last_activity"] is None
    assert row["drift"] == []


def test_audit_prints_a_table(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    code, out, _ = cli(inst, "audit")
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0].split() == [
        "NAME",
        "VERSION",
        "SOURCE",
        "TIER",
        "ENABLED",
        "LAST",
        "ACTIVITY",
        "DRIFT",
    ]
    assert lines[1].split() == ["demo", "1.0.0", "marketplace", "first-party", "no", "never", "-"]
    assert "  network: api.example.org" in out
    assert "  intents: note.create" in out
    assert out.rstrip().endswith("no drift")


def test_audit_reads_the_last_activity_from_the_store(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    path = inst / "state" / "horae.db"
    Store.open(path).close()
    with sqlite3.connect(path) as conn:
        for run, started, plugins in (
            ("r1", "2026-11-01T08:00:00.000000Z", {"demo": {"version": "1.0.0"}}),
            ("r2", "2026-11-02T08:00:00.000000Z", {"demo": {"version": "1.0.0"}}),
            ("r3", "2026-11-03T08:00:00.000000Z", {"other": {"version": "1.0.0"}}),
        ):
            conn.execute(
                "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, "
                "record) VALUES (?, 'daily', 'ok', 0, ?, ?, ?)",
                (run, started, started, json.dumps({"plugins": plugins})),
            )
    code, report = _audit(inst)
    assert code == 0
    assert report["plugins"][0]["last_activity"] == "2026-11-02T08:00:00+00:00"


def test_audit_reports_lock_and_tree_drift(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    # a locked plugin that is not installed, and a recorded tree that is not locked
    data = lock(inst)
    data["plugins"]["ghost"] = copy.deepcopy(data["plugins"]["demo"])
    (inst / "horae.lock").write_text(yaml.safe_dump(data), encoding="utf-8")
    shutil.copytree(inst / "plugins" / "demo", inst / "plugins" / "stray")
    code, report = _audit(inst)
    assert code == 4, report
    assert report["drift"] is True
    drift = {p["name"]: {d["kind"] for d in p["drift"]} for p in report["plugins"]}
    assert drift == {"demo": set(), "ghost": {"not_installed"}, "stray": {"not_locked"}}


def test_audit_reports_a_record_and_a_manifest_that_are_not_the_locked_ones(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    path = inst / "plugins" / "demo" / ".horae-install.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace('"1.0.0"', '"9.9.9"'), encoding="utf-8")
    assert _kinds(_audit(inst)[1]) == {"record_mismatch"}
    path.write_text(original, encoding="utf-8")
    widened = json.loads(manifest("1.0.0"))
    widened["permissions"]["network"].append("evil.example.net")
    (inst / "plugins" / "demo" / "plugin.yaml").write_text(json.dumps(widened), "utf-8")
    code, report = _audit(inst)
    assert code == 4
    assert _kinds(report) == {"host_refuses"}


def test_audit_reports_a_missing_venv(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    shutil.rmtree(inst / "plugins" / "demo" / ".venv")
    code, report = _audit(inst)
    assert (code, _kinds(report)) == (4, {"venv_missing"})


def test_audit_reports_pipeline_drift(inst: Path, pack: Path) -> None:  # noqa: F811
    (pack / "alpha.yaml").write_text("# changed\n", encoding="utf-8")
    (pack / "beta.yaml").unlink()
    (pack / "extra.yaml").write_text("# mine\n", encoding="utf-8")
    code, report = _audit(inst)
    assert code == 4
    assert _kinds(report) == {"pipeline_changed", "pipeline_missing", "pipeline_unapproved"}
    shutil.rmtree(pack)
    assert _kinds(_audit(inst)[1]) == {"pipeline_missing"}


def test_audit_reports_leftovers_of_an_interrupted_upgrade(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    (inst / "plugins" / ".upgrade-old-demo-0123456789abcdef").mkdir()
    code, out, _ = cli(inst, "audit")
    assert code == 4
    assert "drift (leftover)" in out


def test_audit_reports_a_plugin_without_record_as_local(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    os.unlink(inst / "plugins" / "demo" / ".horae-install.json")
    code, report = _audit(inst)
    assert code == 0, report  # an accepted gap (decision 70 F): checked by its manifest only
    assert report["plugins"][0]["source"] == "local"


def test_audit_works_on_an_instance_the_loader_refuses(inst: Path, pack: Path) -> None:  # noqa: F811
    _enable(inst)
    (pack / "alpha.yaml").write_text(PIPELINE.format(name="alpha") + "# edited\n", "utf-8")
    code, report = _audit(inst)
    assert code == 4
    assert _kinds(report) == {"pipeline_changed"}


def test_audit_on_an_empty_instance(inst: Path) -> None:  # noqa: F811
    code, out, _ = cli(inst, "audit")
    assert code == 0
    assert "no plugin installed" in out


# --- the lock is renamed into place, then flushing its directory fails -----------------------


def _directory_fsync_raises(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """`fsync` of a directory raises `error` (that of the lock's temporary file works): the
    rename of `write_lock`, its commit point, has happened."""
    real = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise error
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def test_write_lock_reports_a_failed_flush_as_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _directory_fsync_raises(monkeypatch, OSError(5, "Input/output error"))
    path = tmp_path / "horae.lock"
    warning = lock_module.write_lock(path, Lockfile(schema_version=1))
    assert warning is not None
    assert "could not be flushed" in warning
    assert lock_module.lock_is(path, Lockfile(schema_version=1))


@pytest.mark.parametrize("error", [OSError(5, "Input/output error"), KeyboardInterrupt()])
def test_an_upgrade_whose_lock_landed_is_committed(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    add(inst)
    second = market.release("1.1.0", manifest("1.1.0"))
    _directory_fsync_raises(monkeypatch, error)
    code, out, err = cli(inst, "upgrade", "demo", "--yes")
    # the lock and the tree agree on the new version, whatever the exit
    assert code == (0 if isinstance(error, OSError) else 130), out + err
    locked = lock(inst)["plugins"]["demo"]
    assert (locked["version"], locked["commit"]) == ("1.1.0", second)
    installed = json.loads((inst / "plugins" / "demo" / ".horae-install.json").read_text())
    assert (installed["version"], installed["commit"]) == ("1.1.0", second)
    assert "previous version is kept" not in out + err
    assert "warning:" in err
    assert hidden(inst) == []


@pytest.mark.parametrize("error", [OSError(5, "Input/output error"), KeyboardInterrupt()])
def test_a_remove_whose_lock_landed_is_committed(
    inst: Path,  # noqa: F811
    pack: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    _directory_fsync_raises(monkeypatch, error)
    code, out, err = cli(inst, "remove", "demo")
    assert code == (0 if isinstance(error, OSError) else 130), out + err
    assert "demo" not in lock(inst)["plugins"]
    assert not (inst / "plugins" / "demo").exists()
    assert not pack.exists()
    assert "nothing was removed" not in out + err
    assert "warning:" in err
    assert hidden(inst) == []
