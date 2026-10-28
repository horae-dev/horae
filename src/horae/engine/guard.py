# SPDX-License-Identifier: Apache-2.0
"""The action guard on LLM-proposed intents (#35, PLAN.md §2.1 tenet 5, schemas decision 46).

Routing (schemas decision 30) binds every *target* of an intent to a trusted pipeline
parameter. It does not look at what the intent *says*: note content, headings, task titles,
notification titles, bodies and links, and the strings of an `http.post` body are untrusted
text the model wrote, which an injected input can steer. The guard checks that text, for
every batch of intents the `llm` step proposes (fresh, reused from `previous` or from the
cache), after schema validation and before routing:

1. **Per-type limits.** At most `limits[type]` intents of each type (the skill's
   `intents: {type: max}`, schemas decision 39 F).
2. **Embeds are always refused**, even with a URL from the input: they fetch when the note
   renders, without a click. Markdown images `![…](…)`, reference images `![…][…]` and
   Obsidian embeds `![[…]]` (any `![`), and the HTML tags of `EMBED_TAGS` (`<img`, `<iframe`,
   `<script`, `<svg`, `<meta`…).
3. **Refused schemes** (`REFUSED_SCHEMES`: `javascript:`, `vbscript:`, `data:`, `file:`)
   followed by anything but whitespace, anywhere; inside an HTML tag also with whitespace or
   control characters inside the scheme (`java&#9;script:`); a protocol-relative link (`//host`)
   as a Markdown link destination, a link reference definition or an HTML attribute value.
4. **Every URL must be in the input set**: an exact string extracted, the same way, from the
   viewed input the model was shown (strings and object keys). A URL is any
   `scheme://…`, `mailto:…` or `www.…` run, up to whitespace, `<`, `>` or a control
   character; trailing punctuation (`.,;:!?)]}'"*_~` and backtick, at most 16 characters) may
   be dropped on either side, so `[a](https://x/p)` matches `https://x/p.` in the input. A
   `notify.push` `link` must be exactly such a URL (and is also routed only under a trusted
   `link_prefix`, decision 30).

Every check runs on the text as written and on its HTML-entity-decoded forms (up to three
passes: `&#x3c;img`, `&amp;lt;img`), ASCII case-insensitively. All scans are linear: `find`
loops whose next search starts after the previous match, and regular expressions without
nested quantifiers.

A violation is reported as a short, **content-free** detail (`GuardViolation.detail`): the
proposer, the intent type and position, the field and the kind of problem, from closed sets
only, never the URL or the text.
"""

import html
import re
import string
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from horae.contracts.intents import Intent, IntentType, NotifyPush

GUARD_ERROR = "guard_violation"
"""The `StepError.kind` of a step whose proposals the guard refused (the run record's
`Violation.kind` is `guard`)."""

MAX_DETAIL = 512
"""Longest violation detail (characters)."""

TEXT_FIELDS: Mapping[IntentType, tuple[str, ...]] = {
    "note.create": ("content",),
    "note.append_section": ("heading", "content"),
    "note.replace_owned_block": ("content",),
    "task.create": ("title", "fingerprint"),
    "task.update_status": (),
    "notify.push": ("title", "body"),
    "http.post": ("body",),
}
"""The untrusted text fields of each intent type that the guard scans (`notify.push` `link`
is checked as a whole URL; targets bound by routing are not text)."""

EMBED_TAGS = frozenset(
    {
        "applet",
        "audio",
        "base",
        "bgsound",
        "embed",
        "form",
        "frame",
        "frameset",
        "iframe",
        "image",
        "img",
        "input",
        "link",
        "math",
        "meta",
        "object",
        "param",
        "picture",
        "portal",
        "script",
        "source",
        "style",
        "svg",
        "track",
        "video",
    }
)
"""HTML tags refused in LLM text, whatever their attributes (they load or run something)."""

REFUSED_SCHEMES = ("javascript:", "vbscript:", "data:", "file:")
"""URL schemes refused in LLM text, even when the input holds them."""

_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_SCHEME_CHARS = frozenset(string.ascii_lowercase + string.digits + "+.-")
_WORD = frozenset(string.ascii_lowercase + string.digits + "-_.")
_RUN = re.compile(r"[^\s<>\x00-\x1f\x7f]*")
_TAG_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")
_TRAILING = frozenset(".,;:!?)]}'\"*_~`")
_MAX_STRIP = 16
_MAX_SCHEME = 32
_DECODE_PASSES = 3


def _views(text: str) -> Iterator[str]:
    """`text`, then its HTML-entity-decoded forms until nothing changes (bounded passes)."""
    yield text
    for _ in range(_DECODE_PASSES):
        decoded = html.unescape(text) if "&" in text else text
        if decoded == text:
            return
        text = decoded
        yield text


def _variants(url: str) -> Iterator[str]:
    """`url` and the forms without its trailing punctuation (at most `_MAX_STRIP` dropped)."""
    yield url
    for _ in range(_MAX_STRIP):
        if len(url) < 2 or url[-1] not in _TRAILING:
            return
        url = url[:-1]
        yield url


def _run_end(text: str, start: int) -> int:
    match = _RUN.match(text, start)
    return start if match is None else match.end()  # pragma: no branch (`*` always matches)


