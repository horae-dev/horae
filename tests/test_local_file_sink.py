# SPDX-License-Identifier: Apache-2.0
"""`LocalFileSink` (#22, provisional): atomic writes and restores in a local vault, with the
same target rules as the read-only `LocalSink`."""

import asyncio
import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import horae.apply.local as local
from horae.apply import LocalFileSink, RestorableSink, SinkCall, SinkError
from horae.contracts.intents import intent_adapter

OWNED = "Daily/today.assistant.md"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _call(**fields: Any) -> SinkCall:
    intent = {"type": "note.append_section", "path": OWNED, "heading": "H", "content": "c"}
    intent.update(fields)
    if intent["type"] != "note.append_section":
        intent.pop("heading")
    return SinkCall("run", 0, "vault.write_note", {}, intent_adapter.validate_python(intent))


def _apply(sink: LocalFileSink, call: SinkCall, expected: str | None) -> Any:
    return asyncio.run(sink.apply(call, expected_before_sha256=expected))


def _restore(sink: LocalFileSink, target: str, content: bytes | None, expected: str | None) -> None:
    asyncio.run(sink.restore(target, content, expected_sha256=expected))


def _kind(fn: Any) -> str:
    with pytest.raises(SinkError) as caught:
        fn()
    return caught.value.kind


def test_it_is_a_restorable_sink(tmp_path: Path) -> None:
    assert isinstance(LocalFileSink(tmp_path), RestorableSink)


def test_apply_creates_directories_and_writes_atomically(tmp_path: Path) -> None:
    sink = LocalFileSink(tmp_path)
    call = _call(type="note.replace_owned_block", block="b")
    applied = _apply(sink, call, None)
    data = (tmp_path / OWNED).read_bytes()
    assert applied.before is None
    assert applied.after == data
    assert b"<!-- horae:begin b -->" in data
    assert stat.S_IMODE((tmp_path / OWNED).stat().st_mode) == 0o644
    assert sorted(p.name for p in (tmp_path / "Daily").iterdir()) == ["today.assistant.md"]
    assert asyncio.run(sink.read_target(OWNED)) == data


def test_apply_keeps_the_mode_and_the_rest_of_the_note(tmp_path: Path) -> None:
    note = tmp_path / OWNED
    note.parent.mkdir()
    before = b"# Day\r\nkeep \xc3\xa9\r\n"
    note.write_bytes(before)
    note.chmod(0o600)
    applied = _apply(LocalFileSink(tmp_path), _call(), _sha(before))
    assert note.read_bytes() == applied.after
    assert applied.after.startswith(before)
    assert b"## H\r\n\r\nc\r\n" in applied.after
    assert stat.S_IMODE(note.stat().st_mode) == 0o600


def test_apply_is_a_compare_and_swap(tmp_path: Path) -> None:
    note = tmp_path / OWNED
    note.parent.mkdir()
    note.write_bytes(b"x\n")
    sink = LocalFileSink(tmp_path)
    assert _kind(lambda: _apply(sink, _call(), _sha(b"other"))) == "target_changed"
    assert _kind(lambda: _apply(sink, _call(), None)) == "target_changed"
    assert note.read_bytes() == b"x\n"


def test_apply_refuses_what_read_refuses(tmp_path: Path) -> None:
    sink = LocalFileSink(tmp_path)
    (tmp_path / "Daily").mkdir()
    real = tmp_path / "real.assistant.md"
    real.write_bytes(b"r\n")
    (tmp_path / OWNED).symlink_to(real)
    assert _kind(lambda: _apply(sink, _call(), _sha(b"r\n"))) == "symlink"
    (tmp_path / OWNED).unlink()
    os.link(real, tmp_path / OWNED)
    assert _kind(lambda: _apply(sink, _call(), _sha(b"r\n"))) == "hard_link"
    (tmp_path / OWNED).unlink()
    (tmp_path / "Link").symlink_to(tmp_path / "Daily")
    assert _kind(lambda: _apply(sink, _call(path="Link/x.assistant.md"), None)) == "symlink"
    assert _kind(lambda: _apply(sink, _call(path="a:b.assistant.md"), None)) == "path_invalid"
    assert _kind(lambda: _apply(sink, _call(), None)) == "note_missing"
    task = SinkCall(
        "run",
        0,
        "vault.tasks",
        {},
        intent_adapter.validate_python({"type": "task.create", "title": "t", "fingerprint": "f"}),
    )
    assert _kind(lambda: _apply(sink, task, None)) == "not_supported"
    assert _kind(lambda: _apply(LocalFileSink(None), _call(), None)) == "vault_unset"
    assert _kind(lambda: asyncio.run(LocalFileSink(None).read_target(OWNED))) == "vault_unset"
    assert real.read_bytes() == b"r\n"


