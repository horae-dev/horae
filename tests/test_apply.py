# SPDX-License-Identifier: Apache-2.0
"""The dry-run diff (#21): note conventions, change sets, unified diffs, text and JSON output.

Invisible and bidi characters are built with `chr()`: none appears literally in this file."""

import asyncio
import errno
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
    HttpCall,
    LocalSink,
    Notification,
    SinkCall,
    SinkError,
    StaticSinks,
    dedupe_key,
    plan_changes,
    render_text,
    to_dict,
    to_json,
    unified_diff,
)
from horae.apply import local as local_module
from horae.apply import plan as plan_module
from horae.apply.notes import note_after
from horae.apply.render import safe
from horae.apply.testing import FixtureSink
from horae.contracts import RunRecord
from horae.contracts.intents import intent_adapter

ESC, RLO, ZWSP, LS, PS, NEL = (
    chr(0x1B),
    chr(0x202E),
    chr(0x200B),
    chr(0x2028),
    chr(0x2029),
    chr(0x85),
)


def _plan(run: RunRecord, sink: Any, **kwargs: Any) -> ChangeSet:
    return asyncio.run(plan_changes(run, StaticSinks(default=sink), **kwargs))


def _note(**fields: Any) -> Any:
    return intent_adapter.validate_python(fields)


def _kind(intent: Any, before: bytes | None) -> str:
    with pytest.raises(SinkError) as caught:
        note_after(intent, before)
    return caught.value.kind


# --- note conventions -------------------------------------------------------------------------


def test_note_create_refuses_an_existing_note_and_adds_a_final_newline() -> None:
    intent = _note(type="note.create", path="A.assistant.md", content="hi")
    assert note_after(intent, None) == b"hi\n"
    assert _kind(intent, b"x") == "note_exists"


def test_only_owned_notes_are_created_or_appended_to() -> None:
    for extra in ({"type": "note.create"}, {"type": "note.append_section", "heading": "H"}):
        intent = _note(path="Mine.md", content="x", **extra)
        assert _kind(intent, b"") == "not_owned"
    # Ownership is the file name's, not a directory's.
    intent = _note(type="note.create", path="x.assistant.md/Mine.md", content="x")
    assert _kind(intent, None) == "not_owned"


def test_append_section_needs_the_note() -> None:
    intent = _note(type="note.append_section", path=INBOX, heading="H", content="body\n")
    assert _kind(intent, None) == "note_missing"
    assert note_after(intent, b"") == b"## H\n\nbody\n"
    assert note_after(intent, b"top") == b"top\n\n## H\n\nbody\n"


def test_append_section_refuses_binary_notes() -> None:
    intent = _note(type="note.append_section", path=INBOX, heading="H", content="b")
    assert _kind(intent, b"\xff\xfe") == "note_not_text"


def _block(content: str = "new", path: str = "Any.assistant.md") -> Any:
    return _note(type="note.replace_owned_block", path=path, block="b", content=content)


BEGIN, END = "<!-- horae:begin b -->", "<!-- horae:end b -->"


def test_replace_owned_block_replaces_creates_and_appends_in_owned_notes() -> None:
    assert note_after(_block(), None).decode() == f"{BEGIN}\nnew\n{END}\n"
    assert note_after(_block(), b"top").decode() == f"top\n\n{BEGIN}\nnew\n{END}\n"
    at_end = f"{BEGIN}\nold\n{END}".encode()
    assert note_after(_block(), at_end).decode() == f"{BEGIN}\nnew\n{END}\n"
    spaced = f"x\n  {BEGIN} \nold\n{END}\ny\n".encode()
    assert note_after(_block(), spaced).decode() == f"x\n{BEGIN}\nnew\n{END}\ny\n"
    other = f"<!-- horae:begin c -->\n{BEGIN}\nold\n{END}\n".encode()
    assert note_after(_block(), other).decode() == f"<!-- horae:begin c -->\n{BEGIN}\nnew\n{END}\n"


def test_a_begin_marker_may_end_the_note() -> None:
    with pytest.raises(SinkError) as caught:
        note_after(_block(), f"{END}\n{BEGIN}".encode())
    assert caught.value.kind == "block_ambiguous"
    assert _kind(_block(), f"top\n{BEGIN}".encode()) == "block_ambiguous"  # no end: not a pair
    crlf_end = f"{BEGIN}\r\nold\r\n{END}".encode()
    assert note_after(_block(), crlf_end).decode() == f"{BEGIN}\r\nnew\r\n{END}\r\n"


