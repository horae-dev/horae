# SPDX-License-Identifier: Apache-2.0
"""Obsidian Tasks title escaping (threat model T02, #42): builders and Tasks sinks."""

import re
from urllib.parse import unquote

import pytest
from hypothesis import given
from hypothesis import strategies as st

from horae.contracts import Permissions, TaskCreate
from horae.engine.guard import SIGNIFIERS as GUARD_SIGNIFIERS
from horae.engine.guard import note_problem
from horae.intents import validate_intents
from horae_sdk import intents
from horae_sdk.tasks import (
    LINK_LABEL,
    MARKDOWN,
    MAX_DESTINATION,
    MAX_LINK,
    SIGNIFIERS,
    UNTITLED,
    escape_title,
    is_task_link,
    link_destination,
    plain_text,
    task_line,
    task_line_for,
)
from sdk_helpers import DUE, HOSTILE, REMINDER, SDK, signifiers_in


def unescaped_markup(text: str) -> list[str]:
    """Markdown characters of `text` not preceded by an escaping backslash."""
    found = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == "&" and re.match(r"&#x[0-9A-F]{2,5};", text[index:]):
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
    assert plain_text(escaped) == HOSTILE  # a reader still sees the same text


def test_the_intent_builder_escapes_and_the_core_accepts_it() -> None:
    intent = intents.task_create(HOSTILE, key="mail:42", due="2026-10-22", time="19:00")
    assert signifiers_in(intent["title"]) == []
    assert unescaped_markup(intent["title"]) == []
    (typed,) = validate_intents(
        [intent], permissions=Permissions(intents=["task.create"]), source="sdk"
    )
    assert isinstance(typed, TaskCreate)
    assert typed.title == intent["title"]


def test_a_tasks_sink_line_holds_only_its_own_fields() -> None:
    raw = intents.task_create(HOSTILE, key="mail:42", escape=False)
    assert signifiers_in(raw["title"])  # unescaped: the sink must escape it
    built = intents.task_create(HOSTILE, key="mail:42", due="2026-10-22", time="19:00")
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

    with pytest.raises(ValueError, match="real date"):
        task_line("x", due="2026-02-30")


def test_lines_controls_and_variation_selectors() -> None:
    assert escape_title(f"a\nb\tc\u2028d\u202ee\u200bf  {DUE}\ufe0f ") == "a b c def &#x1F4C5;"
    # `x:` looks like a URL scheme, so its colon is escaped too (no autolink)
    assert escape_title("x::y:::z") == "x\\:\\:y\\:\\:\\:z"
    assert escape_title("1::2 :: 3") == "1:\\:2 :\\: 3"


def test_no_pair_survives_an_escape_or_a_dropped_character() -> None:
    # finding: `\::` left a literal `::`: the rules look at the last piece written
    assert escape_title("\\::") == "\\:\\:"
    assert escape_title(":\u200b:") == ":\\:"
    assert escape_title("1\\:\u202e:") == "1\\:\\:"
    assert escape_title("\\%%") == "\\%&#x25;"
    assert escape_title("\\%\\% \\=\\=") == "\\%&#x25; \\=&#x3D;"
    assert escape_title("=\u200b=") == "\\=&#x3D;"


def test_a_title_with_nothing_printable_is_untitled() -> None:
    for title in ("\u200b", "\u200b\u200c\ufeff", "\u202e\u2066\u2069", "\n\r\t \u2028", ""):
        assert escape_title(title) == UNTITLED
        intent = intents.task_create(title, key="mail:1")
        assert intent["title"] == UNTITLED
        assert plain_text(intent["title"]) == "(untitled)"
    assert escape_title(UNTITLED) == UNTITLED


def test_a_hostile_item_does_not_abort_a_collect() -> None:
    subjects = ["Pay the bill", "\u200b\u202e\n", f"{DUE} 2020-01-01"]
    built = [intents.task_create(s, key=intents.fingerprint("mail", s)) for s in subjects]
    assert [intent["title"] for intent in built] == [
        "Pay the bill",
        UNTITLED,
        "&#x1F4C5; 2020-01-01",
    ]


