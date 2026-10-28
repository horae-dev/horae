# SPDX-License-Identifier: Apache-2.0
"""The dry-run diff (#21): sink previews, change sets, unified diffs, text and JSON output."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from apply_fixtures import DAILY, HOOK, INBOX, INTENTS, record
from horae.apply import (
    MAX_DIFF_BYTES,
    Applied,
    ChangeSet,
    FileChange,
    HttpCall,
    LocalSink,
    Notification,
    Preview,
    PreviewError,
    SinkCall,
    StaticSinks,
    TaskChange,
    dedupe_key,
    plan_changes,
    render_text,
    to_dict,
    to_json,
    unified_diff,
)
from horae.apply import local as local_module
from horae.apply.notes import note_after
from horae.apply.testing import FixtureSink
from horae.contracts import RunRecord
from horae.contracts.intents import intent_adapter


def _plan(run: RunRecord, sink: Any, **kwargs: Any) -> ChangeSet:
    return asyncio.run(plan_changes(run, StaticSinks(default=sink), **kwargs))


def _note(**fields: Any) -> Any:
    return intent_adapter.validate_python(fields)


# --- note conventions -------------------------------------------------------------------------


def test_note_create_refuses_an_existing_note_and_adds_a_final_newline() -> None:
    intent = _note(type="note.create", path="A.assistant.md", content="hi")
    assert note_after(intent, None) == b"hi\n"
    with pytest.raises(PreviewError) as caught:
        note_after(intent, b"x")
    assert caught.value.kind == "note_exists"


def test_only_owned_notes_are_created_or_appended_to() -> None:
    for extra in ({"type": "note.create"}, {"type": "note.append_section", "heading": "H"}):
        intent = _note(path="Mine.md", content="x", **extra)
        with pytest.raises(PreviewError) as caught:
            note_after(intent, b"")
        assert caught.value.kind == "not_owned"


def test_append_section_needs_the_note() -> None:
    intent = _note(type="note.append_section", path=INBOX, heading="H", content="body\n")
    with pytest.raises(PreviewError) as caught:
        note_after(intent, None)
    assert caught.value.kind == "note_missing"
    assert note_after(intent, b"") == b"## H\n\nbody\n"
    assert note_after(intent, b"top") == b"top\n\n## H\n\nbody\n"


def test_append_section_refuses_binary_notes() -> None:
    intent = _note(type="note.append_section", path=INBOX, heading="H", content="b")
    with pytest.raises(PreviewError) as caught:
        note_after(intent, b"\xff\xfe")
    assert caught.value.kind == "note_not_text"


def _block(content: str = "new") -> Any:
    return _note(type="note.replace_owned_block", path="Any.md", block="b", content=content)


def test_replace_owned_block_replaces_creates_and_appends() -> None:
    begin, end = "<!-- horae:begin b -->", "<!-- horae:end b -->"
    assert note_after(_block(), None).decode() == f"{begin}\nnew\n{end}\n"
    assert note_after(_block(), b"top").decode() == f"top\n\n{begin}\nnew\n{end}\n"
    text = f"top\r\n{begin}\r\nold\r\n{end}\r\ntail\r\n".encode()
    assert note_after(_block(), text).decode() == f"top\r\n{begin}\nnew\n{end}\ntail\r\n"
    at_end = f"{begin}\nold\n{end}".encode()
    assert note_after(_block(), at_end).decode() == f"{begin}\nnew\n{end}\n"


@pytest.mark.parametrize(
    "text",
    [
        "<!-- horae:begin b -->\n<!-- horae:begin b -->\n<!-- horae:end b -->\n",
        "<!-- horae:end b -->\n<!-- horae:begin b -->\n",
        "<!-- horae:begin b -->\nno end\n",
    ],
)
def test_replace_owned_block_refuses_ambiguous_markers(text: str) -> None:
    with pytest.raises(PreviewError) as caught:
        note_after(_block(), text.encode())
    assert caught.value.kind == "block_ambiguous"


def test_untrusted_content_cannot_move_block_markers() -> None:
    with pytest.raises(PreviewError) as caught:
        note_after(_block("x\n<!-- horae:end b -->\nevil"), None)
    assert caught.value.kind == "content_has_marker"


# --- unified diffs ----------------------------------------------------------------------------


def test_unified_diff_is_deterministic_with_git_style_labels() -> None:
    diff = unified_diff("N.md", b"a\nb\nc\n", b"a\nB\nc\n")
    assert diff == "--- a/N.md\n+++ b/N.md\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    assert unified_diff("N.md", None, b"x\n") == "--- /dev/null\n+++ b/N.md\n@@ -0,0 +1 @@\n+x\n"


def test_unified_diff_marks_missing_final_newlines_and_keeps_carriage_returns() -> None:
    diff = unified_diff("N.md", b"a\r\nb", b"a\nb\n")
    assert diff == (
        "--- a/N.md\n+++ b/N.md\n@@ -1,2 +1,2 @@\n-a\r\n-b\n\\ No newline at end of file\n+a\n+b\n"
    )


def test_unified_diff_refuses_binary() -> None:
    with pytest.raises(ValueError, match="binary"):
        unified_diff("N.md", b"\0", b"x")


def _record_with(*intents: dict[str, Any], run: RunRecord | None = None) -> RunRecord:
    base = record() if run is None else run
    proposed = [
        base.intents[0].model_copy(update={"intent": intent_adapter.validate_python(i)})
        for i in intents
    ]
    return base.model_copy(update={"intents": proposed})


class _Static:
    """A sink returning a given preview (or raising)."""

    def __init__(self, preview: Preview | BaseException) -> None:
        self.result = preview

    async def preview(self, call: SinkCall) -> Preview:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def apply(self, call: SinkCall) -> Applied:
        raise AssertionError("apply must not be called")


def _file(before: bytes | None, after: bytes, path: str = DAILY) -> ChangeSet:
    run = _record_with(INTENTS[0])
    return _plan(run, _Static(FileChange(path, before, after)))


def test_file_status_sizes_and_hashes() -> None:
    (created,) = _file(None, b"x\n").files
    assert (created.status, created.before_size, created.before_sha256) == ("create", None, None)
    (same,) = _file(b"x\n", b"x\n").files
    assert (same.status, same.diff, same.binary) == ("unchanged", None, False)
    assert _file(b"x\n", b"x\n").empty
    (changed,) = _file(b"x\n", b"y\n").files
    assert changed.status == "modify"
    assert changed.after_size == 2
    assert changed.after_sha256 == hashlib.sha256(b"y\n").hexdigest()
    assert changed.before_sha256 == hashlib.sha256(b"x\n").hexdigest()


def test_binary_and_oversized_sides_get_no_diff() -> None:
    (binary,) = _file(b"a\0b", b"text\n").files
    assert binary.binary
    assert binary.diff is None
    (latin,) = _file(None, b"\xe9t\xe9").files
    assert latin.binary
    big = b"x\n" * (MAX_DIFF_BYTES // 2 + 1)
    (large,) = _file(b"", big).files
    assert large.too_large
    assert not large.binary
    assert large.diff is None
    text = render_text(_file(b"a\0b", b"text\n"))
    assert f"Binary files a/{DAILY} and b/{DAILY} differ (3 -> 5 bytes)" in text
    assert "Too large to diff" in render_text(_file(b"", big))


# --- planning ---------------------------------------------------------------------------------


def test_a_failed_or_undone_run_is_never_planned() -> None:
    sink = FixtureSink()
    for status in ("failed", "undone"):
        changes = _plan(record().model_copy(update={"status": status}), sink)
        assert changes.refused is not None
        assert changes.files == ()
        assert render_text(changes).endswith(f"Nothing to apply: {changes.refused}.\n")
    assert sink.previews == []


def test_unrouted_intents_are_reported_and_never_previewed() -> None:
    run = record()
    unrouted = run.intents[0].model_copy(
        update={"applied_by": None, "applied_by_step": None, "applied_by_call": None}
    )
    sink = FixtureSink()
    changes = _plan(run.model_copy(update={"intents": [unrouted]}), sink)
    assert [(p.kind, p.applied_by) for p in changes.problems] == [("unrouted", None)]
    assert sink.previews == []
    assert "intent 0: unrouted: the intent has no sink" in render_text(changes)


def test_intents_of_a_step_that_did_not_run_are_not_planned() -> None:
    run = record()
    steps = list(run.steps)
    steps[2] = steps[2].model_copy(update={"status": "skipped"})
    changes = _plan(run.model_copy(update={"steps": steps}), FixtureSink())
    assert {p.kind for p in changes.problems} == {"entry_skipped"}
    assert len(changes.problems) == 5
    assert len(changes.notifications) == 1


def test_missing_sinks_and_failing_previews_are_problems() -> None:
    run = record()
    sink = FixtureSink(fail={0: PreviewError("note_missing", "gone")})
    registry = StaticSinks({"vault.write_note": sink, "ntfy.push": _Static(RuntimeError("s3cr3t"))})
    changes = asyncio.run(plan_changes(run, registry))
    kinds = [(p.intent, p.kind) for p in changes.problems]
    assert kinds == [(0, "note_missing"), (1, "note_missing"), (2, "no_sink"), (3, "no_sink"),
                     (4, "no_sink"), (5, "host_error")]  # fmt: skip
    assert "s3cr3t" not in to_json(changes)


@pytest.mark.parametrize(
    ("intent", "preview"),
    [
        (INTENTS[0], FileChange("Other.assistant.md", None, b"x")),
        (INTENTS[0], Notification(None, "x")),
        (INTENTS[0], FileChange(DAILY, None, "text")),  # type: ignore[arg-type]
        (INTENTS[2], TaskChange("other", "create", None, "x")),
        (INTENTS[3], TaskChange("fp-new", "update_status", None, "done")),
        (INTENTS[4], HttpCall("https://hooks.example.org/other", {})),
        (INTENTS[5], HttpCall(HOOK, {})),
        (INTENTS[0], "not a preview"),
    ],
)
def test_a_preview_must_describe_the_routed_target(intent: dict[str, Any], preview: Any) -> None:
    changes = _plan(_record_with(intent), _Static(preview))
    assert [p.kind for p in changes.problems] == ["preview_invalid"]
    assert changes.empty


def test_overlapping_note_changes_are_flagged() -> None:
    first = dict(INTENTS[0])
    second = {**first, "content": "other\n"}
    changes = _plan(_record_with(first, second), FixtureSink())
    assert [f.overlaps for f in changes.files] == [(1,), (0,)]
    assert "intents 1 also change it" in render_text(changes)


def test_entry_params_reach_the_sink() -> None:
    sink = FixtureSink(files={INBOX: b""})
    _plan(record(), sink)
    assert sink.previews[0].params == {"path": DAILY, "mode": "replace-owned"}
    assert [c.index for c in sink.previews] == [0, 1, 2, 3, 4, 5]
    garbled = record()
    steps = list(garbled.steps)
    steps[2] = steps[2].model_copy(update={"output": None})
    sink = FixtureSink(files={INBOX: b""})
    _plan(garbled.model_copy(update={"steps": steps}), sink)
    assert sink.previews[0].params == {}


def test_notifications_carry_a_dedupe_key() -> None:
    run = record()
    seen: list[str] = []

    def notified(key: str) -> bool:
        seen.append(key)
        return True

    changes = _plan(run, FixtureSink(files={INBOX: b""}), notified=notified)
    (note,) = changes.notifications
    assert note.dedupe_key == dedupe_key(run, run.intents[5].intent) == seen[0]
    assert note.duplicate is True
    assert "(already sent, skipped)" in render_text(changes)
    hashed = run.model_copy(update={"input_hash": "a" * 64})
    assert dedupe_key(hashed, run.intents[5].intent) != note.dedupe_key
    other_run = hashed.model_copy(update={"id": "another"})
    assert dedupe_key(other_run, run.intents[5].intent) == dedupe_key(hashed, run.intents[5].intent)
    assert _plan(run, FixtureSink(files={INBOX: b""})).notifications[0].duplicate is None


# --- output -----------------------------------------------------------------------------------


def test_text_output_escapes_untrusted_control_characters() -> None:
    evil = "\x1b[2J‮boom\r"
    run = _record_with(
        {**INTENTS[0], "content": evil}, {"type": "notify.push", "title": "t\u202e", "body": evil}
    )
    text = render_text(_plan(run, FixtureSink()))
    assert "\x1b" not in text
    assert "‮" not in text
    assert "\r" not in text
    assert "\\x1b[2J\\u202eboom\\r" in text
    colored = render_text(_plan(run, FixtureSink()), color=True)
    assert "\x1b[32m+\\x1b[2J" in colored
    assert "\x1b[1m--- /dev/null\x1b[0m" in colored


def test_text_output_without_changes() -> None:
    changes = ChangeSet("r1", "p", "noop", False)
    assert render_text(changes) == "Run r1 (p, noop)\nNo changes.\n"


def test_long_http_bodies_are_shortened_in_text() -> None:
    run = _record_with({**INTENTS[4], "body": {"text": "x" * 500}})
    text = render_text(_plan(run, FixtureSink()))
    assert "(511 bytes)" in text
    assert "…" in text


def test_json_output_has_a_stable_schema() -> None:
    changes = _plan(record(), FixtureSink(files={INBOX: b"# Inbox\n"}))
    data = json.loads(to_json(changes))
    assert data == to_dict(changes)
    assert list(data) == [
        "schema_version",
        "run",
        "refused",
        "files",
        "tasks",
        "notifications",
        "http_calls",
        "problems",
    ]
    assert data["schema_version"] == 1
    assert list(data["files"][0]) == [
        "intent",
        "applied_by",
        "path",
        "status",
        "before",
        "after",
        "binary",
        "too_large",
        "overlaps",
        "diff",
    ]
    assert data["http_calls"] == [
        {"intent": 4, "applied_by": "hooks.post", "method": "POST", "url": HOOK,
         "body": {"items": 2}}
    ]  # fmt: skip
    assert data["tasks"][1] == {
        "intent": 3,
        "applied_by": "vault.tasks",
        "fingerprint": "fp-old",
        "action": "update_status",
        "before": None,
        "after": "done",
    }
    assert to_json(changes) == to_json(_plan(record(), FixtureSink(files={INBOX: b"# Inbox\n"})))
    assert to_json(changes).isascii()


# --- the built-in local sink and the fixture sink ---------------------------------------------


def _call(intent: dict[str, Any]) -> SinkCall:
    return SinkCall("r1", 0, "vault.write_note", {}, intent_adapter.validate_python(intent))


def test_local_sink_previews_notes_in_the_vault(tmp_path: Path) -> None:
    (tmp_path / "Daily").mkdir()
    sink = LocalSink(tmp_path)
    preview = asyncio.run(sink.preview(_call(INTENTS[0])))
    assert isinstance(preview, FileChange)
    assert preview.before is None
    (tmp_path / DAILY).write_bytes(b"old\n")
    preview = asyncio.run(sink.preview(_call(INTENTS[0])))
    assert isinstance(preview, FileChange)
    assert preview.before == b"old\n"
    task = asyncio.run(sink.preview(_call({**INTENTS[2], "time": "09:00"})))
    assert task == TaskChange("fp-new", "create", None, "- [ ] Review PR 42 (due 2026-10-27 09:00)")
    snooze = {"type": "task.update_status", "task": "f", "status": "snoozed", "until": "2026-11-01"}
    assert asyncio.run(sink.preview(_call(snooze))).after == "snoozed until 2026-11-01"  # type: ignore[union-attr]


def _local_error(sink: LocalSink, intent: dict[str, Any]) -> str:
    with pytest.raises(PreviewError) as caught:
        asyncio.run(sink.preview(_call(intent)))
    return caught.value.kind


def test_local_sink_refuses_what_it_must_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, outside = tmp_path / "vault", tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    os.symlink(outside, vault / "Daily")
    assert _local_error(LocalSink(vault), INTENTS[0]) == "outside_vault"
    os.unlink(vault / "Daily")
    (vault / DAILY).mkdir(parents=True)
    assert _local_error(LocalSink(vault), INTENTS[0]) == "not_a_file"
    assert _local_error(LocalSink(None), INTENTS[0]) == "vault_unset"
    (vault / INBOX).write_bytes(b"12345")
    monkeypatch.setattr(local_module, "MAX_READ_BYTES", 4)
    assert _local_error(LocalSink(vault), INTENTS[1]) == "note_too_large"
    with pytest.raises(PreviewError) as caught:
        asyncio.run(LocalSink(vault).apply(_call(INTENTS[0])))
    assert caught.value.kind == "not_supported"


def test_local_sink_size_check_also_holds_for_growing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / INBOX).write_bytes(b"1234")
    monkeypatch.setattr(local_module, "MAX_READ_BYTES", 4)
    real_stat = os.stat

    def shrunk(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        (tmp_path / INBOX).write_bytes(b"123456")
        return result

    monkeypatch.setattr(os, "stat", shrunk)
    assert _local_error(LocalSink(tmp_path), INTENTS[1]) == "note_too_large"


def test_fixture_sink_applies_in_memory_only() -> None:
    sink = FixtureSink(files={INBOX: b"# Inbox\n"}, tasks={"fp-old": "- [ ] Old"})
    applied = [asyncio.run(sink.apply(_call(intent))) for intent in INTENTS]
    assert [a.target for a in applied] == [
        DAILY,
        INBOX,
        "fp-new",
        "fp-old",
        HOOK,
        "vault.write_note",
    ]
    assert applied[1].before == b"# Inbox\n"
    assert sink.files[INBOX] == applied[1].after
    assert sink.tasks["fp-old"] == "done"
    assert sink.sent == [HttpCall(HOOK, {"items": 2}), Notification("Brief", "2 items today")]
    assert len(sink.applied) == len(INTENTS)
    assert sink.previews == []