def test_crlf_notes_get_crlf_lines() -> None:
    text = f"top\r\n{BEGIN}\r\nold\r\n{END}\r\ntail\r\n".encode()
    assert note_after(_block("a\nb"), text).decode() == (
        f"top\r\n{BEGIN}\r\na\r\nb\r\n{END}\r\ntail\r\n"
    )
    assert note_after(_block("a\r\nb\r\n"), b"top\r\n").decode() == (
        f"top\r\n\r\n{BEGIN}\r\na\r\nb\r\n{END}\r\n"
    )
    append = _note(type="note.append_section", path=INBOX, heading="H", content="x\ny")
    assert (
        note_after(append, b"# Inbox\r\n- a\r\n") == b"# Inbox\r\n- a\r\n\r\n## H\r\n\r\nx\r\ny\r\n"
    )
    assert note_after(append, b"one line") == b"one line\n\n## H\n\nx\ny\n"


def test_horae_never_creates_content_in_a_note_it_does_not_own() -> None:
    user = "Journal.md"
    assert _kind(_block(path=user), None) == "not_owned"
    assert _kind(_block(path=user), b"# My notes\n") == "not_owned"
    existing = f"# Mine\n{BEGIN}\nold\n{END}\n".encode()
    assert note_after(_block(path=user), existing).decode() == f"# Mine\n{BEGIN}\nnew\n{END}\n"


@pytest.mark.parametrize(
    "text",
    [
        f"{BEGIN}\n{BEGIN}\n{END}\n",
        f"{END}\n{BEGIN}\n",
        f"{BEGIN}\nno end\n",
        f"<!--horae:begin b-->\nold\n{END}\n",
        f"<!-- HORAE:BEGIN b -->\nold\n{END}\n",
        f"{BEGIN}\nold\n{END} trailing\n",
        f"{BEGIN} {END}\n",
    ],
)
def test_replace_owned_block_refuses_ambiguous_or_malformed_markers(text: str) -> None:
    assert _kind(_block(), text.encode()) == "block_ambiguous"


@pytest.mark.parametrize(
    "content",
    [
        f"x\n{END}\nevil",
        "<!-- HORAE:END b -->",
        "<!--horae:end b-->",
        "  <!-- horae:begin b -->  ",
        "inline horae:end b in prose",
        "horae:BEGIN other",
        "a lone horae:begin",
        f"x\t{END}",
    ],
)
def test_untrusted_content_cannot_hold_a_marker(content: str) -> None:
    assert _kind(_block(content), None) == "content_has_marker"
    create = _note(type="note.create", path="N.assistant.md", content=content)
    assert _kind(create, None) == "content_has_marker"
    append = _note(type="note.append_section", path=INBOX, heading="H", content=content)
    assert _kind(append, b"") == "content_has_marker"


def test_untrusted_headings_cannot_hold_a_marker_or_a_line_break() -> None:
    for heading in ("horae:end b", f"H{LS}x"):
        append = _note(type="note.append_section", path=INBOX, heading=heading, content="x")
        assert _kind(append, b"") in {"content_has_marker", "content_line_break"}


