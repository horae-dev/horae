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
   Obsidian embeds `![[…]]` (any `![`).
3. **HTML: an allowlist, without attributes.** Any tag (`<name` or `</name`) whose name is
   not in `INERT_TAGS` (`b`, `i`, `em`, `strong`, `code`, `p`, `li`, `h1`…) is refused, and an
   allowed tag must carry nothing but an optional `/` before its `>`: no attribute at all
   (so no `on*`, `style`, `background`, `poster`, `srcset`, `ping`, `href`, `url(` or
   `@import`). `<scheme:…>` with no whitespace before its `>` is a Markdown autolink, checked
   as a URL (4).
4. **URLs.** Backslashes are read as `/` first (`https:/\\host`, `\\\\host`). A URL is a run
   (up to whitespace, `<`, `>` or a control character) starting at
   - `scheme:` followed by `/` (any scheme, `[a-z][a-z0-9+.-]*`), or a scheme of
     `SPECIAL_SCHEMES` (`http`, `ftp`, `mailto`, `obsidian`, `javascript`…) followed by any
     non-space character (`ftp:evil`);
   - `//` at a token boundary (protocol-relative), followed by at least one character;
   - `www.` at a word start.

   A URL whose scheme is in `REFUSED_SCHEMES` (`javascript`, `vbscript`, `data`, `file`,
   `blob`) is refused, even when the input holds it. Every other URL must be **in the input
   set**: an exact string extracted, the same way, from the viewed input the model was shown
   (strings and object keys). Trailing punctuation (`.,;:!?)]}'"*_~` and backtick, at most 16
   characters) may be dropped on either side, so `[a](https://x/p)` matches `https://x/p.` in
   the input. A `notify.push` `link` must be exactly such a URL (and is also routed only under
   a trusted `link_prefix`, decision 30).
5. **Link contexts.** After `](` (a Markdown link destination), `]:` (a link reference
   definition) and `<`, whitespace and control characters are dropped (`[x]( java\\tscript :…)`)
   and a refused scheme, whatever follows its colon, or `//` is refused.

A refused scheme in prose (`data: three items`, colon then a space) is not a link in Markdown
and passes. Every check runs on the text as written and on its HTML-entity-decoded forms (up
to three passes: `&#x3c;img`, `&amp;lt;img`), ASCII case-insensitively. All scans are linear:
`find` loops whose next search starts after the previous match (or a bounded window), and
regular expressions without nested quantifiers.

A violation is reported as a short, **content-free** detail: the proposer, the intent type and
position, the field and the kind of problem, from closed sets only, never the URL or the text.
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

