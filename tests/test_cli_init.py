# SPDX-License-Identifier: Apache-2.0
"""`horae init` (#26): a working example instance, never overwriting anything."""

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_init
from horae.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, app
from horae.config import check_instance, load_instance
from horae.contracts import Lockfile
from horae.plugins.builtin import BUILTIN, InstallError, install
from horae.plugins.host import HostRegistry

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "walking-skeleton"
runner = CliRunner()


def _init(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["init", *args])
    return result.exit_code, result.output


def test_init_creates_a_valid_instance_with_its_plugins(tmp_path: Path) -> None:
    root = tmp_path / "new" / "instance"
    code, out = _init(str(root), "--json")
    assert code == EXIT_OK, out
    report = json.loads(out)
    real = root.resolve()
    assert report == {
        "schema_version": 1,
        "instance": str(real),
        "vault": str(real / "vault"),
        "pipelines": ["daily-fixture"],
        "plugins": sorted(BUILTIN),
        "files": [
            ".gitignore",
            "horae.lock",
            "horae.yaml",
            "pipelines/daily-fixture.yaml",
            "plugins/file-sink/plugin.yaml",
            "plugins/fixture-source/plugin.yaml",
        ],
    }
    assert (real / "vault").is_dir()
    assert stat.S_IMODE(os.stat(real / "state").st_mode) == 0o700
    assert check_instance(real).ok
    instance = load_instance(real)
    registry = HostRegistry.from_directory(instance.lock, real / "plugins")
    assert registry.plugin_for_tool("file-sink.write_note").manifest.name == "file-sink"
    assert ".gitignore" in report["files"]
    assert (real / ".gitignore").read_text(encoding="utf-8").splitlines()[1:] == [
        "state/",
        "plugins/",
        "vault/",
    ]


def test_init_files_are_the_examples(tmp_path: Path) -> None:
    """The packaged template stays identical to `examples/walking-skeleton`, and the lock
    generated is the example's with the vault replaced."""
    for name in ("horae.yaml", "pipelines/daily-fixture.yaml"):
        assert (cli_init.TEMPLATE / name).read_bytes() == (EXAMPLE / name).read_bytes()
    vault = tmp_path / "vault"
    example = yaml.safe_load((EXAMPLE / "horae.lock").read_text(encoding="utf-8"))
    example["plugins"]["file-sink"]["permissions"]["paths"]["write"] = [str(vault)]
    assert Lockfile.model_validate(cli_init.lock_data(vault)) == Lockfile.model_validate(example)


def test_init_in_the_current_directory_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = os.stat(tmp_path)
    monkeypatch.chdir(tmp_path)
    code, out = _init()
    assert code == EXIT_OK, out
    assert out.splitlines()[0] == f"created a Horae instance in {tmp_path.resolve()}"
    assert f"horae run daily-fixture --dry-run -i {tmp_path.resolve()}" in out
    assert "not relocatable" in out
    assert (tmp_path / "horae.yaml").is_file()
    # The existing empty directory is filled in place (a shell inside it stays there).
    after = os.stat(tmp_path)
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".horae-init-")]
    assert check_instance(tmp_path).ok


def test_init_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("mine", encoding="utf-8")
    code, out = _init(str(tmp_path))
    assert code == EXIT_REFUSED == 3
    assert "is not empty" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.md"]
    code, out = _init(str(tmp_path), "--json")
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["kind"] == "target_not_empty"