@pytest.mark.parametrize("separator", ["\r", NEL, LS, PS, "a\rb"])
def test_untrusted_content_cannot_break_lines_ambiguously(separator: str) -> None:
    smuggled = f"x{separator}{END.replace('horae', 'h')}"
    assert _kind(_block(smuggled), None) == "content_line_break"
    assert note_after(_block("a\r\nb"), None).decode() == f"{BEGIN}\na\nb\n{END}\n"


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
    """A sink whose read returns a given value (or raises); apply must never be called."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.reads = 0

    async def read(self, call: SinkCall) -> bytes | None:
        self.reads += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        raise AssertionError("apply must not be called")


def _file(before: bytes | None, content: str = "x") -> ChangeSet:
    return _plan(_record_with({**INTENTS[0], "content": content}), _Static(before))


def test_file_status_sizes_and_hashes() -> None:
    new = b"<!-- horae:begin horae-brief -->\nx\n<!-- horae:end horae-brief -->\n"
    (created,) = _file(None).files
    assert (created.status, created.before_size, created.before_sha256) == ("create", None, None)
    (same,) = _file(new).files
    assert (same.status, same.diff, same.binary) == ("unchanged", None, False)
    assert _file(new).empty
    (changed,) = _file(new, "y").files
    assert changed.status == "modify"
    assert changed.after_size == len(new)
    assert changed.after_sha256 == hashlib.sha256(new.replace(b"x", b"y")).hexdigest()
    assert changed.before_sha256 == hashlib.sha256(new).hexdigest()


def test_binary_and_oversized_sides_get_no_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    (binary,) = _file(b"a\0b").files
    assert binary.binary
    assert binary.diff is None
    text = render_text(_file(b"a\0b"))
    assert f"Binary files a/{DAILY} and b/{DAILY} differ (3 -> " in text
    big = b"x\n" * (MAX_DIFF_BYTES // 2 + 1)
    (large,) = _file(big).files
    assert large.too_large
    assert not large.binary
    assert large.diff is None
    assert "Too large to diff" in render_text(_file(big))
    monkeypatch.setattr(plan_module, "_text", lambda data: None)
    assert f"Binary files /dev/null and b/{DAILY} differ" in render_text(_file(None))


def test_sink_reads_are_type_and_size_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    for wrong in ("text", bytearray(b"x"), 3, memoryview(b"x")):
        changes = _file(wrong)  # type: ignore[arg-type]
        assert [p.kind for p in changes.problems] == ["preview_invalid"]
    monkeypatch.setattr(plan_module, "MAX_NOTE_BYTES", 4)
    assert [p.kind for p in _file(b"12345").problems] == ["note_too_large"]
    assert [p.kind for p in _file(None, "a long content").problems] == ["note_too_large"]


# --- planning ---------------------------------------------------------------------------------


def test_failed_undone_and_applied_runs_are_refused() -> None:
    sink = FixtureSink()
    for status, kind in (("failed", "run_failed"), ("undone", "run_undone")):
        changes = _plan(record().model_copy(update={"status": status}), sink)
        assert changes.refused is not None
        assert changes.refused.kind == kind
        assert changes.files == ()
        assert render_text(changes).endswith(f"({kind}).\n")
    run = record(dry_run=False)
    change = {"intent": 0, "target": DAILY}
    applied = RunRecord.model_validate({**run.model_dump(), "changes": [change]})
    changes = _plan(applied, sink)
    assert changes.refused is not None
    assert changes.refused.kind == "already_applied"
    assert "#22" in changes.refused.message
    assert to_dict(changes)["refused"]["kind"] == "already_applied"
    assert sink.reads == []


def test_a_degraded_run_is_planned() -> None:
    changes = _plan(record().model_copy(update={"status": "degraded"}), FixtureSink())
    assert changes.refused is None
    assert changes.tasks


def test_unrouted_intents_are_reported_and_never_read() -> None:
    run = record()
    unrouted = run.intents[0].model_copy(
        update={"applied_by": None, "applied_by_step": None, "applied_by_call": None}
    )
    sink = FixtureSink()
    changes = _plan(run.model_copy(update={"intents": [unrouted]}), sink)
    assert [(p.kind, p.applied_by) for p in changes.problems] == [("unrouted", None)]
    assert sink.reads == []
    assert "intent 0: unrouted: the intent has no sink" in render_text(changes)


def test_intents_of_a_step_that_did_not_run_are_not_planned() -> None:
    run = record()
    steps = list(run.steps)
    steps[2] = steps[2].model_copy(update={"status": "skipped"})
    changes = _plan(run.model_copy(update={"steps": steps}), FixtureSink())
    assert {p.kind for p in changes.problems} == {"entry_skipped"}
    assert len(changes.problems) == 5
    assert len(changes.notifications) == 1


def test_missing_sinks_and_failing_reads_are_problems() -> None:
    run = record()
    sink = FixtureSink(fail={0: SinkError("unreadable", "gone")})
    registry = StaticSinks(
        {"vault.write_note": sink, "ntfy.push": _Static(None), "hooks.post": _Static(None)}
    )
    changes = asyncio.run(plan_changes(run, registry))
    kinds = [(p.intent, p.kind) for p in changes.problems]
    assert kinds == [(0, "unreadable"), (1, "note_missing"), (2, "no_sink"), (3, "no_sink")]
    crash = _plan(_record_with(INTENTS[0]), _Static(RuntimeError("s3cr3t")))
    assert [p.kind for p in crash.problems] == ["host_error"]
    assert "s3cr3t" not in to_json(crash)


def test_previews_are_built_by_the_core_without_calling_the_sink() -> None:
    sink = _Static(AssertionError("never read"))
    changes = _plan(_record_with(*INTENTS[2:]), sink)
    assert sink.reads == 0
    assert changes.problems == ()
    assert [t.change.after for t in changes.tasks] == [
        "- [ ] Review PR 42 (due 2026-10-27)",
        "done",
    ]
    assert changes.http_calls[0].call == HttpCall(HOOK, {"items": 2})
    assert changes.notifications[0].notification == Notification("Brief", "2 items today")
    timed = _record_with({**INTENTS[2], "time": "09:00"})
    assert _plan(timed, sink).tasks[0].change.after.endswith("(due 2026-10-27 09:00)")
    snooze = {"type": "task.update_status", "task": "f", "status": "snoozed", "until": "2026-11-01"}
    assert _plan(_record_with(snooze), sink).tasks[0].change.after == "snoozed until 2026-11-01"


def test_a_sink_cannot_change_the_plan_through_its_call() -> None:
    run = record()

    class Meddler(FixtureSink):
        async def read(self, call: SinkCall) -> bytes | None:
            call.params["path"] = "Other.md"
            call.intent.content = "tampered"  # type: ignore[union-attr]
            return None

    changes = _plan(run, Meddler(files={INBOX: b""}))
    assert run.intents[0].intent.content == INTENTS[0]["content"]  # type: ignore[union-attr]
    assert "tampered" not in (changes.files[0].diff or "")
    assert plan_module.entry_params(run, run.intents[0])["path"] == DAILY


def test_overlapping_note_changes_are_chained() -> None:
    first = dict(INTENTS[0])
    second = {**first, "content": "other\n"}
    sink = FixtureSink()
    changes = _plan(_record_with(first, second), sink)
    one, two = changes.files
    assert len(sink.reads) == 1
    assert (one.follows, two.follows) == (None, 0)
    assert (one.status, two.status) == ("create", "modify")
    assert two.before_sha256 == one.after_sha256
    assert two.diff is not None
    assert "-- [ ] Review PR 42\n+other\n" in two.diff
    assert "after intent 0" in render_text(changes)
    # Applying in run order with the planned compare-and-swap reproduces the plan.
    for item in changes.files:
        call = SinkCall(
            "r", item.intent, "t", {}, _record_with(first, second).intents[item.intent].intent
        )
        applied = asyncio.run(sink.apply(call, expected_before_sha256=item.before_sha256))
        assert applied.after is not None
        assert hashlib.sha256(applied.after).hexdigest() == item.after_sha256


def test_a_failed_intent_does_not_change_what_the_next_one_sees() -> None:
    bad = {"type": "note.create", "path": DAILY, "content": "x"}
    changes = _plan(_record_with(INTENTS[0], bad, INTENTS[0]), FixtureSink())
    assert [p.kind for p in changes.problems] == ["note_exists"]
    assert [f.follows for f in changes.files] == [None, 0]
    assert changes.files[1].status == "unchanged"


def test_entry_params_reach_the_sink() -> None:
    sink = FixtureSink(files={INBOX: b""})
    _plan(record(), sink)
    assert sink.reads[0].params == {"path": DAILY, "mode": "replace-owned"}
    assert [c.index for c in sink.reads] == [0, 1]
    garbled = record()
    steps = list(garbled.steps)
    steps[2] = steps[2].model_copy(update={"output": None})
    sink = FixtureSink(files={INBOX: b""})
    _plan(garbled.model_copy(update={"steps": steps}), sink)
    assert sink.reads[0].params == {}


def test_notifications_carry_a_dedupe_key() -> None:
    run = record()
    seen: list[str] = []

    def notified(key: str) -> bool:
        seen.append(key)
        return True

    changes = _plan(run, FixtureSink(files={INBOX: b""}), notified=notified)
    (note,) = changes.notifications
    assert note.dedupe_key == dedupe_key(run, 5) == seen[0]
    assert note.duplicate is True
    assert changes.problems == ()
    assert "(already sent, skipped)" in render_text(changes)
    hashed = run.model_copy(update={"input_hash": "a" * 64})
    assert dedupe_key(hashed, 5) != note.dedupe_key
    other_run = hashed.model_copy(update={"id": "another"})
    assert dedupe_key(other_run, 5) == dedupe_key(hashed, 5)
    assert dedupe_key(hashed.model_copy(update={"pipeline": "other"}), 5) != dedupe_key(hashed, 5)
    assert _plan(run, FixtureSink(files={INBOX: b""})).notifications[0].duplicate is None


def test_identical_notifications_of_one_run_get_distinct_keys() -> None:
    run = record()
    twice = run.model_copy(update={"intents": [*run.intents, run.intents[5]]})
    assert dedupe_key(twice, 5) != dedupe_key(twice, 6)
    assert dedupe_key(twice, 5) == dedupe_key(run, 5)
    steps = list(run.steps)
    output = [{**steps[3].output[0], "params": {"when": "08:00"}}]
    steps[3] = steps[3].model_copy(update={"output": output})
    assert dedupe_key(run.model_copy(update={"steps": steps}), 5) != dedupe_key(run, 5)


# --- output -----------------------------------------------------------------------------------


def test_safe_escapes_every_control_format_and_separator_character() -> None:
    assert safe("a\tb") == "a\tb"
    assert safe(f"{ESC}[2J{RLO}{ZWSP}{LS}{PS}{NEL}\r\n\x7f") == (
        "\\x1b[2J\\u202e\\u200b\\u2028\\u2029\\x85\\r\\n\\x7f"
    )
    assert safe(chr(0xE0001)) == "\\U000e0001"
    assert safe("café — ok") == "café — ok"


def test_text_output_escapes_untrusted_control_characters() -> None:
    evil = f"{ESC}[2J{RLO}boom{ZWSP}"
    run = _record_with(
        {**INTENTS[0], "content": evil}, {"type": "notify.push", "title": f"t{RLO}", "body": evil}
    )
    text = render_text(_plan(run, FixtureSink()))
    for char in (ESC, RLO, ZWSP):
        assert char not in text
    assert "\\x1b[2J\\u202eboom\\u200b" in text
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


def test_json_output_is_strict_even_with_non_finite_numbers() -> None:
    run = _record_with({**INTENTS[4], "body": {"n": 1}})
    changes = _plan(run, FixtureSink())
    call = changes.http_calls[0]
    nan = HttpCall(HOOK, {"n": float("nan"), "l": [float("inf")]})
    patched = ChangeSet(**{**changes.__dict__, "http_calls": (type(call)(4, "h", nan),)})
    data = json.loads(to_json(patched))
    assert data["http_calls"][0]["body"] == {"n": "NaN", "l": ["Infinity"]}
    assert "NaN" not in to_json(changes)
    assert '{"n":"NaN","l":["Infinity"]}' in render_text(patched)


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
    assert data["refused"] is None
    assert list(data["files"][0]) == [
        "intent",
        "applied_by",
        "path",
        "status",
        "before",
        "after",
        "binary",
        "too_large",
        "follows",
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


def _read(sink: LocalSink, intent: dict[str, Any] = INTENTS[0]) -> bytes | None:
    return asyncio.run(sink.read(_call(intent)))


def _local_error(sink: LocalSink, intent: dict[str, Any] = INTENTS[0]) -> str:
    with pytest.raises(SinkError) as caught:
        _read(sink, intent)
    return caught.value.kind


def test_local_sink_reads_notes_in_the_vault(tmp_path: Path) -> None:
    sink = LocalSink(tmp_path)
    assert _read(sink) is None  # no `Daily/`
    (tmp_path / "Daily").mkdir()
    assert _read(sink) is None
    (tmp_path / DAILY).write_bytes(b"old\n")
    assert _read(sink) == b"old\n"
    assert _local_error(sink, INTENTS[2]) == "not_supported"
    with pytest.raises(SinkError) as caught:
        asyncio.run(sink.apply(_call(INTENTS[0]), expected_before_sha256=None))
    assert caught.value.kind == "not_supported"
    assert _local_error(LocalSink(None)) == "vault_unset"
    assert _local_error(LocalSink(tmp_path / "missing")) == "unreadable"


def test_local_sink_never_follows_symlinks(tmp_path: Path) -> None:
    vault, outside = tmp_path / "vault", tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (outside / "2026-10-27.assistant.md").write_text("secret")
    os.symlink(outside, vault / "Daily")
    assert _local_error(LocalSink(vault)) == "symlink"
    os.unlink(vault / "Daily")
    (vault / "Daily").mkdir()
    (vault / "Daily" / "user.md").write_text("mine")
    os.symlink("user.md", vault / DAILY)  # a link cannot make a user note look owned
    assert _local_error(LocalSink(vault)) == "symlink"


def test_local_sink_refuses_what_is_not_a_single_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / DAILY).mkdir(parents=True)
    assert _local_error(LocalSink(tmp_path)) == "not_a_file"
    os.rmdir(tmp_path / DAILY)
    os.mkfifo(tmp_path / DAILY)
    assert _local_error(LocalSink(tmp_path)) == "not_a_file"  # O_NONBLOCK: no hang
    os.unlink(tmp_path / DAILY)
    (tmp_path / "Daily.md").write_text("x")
    nested = {**INTENTS[0], "path": "Daily.md/x.assistant.md"}
    assert _local_error(LocalSink(tmp_path), nested) == "not_a_file"
    (tmp_path / "user.md").write_text("mine")
    os.link(tmp_path / "user.md", tmp_path / DAILY)
    assert _local_error(LocalSink(tmp_path)) == "hard_link"
    os.unlink(tmp_path / DAILY)
    (tmp_path / INBOX).write_bytes(b"12345")
    monkeypatch.setattr(local_module, "MAX_NOTE_BYTES", 4)
    assert _local_error(LocalSink(tmp_path), INTENTS[1]) == "note_too_large"
    colon = {**INTENTS[0], "path": "C:x.assistant.md"}
    assert _local_error(LocalSink(tmp_path), colon) == "path_invalid"


def test_local_sink_reports_unreadable_notes(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    (tmp_path / INBOX).write_text("x")
    os.chmod(tmp_path / INBOX, 0)
    try:
        assert _local_error(LocalSink(tmp_path), INTENTS[1]) == "unreadable"
    finally:
        os.chmod(tmp_path / INBOX, 0o600)


@pytest.mark.parametrize(
    ("code", "kind"), [(errno.ENAMETOOLONG, "not_a_file"), (errno.EIO, "unreadable")]
)
def test_local_sink_maps_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, kind: str
) -> None:
    real_open = os.open

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if "dir_fd" in kwargs:
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    assert _local_error(LocalSink(tmp_path)) == kind


def test_local_sink_size_check_also_holds_for_growing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / INBOX).write_bytes(b"1234")
    monkeypatch.setattr(local_module, "MAX_NOTE_BYTES", 4)
    real_fstat = os.fstat

    def grown(fd: int) -> Any:
        result = real_fstat(fd)
        (tmp_path / INBOX).write_bytes(b"123456")
        return result

    monkeypatch.setattr(os, "fstat", grown)
    assert _local_error(LocalSink(tmp_path), INTENTS[1]) == "note_too_large"


def test_fixture_sink_applies_in_memory_with_compare_and_swap() -> None:
    run = record()
    sink = FixtureSink(files={INBOX: b"# Inbox\n"}, tasks={"fp-old": "- [ ] Old"})
    expected = [None, hashlib.sha256(b"# Inbox\n").hexdigest(), None, None, None, None]
    applied = []
    for index, intent in enumerate(INTENTS):
        call = SinkCall("r1", index, "t", {}, intent_adapter.validate_python(intent))
        if index == 5:
            call = SinkCall(
                "r1", index, "ntfy.push", {}, call.intent, dedupe_key=dedupe_key(run, 5)
            )
        applied.append(asyncio.run(sink.apply(call, expected_before_sha256=expected[index])))
    assert [a.target for a in applied] == [DAILY, INBOX, "fp-new", "fp-old", HOOK, "ntfy.push"]
    assert applied[1].before == b"# Inbox\n"
    assert sink.files[INBOX] == applied[1].after
    assert sink.tasks["fp-old"] == "done"
    assert sink.sent == [HttpCall(HOOK, {"items": 2}), Notification("Brief", "2 items today")]
    assert sink.dedupe_keys == [dedupe_key(run, 5)]
    assert sink.reads == []
    with pytest.raises(SinkError) as caught:
        asyncio.run(sink.apply(_call(INTENTS[1]), expected_before_sha256=expected[1]))
    assert caught.value.kind == "target_changed"
    fail = FixtureSink(fail={0: SinkError("boom", "x")})
    with pytest.raises(SinkError):
        asyncio.run(fail.apply(_call(INTENTS[0]), expected_before_sha256=None))