INERT_TAGS = frozenset(
    {"b", "i", "u", "s", "em", "strong", "code", "pre", "br", "hr", "p", "ul", "ol", "li"}
    | {"blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "kbd", "sup", "sub", "mark", "del"}
)
"""The only HTML tags allowed in LLM text, and only without attributes."""

EMBED_TAGS = frozenset(
    {"applet", "audio", "base", "bgsound", "embed", "form", "frame", "frameset", "iframe"}
    | {"image", "img", "input", "link", "math", "meta", "object", "param", "picture"}
    | {"portal", "script", "source", "style", "svg", "track", "video"}
)
"""Refused tags that a violation detail names (any other refused tag is not named: its name
is the model's text)."""

REFUSED_SCHEMES = frozenset({"javascript", "vbscript", "data", "file", "blob"})
"""URL schemes refused in LLM text, even when the input holds them."""

SPECIAL_SCHEMES = REFUSED_SCHEMES | {"http", "https", "ftp", "ws", "wss", "obsidian", "mailto"}
"""Schemes that start a URL whatever follows their colon (other schemes need a `/`)."""

_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_LETTERS = frozenset(string.ascii_lowercase)
_SCHEME_CHARS = frozenset(string.ascii_lowercase + string.digits + "+.-")
_WORD = frozenset(string.ascii_lowercase + string.digits + "-_.")
_RUN = re.compile(r"[^\s<>\x00-\x1f\x7f]*")
_TAG_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_AUTOLINK = re.compile(r"[a-z][a-z0-9+.-]{0,31}:")
_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")
_TRAILING = frozenset(".,;:!?)]}'\"*_~`")
_REFUSED_PREFIXES = tuple(f"{scheme}:" for scheme in sorted(REFUSED_SCHEMES))
_CONTEXTS = ("](", "]:", "<")
_WINDOW = 64
_MAX_STRIP = 16
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


def _scheme_urls(text: str, low: str) -> Iterator[str]:
    """`scheme:/…` and `special:…` runs. A walk back from a colon stops at the previous colon
    (not a scheme character), so the walks are disjoint."""
    colon = low.find(":")
    while colon != -1:
        start = colon
        while start > 0 and low[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < colon and low[start] not in _LETTERS:
            start += 1
        scheme = low[start:colon]
        following = low[colon + 1 : colon + 2]
        if scheme and (
            following == "/"
            or (scheme in SPECIAL_SCHEMES and following and not following.isspace())
        ):
            end = _run_end(text, colon + 1)
            yield text[start:end]
            colon = low.find(":", max(end, colon + 1))
        else:
            colon = low.find(":", colon + 1)


def urls(text: str) -> Iterator[str]:
    """Every URL run of `text` (module docstring, 4), backslashes read as `/`."""
    text = text.replace("\\", "/")
    low = text.translate(_LOWER)
    yield from _scheme_urls(text, low)
    found = low.find("//")
    while found != -1:
        before = low[found - 1] if found else " "
        if before in _SCHEME_CHARS or before in ":/":
            found = low.find("//", found + 1)
            continue
        end = _run_end(text, found)
        if end - found > 2:
            yield text[found:end]
        found = low.find("//", max(end, found + 2))
    found = low.find("www.")
    while found != -1:
        end = _run_end(text, found)
        if found == 0 or low[found - 1] not in _WORD:
            yield text[found:end]
        found = low.find("www.", max(end, found + 1))


def _scheme_of(url: str) -> str | None:
    colon = url.find(":")
    head = url[:colon].translate(_LOWER) if colon > 0 else ""
    return head if head and set(head) <= _SCHEME_CHARS else None


def _context_problem(low: str) -> str | None:
    """A refused scheme or `//` right after `](`, `]:` or `<`, whitespace dropped."""
    for marker in _CONTEXTS:
        found = low.find(marker)
        while found != -1:
            after = found + len(marker)
            window = _SPACE.sub("", low[after : after + _WINDOW]).lstrip("<")
            if window.startswith(_REFUSED_PREFIXES):
                return "a script, data or file link"
            if window.startswith("//"):
                return "a protocol-relative link"
            found = low.find(marker, after)
    return None


def _autolink(low: str, at: int) -> bool:
    """`<scheme:…>` with nothing but URL characters up to its `>` (a Markdown autolink)."""
    if _AUTOLINK.match(low, at) is None:
        return False
    end = _run_end(low, at)
    return low[end : end + 1] == ">"


def _tag_problem(low: str) -> str | None:
    """A tag outside `INERT_TAGS`, or an allowed tag with an attribute. Each tag's body ends at
    its `>` or the next `<`, so the scans are disjoint."""
    found = low.find("<")
    while found != -1:
        following = low.find("<", found + 1)
        stop = len(low) if following == -1 else following
        at = found + 2 if low.startswith("</", found) else found + 1
        name = _TAG_NAME.match(low, at)
        if name is not None and not _autolink(low, at):
            tag = name.group()
            if tag not in INERT_TAGS:
                if tag in EMBED_TAGS:
                    return f"an HTML <{tag}> embed"
                return "an HTML tag outside the allowlist"
            close = low.find(">", name.end(), stop)
            body = _SPACE.sub("", low[name.end() : stop if close == -1 else close])
            if body not in ("", "/"):
                return "an HTML attribute"
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
        low = view.replace("\\", "/").translate(_LOWER)
        if "![" in low:
            return "an image or embed"
        problem = _tag_problem(low) or _context_problem(low)
        if problem is not None:
            return problem
    unknown = False
    for view in views:
        for url in urls(view):
            if _scheme_of(url) in REFUSED_SCHEMES:
                return "a script, data or file link"
            if not any(known(variant) for variant in _variants(url)):
                unknown = True
    return "a link not in the input" if unknown else None


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
    "INERT_TAGS",
    "MAX_DETAIL",
    "REFUSED_SCHEMES",
    "SPECIAL_SCHEMES",
    "TEXT_FIELDS",
    "check_intents",
    "guard_for",
    "text_problem",
    "urls",
]