def test_init_twice_refuses_the_second_time(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    assert _init(str(root))[0] == EXIT_OK
    before = (root / "horae.lock").read_bytes()
    assert _init(str(root))[0] == EXIT_REFUSED
    assert (root / "horae.lock").read_bytes() == before


def test_init_refuses_a_file_or_a_symbolic_link(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x", encoding="utf-8")
    code, out = _init(str(tmp_path / "file"), "--json")
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["kind"] == "target_invalid"
    (tmp_path / "empty").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "empty")
    assert _init(str(tmp_path / "link"))[0] == EXIT_REFUSED
    assert list((tmp_path / "empty").iterdir()) == []


def test_init_reports_write_and_install_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode
        pytest.skip("permissions are not enforced for root")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code, out = _init(str(locked / "instance"), "--json")
    finally:
        locked.chmod(0o700)
    assert code == EXIT_PROBLEMS
    assert json.loads(out)["error"]["kind"] == "init_failed"

    def broken(directory: Path, *, python: str | None = None) -> dict[str, Path]:
        raise InstallError("no interpreter")

    monkeypatch.setattr(cli_init, "install", broken)
    code, out = _init(str(tmp_path / "other"))
    assert code == EXIT_PROBLEMS
    assert "horae init: no interpreter" in out


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_path_the_lock_cannot_hold_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "new" / "back\\slash"
    code, out = _init(str(target), "--json")
    assert code == EXIT_PROBLEMS == 1
    error = json.loads(out)["error"]
    assert error["kind"] == "target_invalid"
    assert "nothing was written" in error["message"]
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("existing", [False, True])
def test_an_install_failure_mid_way_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    real = install

    def half(directory: Path, *, python: str | None = None) -> dict[str, Path]:
        real(directory, python=python)  # the plugins are written, then it fails
        raise InstallError("no interpreter")

    monkeypatch.setattr(cli_init, "install", half)
    target = tmp_path / "new" / "instance"
    if existing:
        target.mkdir(parents=True)
    code, out = _init(str(target), "--json")
    assert code == EXIT_PROBLEMS
    assert json.loads(out)["error"]["kind"] == "install_failed"
    if existing:
        assert _leftovers(target) == []  # still the empty directory it was
    else:
        assert _leftovers(tmp_path) == []  # nor `new/`, which init created


def test_a_target_created_meanwhile_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance"
    real = install

    def racing(directory: Path, *, python: str | None = None) -> dict[str, Path]:
        target.mkdir()
        (target / "mine.md").write_text("mine", encoding="utf-8")
        return real(directory, python=python)

    monkeypatch.setattr(cli_init, "install", racing)
    code, out = _init(str(target), "--json")
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["kind"] == "target_not_empty"
    assert _leftovers(target) == ["mine.md"]
    assert _leftovers(tmp_path) == ["instance"]


def _btrfs_listdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model btrfs: listing a directory descriptor only returns the entries that existed
    when it was opened, however often it is listed again."""
    real_open, real_close, real_listdir = os.open, os.close, os.listdir
    snapshots: dict[int, list[str]] = {}

    def opening(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_DIRECTORY:
            snapshots[fd] = real_listdir(fd)
        return fd

    def closing(fd: int) -> None:
        snapshots.pop(fd, None)
        real_close(fd)

    def listing(path: Any = ".") -> list[str]:
        if isinstance(path, int) and path in snapshots:
            return list(snapshots[path])
        return real_listdir(path)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    monkeypatch.setattr(os, "listdir", listing)


def test_init_fills_an_empty_directory_on_btrfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance"
    target.mkdir()
    _btrfs_listdir(monkeypatch)
    code, out = _init(str(target), "--json")
    assert code == EXIT_OK, out
    assert (target / "horae.lock").is_file()
    assert not [name for name in os.listdir(target) if name.startswith(".horae-init-")]


def test_an_entry_appearing_in_an_empty_target_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance"
    target.mkdir()
    real = install

    def racing(directory: Path, *, python: str | None = None) -> dict[str, Path]:
        (target / "mine.md").write_text("mine", encoding="utf-8")
        return real(directory, python=python)

    monkeypatch.setattr(cli_init, "install", racing)
    _btrfs_listdir(monkeypatch)
    code, out = _init(str(target), "--json")
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["kind"] == "target_not_empty"
    assert _leftovers(target) == ["mine.md"]