def test_restore_writes_and_removes_with_a_compare_and_swap(tmp_path: Path) -> None:
    sink = LocalFileSink(tmp_path)
    note = tmp_path / OWNED
    _restore(sink, OWNED, b"one\r\n\x00two", None)
    assert note.read_bytes() == b"one\r\n\x00two"
    assert _kind(lambda: _restore(sink, OWNED, b"x", None)) == "target_changed"
    _restore(sink, OWNED, None, _sha(b"one\r\n\x00two"))
    assert not note.exists()
    _restore(sink, OWNED, None, None)  # already gone
    _restore(sink, "Missing/dir.assistant.md", None, None)
    assert not (tmp_path / "Missing").exists()
    assert _kind(lambda: _restore(sink, "Missing/x.md", None, _sha(b"x"))) == "target_changed"


def test_write_failures_leave_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = LocalFileSink(tmp_path)
    (tmp_path / "Daily").mkdir()

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "replace", broken)
    assert _kind(lambda: _restore(sink, OWNED, b"x", None)) == "unwritable"
    assert list((tmp_path / "Daily").iterdir()) == []


def test_unwritable_directories(tmp_path: Path) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root ignores permissions
        pytest.skip("permissions are not enforced")
    sink = LocalFileSink(tmp_path)
    locked = tmp_path / "Locked"
    locked.mkdir()
    (locked / "n.assistant.md").write_bytes(b"n\n")
    locked.chmod(0o500)
    try:
        assert _kind(lambda: _restore(sink, "Locked/new.assistant.md", b"x", None)) == "unwritable"
        assert _kind(lambda: _restore(sink, "Locked/n.assistant.md", None, _sha(b"n\n"))) == (
            "unwritable"
        )
        assert _kind(lambda: _restore(sink, "Locked/sub/x.assistant.md", b"x", None)) == (
            "unwritable"
        )
    finally:
        locked.chmod(0o700)


# --- review findings 8 to 10: nothing created before the check, re-checks, temporary files ---


def test_a_failed_check_creates_no_directory(tmp_path: Path) -> None:
    sink = LocalFileSink(tmp_path)
    deep = "A/B/n.assistant.md"
    assert _kind(lambda: _apply(sink, _call(path=deep), _sha(b"x"))) == "target_changed"
    assert _kind(lambda: _restore(sink, deep, b"x", _sha(b"x"))) == "target_changed"
    assert list(tmp_path.iterdir()) == []
    assert asyncio.run(sink.parent_exists(deep)) is False
    _restore(sink, deep, b"x", None)
    assert asyncio.run(sink.parent_exists(deep)) is True
    assert (tmp_path / deep).read_bytes() == b"x"


def test_new_directories_are_synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        synced.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    sink = LocalFileSink(tmp_path)
    _restore(sink, "A/B/n.assistant.md", b"x", None)
    created = len(synced)
    synced.clear()
    _restore(sink, "A/B/m.assistant.md", b"x", None)
    assert created == len(synced) + 2  # the vault (for A) and A (for B)


