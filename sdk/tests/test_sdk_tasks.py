# SPDX-License-Identifier: Apache-2.0
"""Obsidian Tasks title escaping (threat model T02, #42): builders and Tasks sinks."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from horae.contracts import Permissions, TaskCreate
from horae.intents import validate_intents
from horae_sdk import intents, tasks
from horae_sdk.tasks import MARKDOWN, SIGNIFIERS, escape_title, task_line, task_line_for
from sdk_helpers import DUE, HOSTILE, REMINDER, signifiers_in


def unescaped_markup(text: str) -> list[str]:
    """Markdown characters of `text` not preceded by an escaping backslash."""
    found = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == "&" and re.match(r"&#x[0-9A-F]{4,5};", text[index:]):
            index = text.index(";", index) + 1
            continue
        if char in MARKDOWN:
            found.append(char)
        index += 1
    return found


def test_a_title_with_every_signifier_and_markdown_is_inert() -> None:
    escaped = escape_title(HOSTILE)
    assert signifiers_in(escaped) == []
    assert unescaped_markup(escaped) == []
    assert "::" not in escaped
    assert tasks.unescape_title(escaped) == HOSTILE  # a reader still sees the same text


def test_the_intent_builder_escapes_and_the_core_accepts_it() -> None:
    intent = intents.task_create(HOSTILE, fingerprint="mail:42", due="2026-10-22", time="19:00")
    assert signifiers_in(intent["title"]) == []
    assert unescaped_markup(intent["title"]) == []
    (typed,) = validate_intents(
        [intent], permissions=Permissions(intents=["task.create"]), source="sdk"
    )
    assert isinstance(typed, TaskCreate)
    assert typed.title == intent["title"]


def test_a_tasks_sink_line_holds_only_its_own_fields() -> None:
    raw = intents.task_create(HOSTILE, fingerprint="mail:42", escape=False)
    assert signifiers_in(raw["title"])  # unescaped: the sink must escape it
    built = intents.task_create(HOSTILE, fingerprint="mail:42", due="2026-10-22", time="19:00")
    for intent in (raw, {**built}):
        line = task_line_for({**intent, "due": "2026-10-22", "time": "19:00"})
        assert line.startswith("- [ ] ")
        assert signifiers_in(line) == [DUE, REMINDER]
        assert line.endswith(f" {DUE} 2026-10-22 {REMINDER} 19:00")
    # escaping twice is escaping once: the builder's title and the raw one give the same line
    assert task_line_for({**raw, "due": "2026-10-22"}) == task_line_for({**built, "time": None})


def test_task_lines() -> None:
    assert task_line("Buy milk") == "- [ ] Buy milk"
    assert task_line("Done", done=True) == "- [x] Done"
    assert task_line("a*b", due="2026-10-22") == f"- [ ] a\\*b {DUE} 2026-10-22"
    for bad in ({"due": "22/10/2026"}, {"time": "19:00"}, {"due": "2026-10-22", "time": "7pm"}):
        with pytest.raises(ValueError, match="must be"):
            task_line("x", **bad)  # type: ignore[arg-type]
    for intent in (
        {"type": "note.create"},
        {"type": "task.create"},
        {"type": "task.create", "title": "x", "due": 1},
    ):
        with pytest.raises(ValueError, match=r"task\.create|title|strings"):
            task_line_for(intent)


def test_lines_controls_and_variation_selectors() -> None:
    assert escape_title(f"a\nb\tc\u2028d\u202ee\u200bf  {DUE}\ufe0f ") == "a b c def &#x1F4C5;"
    assert escape_title("x::y:::z") == "x:\\:y:\\:\\:z"
    with pytest.raises(ValueError, match="empty"):
        escape_title("\u202e\n\t")
    with pytest.raises(intents.IntentError, match="empty"):
        intents.task_create("\u200b", fingerprint="x")


def test_long_titles_are_cut_on_an_escape_boundary() -> None:
    escaped = escape_title(DUE * 400)
    assert len(escaped) <= 500
    assert escaped.endswith("…")
    assert re.fullmatch(r"(&#x1F4C5;)+…", escaped)
    assert len(intents.task_create("x" * 600, fingerprint="f")["title"]) == 500


def _check_inert(title: str) -> None:
    try:
        once = escape_title(title)
    except ValueError:
        return
    assert escape_title(once) == once
    assert signifiers_in(once) == []
    assert unescaped_markup(once) == []
    assert not re.search(r"[\x00-\x1f\x7f]", once)


@given(
    st.text(
        alphabet=st.sampled_from(
            [*SIGNIFIERS, *MARKDOWN, "a", " ", ":", "\\", "&", "#", "x", "1", ";"]
        ),
        max_size=80,
    )
)
def test_escaping_hostile_alphabets(title: str) -> None:
    _check_inert(title)


@given(st.text(max_size=300))
def test_escaping_is_idempotent_and_inert(title: str) -> None:
    _check_inert(title)