def test_long_titles_are_cut_on_an_escape_boundary() -> None:
    escaped = escape_title(DUE * 400)
    assert len(escaped) <= 500
    assert escaped.endswith("…")
    assert re.fullmatch(r"(&#x1F4C5;)+…", escaped)
    assert len(intents.task_create("x" * 600, key="f")["title"]) == 500
    # the cut happens after escaping: 300 `*` are 600 characters once escaped
    assert escape_title("*" * 300) == "\\*" * 249 + "\u2026"


def test_the_cut_never_separates_a_mark_from_its_character() -> None:
    accented = escape_title("e\u0301" * 300)
    assert accented.endswith("e\u0301\u2026")
    assert escape_title("a" * 498 + "e\u0301\u0301") == "a" * 498 + "\u2026"
    thumbs = escape_title("\U0001f44d\U0001f3fd" * 300)
    assert thumbs.endswith("\U0001f44d\U0001f3fd\u2026")


def test_bare_urls_are_not_autolinked() -> None:
    assert escape_title("see https://evil.example/x") == "see https\\://evil.example/x"
    assert escape_title("obsidian://open?vault=v") == "obsidian\\://open?vault\\=v"
    assert escape_title("javascript:alert(1)") == "javascript\\:alert\\(1\\)"
    assert escape_title("mailto:a@example.org") == "mailto\\:a@example.org"
    assert escape_title("www.example.org WWW.example.org") == "www\\.example.org WWW\\.example.org"
    # not a scheme: prose, times, a colon before a space
    assert escape_title("Re: lunch at 10:30, ratio 1:2") == "Re: lunch at 10:30, ratio 1:2"
    assert escape_title("awww.no -https://x -www.x") == "awww.no -https\\://x -www\\.x"
    assert plain_text(escape_title("https://x.example")) == "https://x.example"


def test_comments_and_highlights_leave_no_pair_in_the_line() -> None:
    escaped = escape_title("%%c%% ==h== 100% a=b")
    assert escaped == "\\%&#x25;c\\%&#x25; \\=&#x3D;h\\=&#x3D; 100\\% a\\=b"
    unbackslashed = re.sub(r"\\(.)", r"\1", escaped)  # a parser that drops backslashes first
    assert "%%" not in unbackslashed
    assert "==" not in unbackslashed


def test_the_manual_checklist_title() -> None:
    """The title of sdk/README.md's manual check in a real vault."""
    title = f"{DUE} 2020-01-01 \U0001f501 every day #t [[x]] %%c%% ==h=="
    escaped = escape_title(title)
    assert escaped == (
        "&#x1F4C5; 2020-01-01 &#x1F501; every day \\#t \\[\\[x\\]\\]"
        " \\%&#x25;c\\%&#x25; \\=&#x3D;h\\=&#x3D;"
    )
    assert plain_text(escaped) == title
    assert tasks_fields(f"- [ ] {escaped}") == []
    assert tasks_fields(f"- [ ] {title}")  # the ported regexes do see the raw title


def test_the_readme_shows_the_line_the_manual_check_writes() -> None:
    title = (
        f"{DUE} 2020-01-01 \U0001f501 every day #t [[x]] %%c%% ==h=="
        " https://example.org www.example.org"
    )
    readme = (SDK / "README.md").read_text(encoding="utf-8")
    assert f"`{title}`" in readme
    assert f"\n   {task_line(title, due='2030-01-01')}\n" in readme


def test_idempotence_trade_offs() -> None:
    # references in the form escape_title writes pass through and show the character
    assert escape_title("&#x1F4C5; &#x25;") == "&#x1F4C5; &#x25;"
    # any other reference shows literally: lowercase hex, named, or not a signifier
    assert escape_title("&#x1f4c5; &amp; &#x41;") == "\\&\\#x1f4c5; \\&amp; \\&\\#x41;"
    # an existing backslash escape is kept (it shows the character)
    assert escape_title("\\#tag \\x") == "\\#tag \\\\x"


def test_plain_text_is_for_display_and_not_an_inverse() -> None:
    assert plain_text("a\\*b &#x1F4C5; \\&#x41;") == f"a*b {DUE} &#x41;"
    # what escaping removes is gone: plain_text is no inverse
    for title in ("a\nb", "x\u200by", "a  b", "y" * 600, "\u200b"):
        assert plain_text(escape_title(title)) != title
    # the unescaped text is hostile again: never for a Markdown or Tasks target
    assert signifiers_in(plain_text(escape_title(HOSTILE)))
    assert plain_text("&#xD800; &#x110000;") == "&#xD800; &#x110000;"


