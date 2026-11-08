# SPDX-License-Identifier: Apache-2.0
"""Intent builders produce intents the core accepts (#42): its validator and JSON Schemas."""

import datetime
import re
from typing import Any

import pytest

from horae.contracts import INTENT_TYPES, Permissions, TaskCreate
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
            "Call the school", key="school:1", due=datetime.date(2026, 10, 22), time="08:30"
        ),
        intents.task_create(
            "Review PR 42", key="github:pr:42", link="https://code.example.org/acme/pull/42#c-7"
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
    # a clean fingerprint is the readable form, unchanged (existing recordings stay stable)
    assert intents.fingerprint("github", "pr", "42") == "github:pr:42"
    assert intents.fingerprint("hello", "Ada") == "hello:Ada"
    lossy = intents.fingerprint("mail", "<id@example.org>")
    assert re.fullmatch(r"mail:-id-example\.org-::[0-9a-f]{16}", lossy)
    long = intents.fingerprint("calendar", "x" * 300)
    assert re.fullmatch(r"calendar:x{101}::[0-9a-f]{16}", long)
    assert len(long) == intents.MAX_FINGERPRINT
    with pytest.raises(IntentError):
        intents.fingerprint()
    with pytest.raises(IntentError):
        intents.fingerprint("a", 1)  # type: ignore[arg-type]
    for value in (lossy, long, intents.fingerprint("a b"), intents.fingerprint("a", "")):
        intents.task_create("t", key=value)
        assert (
            schema_errors(
                "intents/task.create/v1.json",
                {"type": "task.create", "title": "t", "fingerprint": value},
            )
            == []
        )


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (("a:b",), ("a-b",)),
        (("a:b",), ("a", "b")),
        (("mail", "Re: x!"), ("mail", "Re: x?")),
        (("a", ""), ("a:",)),
        (("a", ""), ("a",)),
        (("x" * 100, "y" * 100), ("x" * 100 + ":" + "y" * 100,)),
        (("x" * 100, "y" * 100), ("x" * 100, "y" * 101)),
        (("p", "a,b"), ("p", "a", "b")),
    ],
)
def test_lossy_fingerprints_do_not_collide(one: tuple[str, ...], other: tuple[str, ...]) -> None:
    assert intents.fingerprint(*one) != intents.fingerprint(*other)


def test_an_exact_fingerprint_never_looks_like_a_digested_one() -> None:
    lossy = intents.fingerprint("a", "b!")
    head, digest = lossy.split("::")
    # the same text built from clean parts is refused as exact (an empty part) and digested
    assert intents.fingerprint(*f"{head}::{digest}".split(":")) != lossy
    assert "::" not in intents.fingerprint("a", "b", "c")


BAD_TASK_LINKS: list[Any] = [
    "http://code.example.org/acme/pull/42",
    "javascript:alert(1)",
    "data:text/html,x",
    "obsidian://open?vault=Main",
    "https://user@code.example.org/acme/",
    "https://user:pw@code.example.org/acme/",
    "https://code.example.org/a b",
    "https://code.example.org\\@evil.example/",
    "https:///acme",
    "https://",
    "https://code.example.org/" + "a" * 2030,
    "https://code.example.org/a\nb",  # not last: Python jsonschema `$` (schemas README)
    7,
]


# Refused by the model and the SDK, not by the JSON Schema pattern (schemas README, "Rules only
# the pydantic models check"): IP-literal hosts and supplementary-plane format characters.
MODEL_ONLY_BAD_TASK_LINKS = [
    "https://127.0.0.1/acme/",
    "https://127.0.0.1:443/acme/",
    "https://10.0.0.1./acme/",
    "https://127.1/",
    "https://2130706433/x",
    "https://0x7f.1/x",
    "https://0X7F.0.0.1/x",
    "https://0x/x",
    "https://evil.example.42/x",  # ends in a number: a browser reads an IPv4 address
    "https://\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11/x",  # full-width
    "https://1\u30022\u30023\u30024/x",  # ideographic full stops
    "https://:443/x",  # no host
    "https://code.example.org/a\U000e0041b",  # a tag character (Cf)
    "https://code.example.org/a\U0001d173",
    "https://code.example.org/a\ud800",  # a lone surrogate
]
SCHEMA_BAD_HOSTS = [
    "https://[::1]/acme/",
    "https://[2001:db8::1]:443/x",
    "https://[v1.x]/x",
    "https://code%2Eexample.org/x",  # no escape in the authority
    "https://127%2e0%2e0%2e1/x",
]
GOOD_TASK_LINKS = [
    "https://code.example.org/acme/pull/42",
    "https://code.example.org:8443/acme/",
    "https://code.example.org./acme/",
    "https://a.b9/x",
    "https://9b.example/x",
    "https://0xg.example/x",
    "https://\u4f8b\u3048.jp/\u30c6\u30b9\u30c8?q=\U0001f600",
    "https://code.example.org/x%20y",  # an escape in the path is fine
]


