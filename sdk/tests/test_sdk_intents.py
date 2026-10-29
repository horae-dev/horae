# SPDX-License-Identifier: Apache-2.0
"""Intent builders produce intents the core accepts (#42): its validator and JSON Schemas."""

import datetime
from typing import Any

import pytest

from horae.contracts import INTENT_TYPES, Permissions
from horae.intents import validate_intents
from horae_sdk import intents
from horae_sdk.intents import IntentError
from sdk_helpers import schema_errors

UTC = datetime.UTC


def every_intent() -> list[dict[str, Any]]:
    return [
        intents.note_create("# Brief\n", path="Daily/2026-10-22.assistant.md"),
        intents.note_append_section("Evening", "- done\n", path="Daily/x.md"),
        intents.note_replace_owned_block("horae-brief", "text", path="Daily/x.md"),
        intents.task_create(
            "Call the school", fingerprint="school:1", due=datetime.date(2026, 10, 22), time="08:30"
        ),
        intents.task_update_status("school:1", "done"),
        intents.task_update_status(
            "school:1", "snoozed", until=datetime.datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
        ),
        intents.task_update_status("school:1", "snoozed", until="2026-10-24"),
        intents.notify_push("Bins tonight", title="Reminder", link="obsidian://open?vault=v"),
        intents.http_post("https://hooks.example.org/x", {"text": "hello", "n": 1}),
    ]


def test_every_builder_makes_an_intent_the_core_accepts() -> None:
    built = every_intent()
    assert {intent["type"] for intent in built} == set(INTENT_TYPES) == set(intents.INTENT_TYPES)
    permissions = Permissions(intents=list(INTENT_TYPES), network=["hooks.example.org"])
    typed = validate_intents(built, permissions=permissions, source="sdk")
    assert [intent.model_dump(mode="json", exclude_none=True) for intent in typed] == built
    for intent in built:
        assert schema_errors(f"intents/{intent['type']}/v1.json", intent) == []
        assert schema_errors("intent/v1.json", intent) == []


def test_a_note_intent_may_leave_its_path_to_the_act_entry() -> None:
    assert intents.note_create("x") == {"type": "note.create", "content": "x"}


def test_fingerprints() -> None:
    assert intents.fingerprint("github", "pr", "42") == "github:pr:42"
    assert intents.fingerprint("mail", "<id@example.org>") == "mail:-id-example.org-"
    long = intents.fingerprint("calendar", "x" * 300)
    assert long.startswith("calendar:sha256:")
    assert len(long) <= 128
    with pytest.raises(IntentError):
        intents.fingerprint()
    for value in (long, intents.fingerprint("a b")):
        intents.task_create("t", fingerprint=value)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: intents.note_create("x", path="/abs.md"), "path"),
        (lambda: intents.note_create("x", path="a/.obsidian/x.md"), "path"),
        (lambda: intents.note_create("x" * (1024 * 1024 + 1)), "content"),
        (lambda: intents.note_append_section("a\nb", "x"), "heading"),
        (lambda: intents.note_replace_owned_block("Bad_Block", "x"), "block"),
        (lambda: intents.task_create("t", fingerprint="no spaces"), "fingerprint"),
        (lambda: intents.task_create("t", fingerprint="f", time="08:00"), "requires due"),
        (lambda: intents.task_create("t", fingerprint="f", due="2026-10-22", time="8h"), "HH:MM"),
        (lambda: intents.task_create("t", fingerprint="f", due="tomorrow"), "due"),
        (lambda: intents.task_create("a\nb", fingerprint="f", escape=False), "title"),
        (lambda: intents.task_create(1, fingerprint="f"), "title"),  # type: ignore[arg-type]
        (lambda: intents.task_update_status("bad id", "done"), "task"),
        (lambda: intents.task_update_status("f", "later"), "status"),  # type: ignore[arg-type]
        (lambda: intents.task_update_status("f", "snoozed"), "until"),
        (lambda: intents.task_update_status("f", "done", until="2026-10-22"), "until"),
        (lambda: intents.task_update_status("f", "snoozed", until="soon"), "until"),
        (
            lambda: intents.task_update_status("f", "snoozed", until=datetime.datetime(2026, 1, 1)),
            "time zone",
        ),
        (lambda: intents.notify_push(""), "body"),
        (lambda: intents.notify_push("x", title="a\nb"), "title"),
        (lambda: intents.notify_push("x", link="javascript:alert(1)"), "link"),
        (lambda: intents.http_post("http://example.org", {}), "https"),
    ],
)
def test_builders_refuse_what_the_core_would(build: Any, message: str) -> None:
    with pytest.raises(IntentError, match=message):
        build()