# --- the Obsidian Tasks plugin's own field regexes ------------------------------------------
# Ported from obsidian-tasks (DefaultTaskSerializer, emoji format; DataviewTaskSerializer),
# with the optional U+FE0F after each signifier. Tasks anchors them at the end of the line and
# strips fields one by one; here they are searched anywhere, which is stricter.
_DATE = r"\d{4}-\d{2}-\d{2}"
_VS = "\ufe0f?"
_EMOJI_FIELDS = {
    "priority": f"[\U0001f53a\u23eb\U0001f53c\U0001f53d\u23ec]{_VS}",
    "start": f"\U0001f6eb{_VS} *{_DATE}",
    "created": f"\u2795{_VS} *{_DATE}",
    "scheduled": f"[\u23f3\u231b]{_VS} *{_DATE}",
    "due": f"[\U0001f4c5\U0001f4c6\U0001f5d3]{_VS} *{_DATE}",
    "done": f"\u2705{_VS} *{_DATE}",
    "cancelled": f"\u274c{_VS} *{_DATE}",
    "recurrence": f"\U0001f501{_VS} *[a-zA-Z0-9, !]+",
    "on completion": f"\U0001f3c1{_VS} *[a-zA-Z]+",
    "depends on": f"\u26d4{_VS} *[a-zA-Z0-9_-]+( *, *[a-zA-Z0-9_-]+ *)*",
    "id": f"\U0001f194{_VS} *[a-zA-Z0-9_-]+",
}
_DATAVIEW_FIELDS = {
    "priority": "priority:: *(highest|high|medium|low|lowest)",
    "start": f"start:: *{_DATE}",
    "created": f"created:: *{_DATE}",
    "scheduled": f"scheduled:: *{_DATE}",
    "due": f"due:: *{_DATE}",
    "done": f"completion:: *{_DATE}",
    "cancelled": f"cancelled:: *{_DATE}",
    "recurrence": "repeat:: *[a-zA-Z0-9, !]+",
    "on completion": "onCompletion:: *[a-zA-Z]+",
    "depends on": "dependsOn:: *[a-zA-Z0-9_-]+",
    "id": "id:: *[a-zA-Z0-9_-]+",
}
_TASKS_REGEXES = {
    **{f"emoji {name}": re.compile(regex, re.IGNORECASE) for name, regex in _EMOJI_FIELDS.items()},
    **{
        f"dataview {name}": re.compile(rf"[\[(]{regex}[\])]", re.IGNORECASE)
        for name, regex in _DATAVIEW_FIELDS.items()
    },
    "tag": re.compile(r"(^|\s)#[^ !@#$%^&*(),.?\":{}|<>]+"),
}
# Obsidian (and GFM) constructs a title must not hold.
_OBSIDIAN = {
    "wikilink or embed": re.compile(r"\[\["),
    "comment": re.compile("%%"),
    "highlight": re.compile("=="),
    "inline field": re.compile("::"),
    "url scheme": re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:(?=\S)"),
    "www autolink": re.compile(r"(?<![A-Za-z0-9])www\.", re.IGNORECASE),
}


def tasks_fields(line: str) -> list[str]:
    """The Tasks fields and Obsidian constructs found in `line`."""
    found = [name for name, regex in _TASKS_REGEXES.items() if regex.search(line)]
    return found + [name for name, regex in _OBSIDIAN.items() if regex.search(line)]


def test_the_ported_regexes_see_every_hostile_field() -> None:
    raw = (
        f"{HOSTILE} \U0001f53a\ufe0f \U0001f4c5\ufe0f 2020-01-01 \U0001f194 a1 \u26d4 a1, b2"
        " \U0001f3c1 delete [priority:: high] [repeat:: every day] (dependsOn:: a1)"
        " [completion:: 2020-01-01] [created:: 2020-01-01] [scheduled:: 2020-01-01]"
        " [cancelled:: 2020-01-01] [onCompletion:: delete] [id:: a1] \u274c 2020-01-01"
        " https://x.example www.example.org"
    )
    assert set(tasks_fields(f"- [ ] {raw}")) == {*_TASKS_REGEXES, *_OBSIDIAN}
    assert tasks_fields(f"- [ ] {escape_title(raw)}") == []