def test_a_directory_created_meanwhile_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.mkdir

    def racing(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real(path, mode, dir_fd=dir_fd)
        raise FileExistsError(path)

    monkeypatch.setattr(os, "mkdir", racing)
    _restore(LocalFileSink(tmp_path), "A/n.assistant.md", b"x", None)
    assert (tmp_path / "A" / "n.assistant.md").read_bytes() == b"x"


def test_a_directory_that_vanishes_after_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def vanishing(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        pass  # "created", then removed by someone else before it is opened

    monkeypatch.setattr(os, "mkdir", vanishing)
    sink = LocalFileSink(tmp_path)
    assert _kind(lambda: _restore(sink, "A/n.assistant.md", b"x", None)) == "unwritable"


def _meddle(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """Run `change(dir_fd)` between the compare and the write (where the sweep runs)."""
    real = local._sweep

    def meddling(dir_fd: int) -> None:
        change(dir_fd)
        real(dir_fd)

    monkeypatch.setattr(local, "_sweep", meddling)


def _hidden_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".horae-"))


def test_a_note_replaced_between_check_and_rename_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "n.assistant.md"
    note.write_bytes(b"old\n")
    sink = LocalFileSink(tmp_path)

    def editor(dir_fd: int) -> None:
        replacement = tmp_path / "editor.tmp"
        replacement.write_bytes(b"old\n")  # same bytes, another file: still a change
        replacement.replace(note)

    _meddle(monkeypatch, editor)
    assert _kind(lambda: _restore(sink, note.name, b"new\n", _sha(b"old\n"))) == "target_changed"
    assert note.read_bytes() == b"old\n"
    assert _hidden_files(tmp_path) == []


def test_a_note_created_between_check_and_rename_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "n.assistant.md"
    _meddle(monkeypatch, lambda dir_fd: note.write_bytes(b"user\n"))
    sink = LocalFileSink(tmp_path)
    assert _kind(lambda: _restore(sink, note.name, b"new\n", None)) == "target_changed"
    assert note.read_bytes() == b"user\n"
    assert _hidden_files(tmp_path) == []


def test_a_removal_checks_what_it_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note = tmp_path / "n.assistant.md"
    note.write_bytes(b"ours\n")
    _meddle(monkeypatch, lambda dir_fd: note.write_bytes(b"edited\n"))
    sink = LocalFileSink(tmp_path)
    assert _kind(lambda: _restore(sink, note.name, None, _sha(b"ours\n"))) == "target_changed"
    assert note.read_bytes() == b"edited\n"  # put back
    assert _hidden_files(tmp_path) == []


def test_a_removal_never_puts_back_over_a_new_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "n.assistant.md"
    note.write_bytes(b"ours\n")
    _meddle(monkeypatch, lambda dir_fd: note.write_bytes(b"edited\n"))
    real = local._read_at

    def creating(dir_fd: int, name: str, path: str) -> bytes | None:
        if name.startswith(".horae-"):
            note.write_bytes(b"newer\n")  # a note created right after the rename
        return real(dir_fd, name, path)

    monkeypatch.setattr(local, "_read_at", creating)
    with pytest.raises(SinkError) as caught:
        _restore(LocalFileSink(tmp_path), note.name, None, _sha(b"ours\n"))
    assert caught.value.kind == "target_changed"
    assert "is kept as .horae-" in caught.value.message
    assert note.read_bytes() == b"newer\n"
    (kept,) = _hidden_files(tmp_path)
    assert kept.endswith(".old")
    assert (tmp_path / kept).read_bytes() == b"edited\n"


def test_removal_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note = tmp_path / "n.assistant.md"
    note.write_bytes(b"ours\n")
    sink = LocalFileSink(tmp_path)
    expected = _sha(b"ours\n")
    real_rename, real_unlink = os.rename, os.unlink

    def gone(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "rename", gone)
    assert _kind(lambda: _restore(sink, note.name, None, expected)) == "target_changed"

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "rename", denied)
    assert _kind(lambda: _restore(sink, note.name, None, expected)) == "unwritable"
    monkeypatch.setattr(os, "rename", real_rename)

    def stuck(name: Any, *args: Any, **kwargs: Any) -> None:
        if str(name).endswith(".old"):
            raise OSError(5, "Input/output error")
        real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", stuck)
    assert _kind(lambda: _restore(sink, note.name, None, expected)) == "unwritable"
    assert note.read_bytes() == b"ours\n"  # put back


def test_temporary_files_are_short_and_stale_ones_are_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []
    real = os.replace

    def recording(src: Any, dst: Any, **kwargs: Any) -> None:
        names.append(str(src))
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", recording)
    stale = tmp_path / ".horae-0123456789ab.tmp"
    fresh = tmp_path / ".horae-ba9876543210.tmp"
    kept = [fresh, tmp_path / ".horae-0123456789ab.old", tmp_path / ".horae-x.tmp"]
    for path in [stale, *kept]:
        path.write_bytes(b"partial")
    old = time.time() - local.STALE_TMP_S - 60
    for path in [stale, *kept[1:]]:
        os.utime(path, (old, old))
    long_name = "n" * 200 + ".assistant.md"
    _restore(LocalFileSink(tmp_path), long_name, b"x", None)
    (name,) = names
    assert re.fullmatch(r"\.horae-[0-9a-f]{12}\.tmp", name)
    assert not stale.exists()
    assert all(path.exists() for path in kept)


def test_too_large_contents_are_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "MAX_NOTE_BYTES", 4)
    sink = LocalFileSink(tmp_path)
    assert _kind(lambda: _restore(sink, "A/n.assistant.md", b"12345", None)) == "note_too_large"
    assert list(tmp_path.iterdir()) == []


def test_the_vault_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "alias").symlink_to(vault)
    identity = LocalFileSink(vault).identity
    info = vault.stat()
    assert identity == f"local:{info.st_dev}:{info.st_ino}:{os.path.realpath(vault)}"
    assert LocalFileSink(tmp_path / "alias").identity == identity
    assert LocalFileSink(tmp_path).identity != identity
    assert _kind(lambda: LocalFileSink(None).identity) == "vault_unset"
    assert _kind(lambda: LocalFileSink(tmp_path / "missing").identity) == "unreadable"
    monkeypatch.setattr(local, "MAX_IDENTITY", 20)
    digest = hashlib.sha256(os.fsencode(os.path.realpath(vault))).hexdigest()
    assert LocalFileSink(vault).identity == f"local:{info.st_dev}:{info.st_ino}:sha256:{digest}"


def test_file_system_work_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[int] = []
    real = local.read_note

    def recording(vault: Path, path: str) -> bytes | None:
        threads.append(threading.get_ident())
        return real(vault, path)

    monkeypatch.setattr(local, "read_note", recording)
    sink = LocalFileSink(tmp_path)
    asyncio.run(sink.read_target(OWNED))
    asyncio.run(sink.read(_call()))
    assert threads
    assert threading.get_ident() not in threads
