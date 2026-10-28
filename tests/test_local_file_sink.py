# SPDX-License-Identifier: Apache-2.0
"""`LocalFileSink` (#22, provisional): atomic writes and restores in a local vault, with the
same target rules as the read-only `LocalSink`."""

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest

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