def _check_inert(title: str) -> None:
    once = escape_title(title)
    assert escape_title(once) == once
    assert signifiers_in(once) == []
    assert unescaped_markup(once) == []
    assert not re.search(r"[\x00-\x1f\x7f]", once)
    assert tasks_fields(f"- [ ] {once}") == []
    unbackslashed = re.sub(r"\\(.)", r"\1", once)
    assert "%%" not in unbackslashed
    assert "==" not in unbackslashed


_HOSTILE_TOKENS = [
    *SIGNIFIERS,
    *MARKDOWN,
    *":%=.\\&#;/ @+-",
    "\ufe0f",
    "\u200b",
    "\u202e",
    "\n",
    "\u0301",
    "a",
    "w",
    "1",
    "www",
    "https",
    "javascript",
    "due",
    "&#x25;",
    "&#x3D;",
    "&#x1F4C5;",
    "&#x1f4c5;",
    "\\:",
    "\\%",
    "\\=",
    "::",
    "%%",
    "==",
    "[[",
    " 2020-01-01",
]


@given(st.lists(st.sampled_from(_HOSTILE_TOKENS), max_size=60).map("".join))
def test_escaping_hostile_alphabets(title: str) -> None:
    _check_inert(title)


@given(st.text(max_size=300))
def test_escaping_is_idempotent_and_inert(title: str) -> None:
    _check_inert(title)


# --- task links (#122, schemas decision 52) --------------------------------------------------
PR = "https://code.example.org/acme/app/pull/42"
# A Markdown inline link with the fixed label; its destination holds no space, `(`, `)`, `<`, `>`.
_LINK = re.compile(rf"\[{LINK_LABEL}\]\(([^\s()<>]*)\)")
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#@!&'*+,;=%]*")


def test_a_task_line_renders_the_link_after_the_title() -> None:
    assert task_line("Review PR 42", link=PR) == f"- [ ] Review PR 42 [link]({PR})"
    line = task_line("Review", link=PR + "#c-7", due="2026-10-22", time="19:00", done=True)
    assert line == f"- [x] Review [link]({PR}#c-7) {DUE} 2026-10-22 {REMINDER} 19:00"
    intent = intents.task_create("Review", key="github:pr:42", due="2026-10-22", link=PR)
    assert task_line_for(intent) == f"- [ ] Review [link]({PR}) {DUE} 2026-10-22"
    for bad in (
        "http://code.example.org/x",
        "javascript:alert(1)",
        "https://u@h/x",
        "https://h/ x",
    ):
        with pytest.raises(ValueError, match="link"):
            task_line("x", link=bad)
    with pytest.raises(ValueError, match="strings"):
        task_line_for({"type": "task.create", "title": "x", "link": 1})