def _unicode_rejected() -> list[str]:
    """Every code point of the categories `Cc`, `Cf`, `Zs`, `Zl`, `Zp` (and the surrogates)."""
    import unicodedata

    categories = {"Cc", "Cf", "Zs", "Zl", "Zp", "Cs"}
    return [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) in categories]


def test_the_sdk_pattern_is_the_core_pattern() -> None:
    from horae.contracts.intents import TASK_LINK_PATTERN
    from horae_sdk import tasks

    assert f"^{tasks._TASK_LINK.pattern}$" == TASK_LINK_PATTERN


def test_task_links_refuse_control_format_and_separator_characters_anywhere() -> None:
    from pydantic import TypeAdapter, ValidationError

    from horae.contracts import TaskLink
    from horae_sdk.tasks import is_task_link

    adapter: TypeAdapter[str] = TypeAdapter(TaskLink)
    chars = _unicode_rejected()
    assert len(chars) > 2000  # the surrogates, controls, bidi, tags...
    for char in chars:
        for link in (f"https://code.exa{char}mple.org/x", f"https://code.example.org/a{char}b"):
            assert not is_task_link(link), hex(ord(char))
            with pytest.raises(ValidationError):
                adapter.validate_python(link)
            if not 0xD800 <= ord(char) <= 0xDFFF and ord(char) <= 0xFFFF:
                document = {"type": "task.create", "title": "t", "fingerprint": "f", "link": link}
                assert schema_errors("intents/task.create/v1.json", document) != [], hex(ord(char))


def test_task_links_refuse_ip_literal_hosts_and_agree() -> None:
    permissions = Permissions(intents=["task.create"])
    for link in MODEL_ONLY_BAD_TASK_LINKS + SCHEMA_BAD_HOSTS:
        intent = {"type": "task.create", "title": "t", "fingerprint": "f", "link": link}
        with pytest.raises(IntentError, match="link"):
            intents.task_create("t", key="f", link=link)
        with pytest.raises(Exception, match="link"):
            validate_intents([intent], permissions=permissions, source="sdk")
    for link in SCHEMA_BAD_HOSTS:
        intent = {"type": "task.create", "title": "t", "fingerprint": "f", "link": link}
        assert schema_errors("intents/task.create/v1.json", intent) != [], link
    for link in GOOD_TASK_LINKS:
        built = intents.task_create("t", key="f", link=link)
        (typed,) = validate_intents([built], permissions=permissions, source="sdk")
        assert isinstance(typed, TaskCreate)
        assert typed.link == link
        assert schema_errors("intents/task.create/v1.json", built) == [], link


def test_task_links_are_checked_like_the_core() -> None:
    permissions = Permissions(intents=["task.create"])
    for link in BAD_TASK_LINKS:
        intent = {"type": "task.create", "title": "t", "fingerprint": "f", "link": link}
        assert schema_errors("intents/task.create/v1.json", intent) != [], link
        with pytest.raises(Exception, match="link"):
            validate_intents([intent], permissions=permissions, source="sdk")
    longest = "https://code.example.org/" + "a" * (2048 - 25)
    built = intents.task_create("t", key="f", link=longest)
    (typed,) = validate_intents([built], permissions=permissions, source="sdk")
    assert isinstance(typed, TaskCreate)
    assert typed.link == longest


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: intents.note_create("x", path="/abs.md"), "path"),
        (lambda: intents.note_create("x", path="a/.obsidian/x.md"), "path"),
        (lambda: intents.note_create("x" * (1024 * 1024 + 1)), "content"),
        (lambda: intents.note_append_section("a\nb", "x"), "heading"),
        (lambda: intents.note_replace_owned_block("Bad_Block", "x"), "block"),
        (lambda: intents.task_create("t", key="no spaces"), "fingerprint"),
        (lambda: intents.task_create("t", key="f", time="08:00"), "requires due"),
        (lambda: intents.task_create("t", key="f", due="2026-10-22", time="8h"), "HH:MM"),
        (lambda: intents.task_create("t", key="f", due="tomorrow"), "due"),
        (lambda: intents.task_create("t", key="f", due="2026-02-30"), "real date"),
        (lambda: intents.task_create("t", key="f", due="2026-13-01"), "real date"),
        (lambda: intents.task_update_status("f", "snoozed", until="2026-02-29"), "until"),
        (
            lambda: intents.task_update_status("f", "snoozed", until="2026-04-31T09:00:00Z"),
            "until",
        ),
        (lambda: intents.task_create("a\nb", key="f", escape=False), "title"),
        (lambda: intents.task_create(1, key="f"), "title"),  # type: ignore[arg-type]
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
        *[
            (lambda link=link: intents.task_create("t", key="f", link=link), "link")
            for link in BAD_TASK_LINKS
        ],
    ],
)
def test_builders_refuse_what_the_core_would(build: Any, message: str) -> None:
    with pytest.raises(IntentError, match=message):
        build()