def urls(text: str) -> Iterator[str]:
    """Every URL run of `text` (`scheme://…`, `mailto:…`, `www.…`), as written."""
    low = text.translate(_LOWER)
    found = low.find("://")
    while found != -1:
        start = found
        while start > 0 and found - start < _MAX_SCHEME and low[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < found and low[start] not in string.ascii_lowercase:
            start += 1
        end = _run_end(text, found + 3)
        if start < found:
            yield text[start:end]
        found = low.find("://", max(end, found + 3))
    for marker in ("mailto:", "www."):
        found = low.find(marker)
        while found != -1:
            end = _run_end(text, found)
            if found == 0 or low[found - 1] not in _WORD:
                yield text[found:end]
            found = low.find(marker, max(end, found + 1))


def _refused_scheme(low: str) -> bool:
    """A refused scheme, at a word start and followed by a non-space character."""
    for scheme in REFUSED_SCHEMES:
        found = low.find(scheme)
        while found != -1:
            after = found + len(scheme)
            boundary = found == 0 or low[found - 1] not in _SCHEME_CHARS
            if boundary and after < len(low) and not low[after].isspace():
                return True
            found = low.find(scheme, after)
    return False


def _relative_destination(low: str) -> bool:
    """`//host` as a Markdown link destination (`](//`) or link reference definition."""
    for marker in ("](", "]:"):
        found = low.find(marker)
        while found != -1:
            rest = low[found + 2 : found + 2 + _MAX_STRIP].lstrip(" \t\n<")
            if rest.startswith("//"):
                return True
            found = low.find(marker, found + 2)
    return False


def _tag_problem(low: str) -> str | None:
    """An embed tag, or a refused scheme or protocol-relative link inside any tag."""
    found = low.find("<")
    while found != -1:
        following = low.find("<", found + 1)
        stop = len(low) if following == -1 else following
        name = _TAG_NAME.match(low, found + 1)
        if name is not None:
            if name.group() in EMBED_TAGS:
                return f"an HTML <{name.group()}> embed"
            close = low.find(">", name.end(), stop)
            body = _SPACE.sub("", low[name.end() : stop if close == -1 else close])
            if any(scheme in body for scheme in REFUSED_SCHEMES):
                return "a script or data link in an HTML tag"
            if any(marker in body for marker in ("=//", "='//", '="//')):
                return "a protocol-relative link in an HTML tag"
        found = following
    return None


class _Known:
    """The input set: every URL variant extracted from the viewed input, built on first use."""

    def __init__(self, shown: Any) -> None:
        self._shown = shown
        self._urls: set[str] | None = None

    def __contains__(self, url: object) -> bool:
        if self._urls is None:
            self._urls = set()
            for text in _strings(self._shown):
                for view in _views(text):
                    for found in urls(view):
                        self._urls.update(_variants(found))
        return url in self._urls


def _strings(value: Any) -> Iterator[str]:
    """Every string value and object key inside `value`, iteratively."""
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    yield key
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def text_problem(text: str, known: Callable[[str], bool]) -> str | None:
    """What the guard refuses in one untrusted text (a closed-set description), or `None`.

    `known(url)` says whether a URL is in the input set. Markup is checked on every view
    first, so an embed is reported as one even when its URL is also unknown."""
    views = list(_views(text))
    for view in views:
        low = view.translate(_LOWER)
        if "![" in low:
            return "an image or embed"
        tag = _tag_problem(low)
        if tag is not None:
            return tag
        if _refused_scheme(low):
            return "a script, data or file link"
        if _relative_destination(low):
            return "a protocol-relative link"
    for view in views:
        for url in urls(view):
            if not any(known(variant) for variant in _variants(url)):
                return "a link not in the input"
    return None


def _fields(intent: Intent) -> Iterator[tuple[str, str]]:
    data = intent.model_dump(mode="json")
    for name in TEXT_FIELDS[intent.type]:
        for text in _strings(data.get(name)):
            yield name, text


def check_intents(
    intents: Sequence[Intent],
    *,
    shown: Any,
    source: str,
    limits: Mapping[str, int] | None = None,
) -> str | None:
    """`None` when the guard accepts every intent of the batch; else the content-free detail
    of the first violation. `shown` is what the model was shown (the viewed input), `source`
    the proposer (`skill:<name>`), `limits` the most intents of each type."""
    for intent_type, most in sorted((limits or {}).items()):
        count = sum(1 for intent in intents if intent.type == intent_type)
        if count > most:
            message = f"{source} proposed {count} {intent_type} intents, the skill allows {most}"
            return _detail(message)
    input_set = _Known(shown)
    known = input_set.__contains__
    for position, intent in enumerate(intents):
        where = f"{source} proposed {intent.type} #{position}"
        if isinstance(intent, NotifyPush) and intent.link is not None and not known(intent.link):
            return _detail(f"{where} with a link not in the input in `link`")
        for name, text in _fields(intent):
            problem = text_problem(text, known)
            if problem is not None:
                return _detail(f"{where} with {problem} in `{name}`")
    return None


def _detail(text: str) -> str:
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def guard_for(
    shown: Any, source: str, limits: Mapping[str, int] | None = None
) -> Callable[[Iterable[Intent]], str | None]:
    """`check_intents` bound to one step's viewed input, proposer and limits."""
    bound = dict(limits or {})
    return lambda intents: check_intents(tuple(intents), shown=shown, source=source, limits=bound)


__all__ = [
    "EMBED_TAGS",
    "GUARD_ERROR",
    "MAX_DETAIL",
    "REFUSED_SCHEMES",
    "TEXT_FIELDS",
    "check_intents",
    "guard_for",
    "text_problem",
    "urls",
]