@pytest.mark.parametrize("char", ["\u754c", "\U0001f600"])  # 3 and 4 UTF-8 bytes
def test_a_link_too_long_once_encoded_is_left_out(char: str) -> None:
    base = "https://x.example/"
    link = base + char * (MAX_LINK - len(base))  # 2030 characters, a valid link
    assert len(link) == MAX_LINK
    assert is_task_link(link)
    assert len(link_destination(link)) > MAX_DESTINATION  # 9 or 12 characters each
    # The line is written without the link: nothing of it, no error.
    assert task_line("Review", link=link, due="2026-10-22") == f"- [ ] Review {DUE} 2026-10-22"
    intent = intents.task_create("Review", key="x:1", link=link)
    assert task_line_for(intent) == "- [ ] Review"
    # Up to the cap, it is kept.
    width = 3 * len(char.encode())
    fits = base + char * ((MAX_DESTINATION - len(base)) // width)
    assert len(link_destination(fits)) <= MAX_DESTINATION
    assert task_line("x", link=fits) == f"- [ ] x [link]({link_destination(fits)})"
    over = fits + char
    assert len(link_destination(over)) > MAX_DESTINATION
    assert task_line("x", link=over) == "- [ ] x"


def test_a_link_destination_is_inert_and_the_same_url() -> None:
    assert link_destination(PR + "?q=is%3Aopen&x=1#top") == PR + "?q=is%3Aopen&x=1#top"
    hostile = PR + '/(a)[b]<c>`d`$e${f}|^"x"\u00e9\U0001f4c5?q=a==b&amp;c&#x2E;::d%%e%zz'
    encoded = link_destination(hostile)
    assert encoded == (
        PR + "/%28a%29%5Bb%5D%3Cc%3E%60d%60%24e%24%7Bf%7D%7C%5E%22x%22%C3%A9%F0%9F%93%85"
        "?q=a=%3Db%26amp;c%26#x2E;:%3Ad%25%25e%25zz"
    )
    assert unquote(encoded) == unquote(hostile)  # percent-decodes to the same string
    assert link_destination(encoded) == encoded  # idempotent


def test_the_title_cannot_forge_or_break_the_link() -> None:
    title = f"x](https://evil.example/) [link](https://evil.example/) {HOSTILE}"
    line = task_line(title, link=PR + "/)x(", due="2026-10-22")
    assert _LINK.findall(line) == [PR + "/%29x%28"]
    assert signifiers_in(line) == [DUE]
    # Of the ported Tasks and Obsidian regexes, only the line's own due date and the URL scheme
    # of the link itself match.
    assert tasks_fields(line) == ["emoji due", "url scheme"]
    _check_fields(line)


_LINK_TOKENS = [
    *_HOSTILE_TOKENS,
    "(",
    ")",
    "<",
    ">",
    "`",
    "$",
    "&amp;",
    "&#x2E;",
    "%2e",
    "%",
    "?",
    "#",
]


def _check_link(link: str) -> None:
    encoded = link_destination(link)
    assert link_destination(encoded) == encoded
    assert _URL_CHARS.fullmatch(encoded)
    assert unquote(encoded) == unquote(link)
    for pair in ("::", "%%", "=="):
        assert pair not in encoded
    assert not re.search(r"&(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);", encoded)
    line = task_line(HOSTILE, link=link, due="2026-10-22")
    assert _LINK.findall(line) == [encoded]
    assert signifiers_in(line) == [DUE]
    _check_fields(line)


def _check_fields(line: str) -> None:
    """Only the line's own due date is a Tasks field, and no Obsidian construct is left,
    except the autolink patterns inside the link destination, which is not inline text."""
    inside = {"url scheme", "www autolink"}
    assert [name for name in tasks_fields(line) if name not in inside] == ["emoji due"]
    assert tasks_fields(_LINK.sub("[link](x)", line)) == ["emoji due"]


@given(st.lists(st.sampled_from(_LINK_TOKENS), max_size=40).map("".join))
def test_hostile_link_paths_stay_inert(path: str) -> None:
    link = PR + "/" + path
    if is_task_link(link):
        _check_link(link)


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=200))
def test_any_link_text_stays_inert(path: str) -> None:
    link = PR + "?" + path
    if is_task_link(link):
        _check_link(link)


# --- the core's action guard (#128, schemas decision 53) ------------------------------------


def test_the_guard_signifiers_are_the_sdk_signifiers() -> None:
    """The core keeps a copy (it does not depend on the SDK): they must not drift."""
    assert frozenset(SIGNIFIERS) == GUARD_SIGNIFIERS


_NOTE_TOKENS = [*_HOSTILE_TOKENS, "horae", ":begin", ":end", " block", "#", "#tag", "- [ ] ", "> "]


@given(st.lists(st.sampled_from(_NOTE_TOKENS), max_size=60).map("".join) | st.text(max_size=80))
def test_an_escaped_title_never_trips_the_guard_note_structure_rules(title: str) -> None:
    """A skill may list escaped titles in a note (`- <title>`): the note structure rules read
    the source, where `escape_title` left no checkbox, signifier, wikilink, field, comment,
    tag or block marker. (The guard's link rules may still refuse an escaped URL that is not
    in the input: they read the Markdown-unescaped text too, decision 46.)"""
    escaped = escape_title(title)
    assert note_problem(escaped) is None
    assert note_problem(f"- {escaped}\n> - {escaped}") is None
