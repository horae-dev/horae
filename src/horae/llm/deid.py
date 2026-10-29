# SPDX-License-Identifier: Apache-2.0
"""De-identification before a cloud model (#34, schemas decision 47, threat T10).

A skill declares **field classes** in its input view (`skill.yaml`, #27):

```yaml
input_view:
  events[].title: { class: free_text }
  events[].attendees[]: { class: person }
  budget.amount: { class: amount, buckets: [0, 50, 200, 1000] }
```

When the step's model alias is a **cloud** alias (`ModelAlias.is_local` false: every backend
but `ollama` unless the alias says `local:`), the engine sends the model a pseudonymized copy
of the viewed input (`Deidentifier.pseudonymize`), and puts real values back into the
accepted output and intents (`Pseudonymized.reidentify`) before they are validated, routed
and applied. A local alias, or a skill without classes, is sent the viewed input unchanged.

**Transforms** (a pure function of the viewed input, the classes and the vocabulary):

- `person`, `email`, `phone`, `place`: every scalar leaf, and every object key, under the
  field becomes a pseudonym, `[PERSON_1]`, `[EMAIL_2]`… (reversible); a value longer than
  `MAX_VALUE_CHARS` is withheld (`[PERSON]`, not reversible);
- `amount`: a bucket label (`50-200`, `<0`, `>=1000`; the `buckets:` option, else powers of
  ten); a value that is not a number becomes `[AMOUNT]` (not reversible);
- **every other string of the viewed input, and every object key** (`free_text` fields and
  unclassed ones alike, decision 47 B) is scanned for the **known values**: the vocabulary's
  terms, the values of the token-class fields above and each of a person's name parts
  (`Bob` and `Martin` of `Bob Martin`, mapped to the same pseudonym), each replaced by its
  pseudonym (reversible); a phone number's last nine digits whatever their separators; and
  an amount's usual renderings (`1234.56`, `1 234,56`…), replaced by its bucket label (not
  reversible). Text already shaped like a pseudonym (`[PERSON_1]`) is neutralized to
  `(PERSON_1)` first, so it can never be put back as a real value.

**Matching** (decision 47 C). A scanned string is NFKC-normalized and stripped of format
characters (zero-width spaces, bidi controls, soft hyphens) and cut into tokens: runs of
letters, runs of digits, runs of white space, single other characters. A known value is the
same token sequence, letters compared case-folded (`STRAUSS` is `Strauß`), any white-space
run equal to any other (`Bob  Martin`, `Bob\\nMartin`, a no-break space): so a value
matches only whole letter runs (`Alice` in `alice_smith`, `user_alice` or `alice123`, not
in `Malice`). Leftmost, then longest, first. A trie over the tokens: linear in the text,
whatever the number of terms. A string in which nothing is found is sent byte-identical.

**Stable per run.** Pseudonyms are numbered per class in order of first occurrence: first the
classed leaves and keys in canonical order (sorted keys, array order), then the matches in
the scanned strings, in canonical order. The same value (same tokens) always gets the same
pseudonym within the input, and the same input always gives the same pseudonymized request:
a replay, or a cache hit, recomputes the same mapping from the same input (no key, no
stored mapping).

**The mapping stays in memory.** It lives only in the `Pseudonymized` object of the step; the
run record keeps what the provider saw (`llm[].response` and `llm[].intents` are the
pseudonymized output, `llm[].request.deid` a digest and counts), the LLM cache stores the
pseudonymized output (re-identified on a hit), and the usage ledger holds no data.

**Re-identification** replaces every pseudonym of this input found in a string (or an object
key) of the output and of the raw intents by the original value's text (a string stays a
string, so the validated output keeps its types; decision 47 G). A pseudonym the input never
issued, or one the model mangled (`Person 1`, `PERSON1`), is left as it is and noted
(`unresolved`); no repair retry is made. Bucketed amounts stay buckets. `growth` gives what
re-identification adds, so the engine refuses an output that would grow past its bound
before substituting (`output_too_large`).

**Limits** (documented, decision 47): values in fields the skill did not classify are only
caught when they equal a known value (the input view is the primary control); matching
misses nicknames, initials and typos, and over-matches a known value that is also a common
word; a name part maps to the whole name (privacy over meaning).
"""

import hashlib
import itertools
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.instance import ModelAlias
from horae.contracts.skill import view_steps
from horae.engine.canonical import canonical_json, sha256_hex
from horae.engine.ports import DeidUnavailable
from horae.secrets import SecretError, SecretResolver
from horae.secrets.redact import current as current_redactor

DEID_VERSION = 2
"""The version of the transforms and of the pseudonym format: part of the LLM cache's stored
key for a cloud alias (decision 47 F)."""

TOKEN_CLASSES = ("person", "email", "phone", "place")
"""The classes replaced by a reversible pseudonym."""
FREE_TEXT = "free_text"
AMOUNT = "amount"
_LABELS = {name: name.upper() for name in TOKEN_CLASSES}

TOKEN = re.compile(r"\[(PERSON|EMAIL|PHONE|PLACE)_([1-9][0-9]{0,5})\]")
"""A pseudonym: `[PERSON_1]`."""
_TOKEN_SHAPED = re.compile(r"\[(person|email|phone|place)_([0-9]{1,12})\]", re.IGNORECASE)
"""Text of the input shaped like a pseudonym (any case): neutralized before sending."""
_LOOSE = re.compile(
    r"\[?(?<![A-Za-z])(?:person|email|phone|place)[ _#-]?[0-9]{1,6}(?![0-9])\]?", re.IGNORECASE
)
"""What a mangled pseudonym looks like (`Person 1`, `PERSON1`, `[place-2]`)."""

DEFAULT_BUCKETS = (0, 10, 100, 1000, 10000)
"""The bucket bounds of an `amount` field without a `buckets` option."""
WITHHELD_AMOUNT = "[AMOUNT]"

MAX_VOCABULARY_TERMS = 4096
MAX_TERM_CHARS = 200
MIN_TERM_CHARS = 2
"""Shorter values and terms are not matched in text (they would match everywhere)."""
MAX_VALUE_CHARS = 1024
"""Longest classed value given a reversible pseudonym (longer: withheld), which bounds what
re-identification can add per pseudonym."""
MAX_TERM_TOKENS = 48
"""Longest known value matched in text, in tokens (a white-space run is one): this bounds
the matching work per position, so it stays linear in the text whatever the input holds.
A longer value keeps its pseudonym in its own field but is not looked for in text."""
MIN_NAME_PART_LETTERS = 3
"""Shortest name part of a `person` value matched on its own."""
MIN_PHONE_DIGITS = 9
"""A phone number is also matched by its last `MIN_PHONE_DIGITS` digits, whatever the
separators (`06 12 34 56 78`, `+33 (0)6.12.34.56.78`)."""
MIN_AMOUNT_DIGITS = 3
"""Shortest rendering of an amount (in digits) matched in text."""
MAX_NOTES = 16
"""Most unresolved pseudonyms noted per step (each at most `MAX_NOTE_CHARS`)."""
MAX_NOTE_CHARS = 40

DEID_NOTE = (
    "Some values of the input were replaced by placeholders such as [PERSON_1], [EMAIL_1], "
    "[PHONE_1] or [PLACE_1]. Copy a placeholder exactly as written wherever you refer to "
    "that value; never guess, translate or reformat it."
)
"""Appended to the skill's prompt of a de-identified request (`horae.llm.base`)."""


class DeidError(DeidUnavailable):
    """De-identification cannot be done (e.g. the declared vocabulary cannot be read). The
    message never holds a vocabulary term or an input value."""


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _key(text: str) -> str:
    return _norm(text).casefold()


# --- tokens -------------------------------------------------------------------------------------

_FORMAT = re.compile(
    "[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]"
)
"""Format characters dropped before matching (soft hyphen, zero-width and bidi controls)."""
_SPACE = "\\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_TOKENS = re.compile(rf"[^\W\d_]+|\d+|[{_SPACE}]+|.", re.DOTALL)
"""A letter run, a digit run, a white-space run, or one other character."""
_PHONE_RUN = re.compile(rf"\+?\(?\d[\d{_SPACE}.\-()]*\d")
"""A run of digits and phone separators (`[\\s.\\-()]`)."""
_PHONE_SEPARATORS = frozenset(".-()+")


def _clean(text: str) -> str:
    """`text` as it is matched: NFKC, without format characters."""
    return _FORMAT.sub("", unicodedata.normalize("NFKC", text))


def _symbol(token: str) -> str:
    return " " if token[0].isspace() else token.casefold()


def _symbols(text: str) -> tuple[str, ...]:
    """The token symbols of a known value (cleaned, case-folded, outer white space dropped)."""
    symbols = [_symbol(match.group()) for match in _TOKENS.finditer(_clean(text))]
    while symbols and symbols[0] == " ":
        symbols.pop(0)
    while symbols and symbols[-1] == " ":
        symbols.pop()
    return tuple(symbols)


def _digits(text: str) -> str:
    return "".join(str(unicodedata.decimal(char)) for char in text if char.isdecimal())


@dataclass(frozen=True)
class Vocabulary:
    """Known terms (`class: term` lines of the `deidentification.vocabulary` file)."""

    terms: tuple[tuple[str, str], ...]
    """`(class, term)` in file order (terms NFC-normalized, duplicates dropped)."""
    digest: str
    """SHA-256 of the file's bytes: part of the LLM cache's stored key (never recorded)."""

    def __repr__(self) -> str:  # never print the terms
        return f"Vocabulary({len(self.terms)} terms)"

    @classmethod
    def parse(cls, data: bytes) -> "Vocabulary":
        """The vocabulary of a file's bytes; raises `DeidError` naming the line, never its
        content."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise DeidError("the vocabulary is not valid UTF-8") from None
        terms: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, colon, term = stripped.partition(":")
            name, term = name.strip(), _norm(term.strip())
            if not colon or name not in TOKEN_CLASSES:
                raise DeidError(
                    f"vocabulary line {number}: expected `<class>: <term>` with a class of "
                    f"{', '.join(TOKEN_CLASSES)}"
                )
            if not MIN_TERM_CHARS <= len(term) <= MAX_TERM_CHARS or any(
                unicodedata.category(char) == "Cc" for char in term
            ):
                raise DeidError(
                    f"vocabulary line {number}: a term has {MIN_TERM_CHARS} to "
                    f"{MAX_TERM_CHARS} characters and no control character"
                )
            if len(_symbols(term)) > MAX_TERM_TOKENS:
                raise DeidError(
                    f"vocabulary line {number}: a term has at most {MAX_TERM_TOKENS} words, "
                    "spaces and signs"
                )
            entry = (name, term)
            if (name, _key(term)) in seen:
                continue
            seen.add((name, _key(term)))
            terms.append(entry)
            if len(terms) > MAX_VOCABULARY_TERMS:
                raise DeidError(f"the vocabulary has more than {MAX_VOCABULARY_TERMS} terms")
        return cls(tuple(terms), hashlib.sha256(data).hexdigest())


# --- the classes of the input view ------------------------------------------------------------

Spec = tuple[str, tuple[float, ...]]
"""`(class, buckets)` of a classed field."""


@dataclass
class _Node:
    spec: Spec | None = None
    """`(class, buckets)` of the value this node ends on."""
    keys: dict[str, "_Node"] = field(default_factory=dict)
    items: "_Node | None" = None

    def classed_below(self) -> bool:
        children = [*self.keys.values(), *([] if self.items is None else [self.items])]
        return any(child.spec is not None or child.classed_below() for child in children)


def _buckets(options: Mapping[str, Any]) -> tuple[float, ...]:
    raw = options.get("buckets")
    if not isinstance(raw, list):
        return DEFAULT_BUCKETS
    bounds = sorted(
        {
            float(value)
            for value in raw
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        }
    )
    return tuple(bounds) or DEFAULT_BUCKETS


def field_classes(view_options: Mapping[str, Any]) -> dict[str, str]:
    """`{path: class}` of the input view options that name a class."""
    return {
        path: str(options["class"])
        for path, options in sorted(view_options.items())
        if isinstance(options, Mapping) and isinstance(options.get("class"), str)
    }


def _tree(view_options: Mapping[str, Mapping[str, Any]]) -> _Node:
    """The classed paths as a tree. Raises `DeidError` when one classed path lies under
    another (the skill contract already refuses overlapping view paths; defence in depth,
    decision 47: an inner class must never be silently shadowed by an outer one)."""
    root = _Node()
    for path, name in field_classes(view_options).items():
        node = root
        for step in view_steps(path):
            if node.spec is not None:
                raise DeidError(f"input view class of {path} lies under another classed path")
            if step == "[]":
                if node.items is None:
                    node.items = _Node()
                node = node.items
            else:
                node = node.keys.setdefault(step, _Node())
        if node.spec is not None or node.classed_below():  # pragma: no cover - parents first
            raise DeidError(f"input view class of {path} overlaps another classed path")
        node.spec = (name, _buckets(view_options[path]))
    return root


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _text(value: Any) -> str:
    return _norm(value) if isinstance(value, str) else canonical_json(value).decode()


def _number(bound: float) -> str:
    return str(int(bound)) if float(bound).is_integer() else repr(bound)


def bucket(value: Any, bounds: tuple[float, ...]) -> str:
    """The bucket label of an `amount`: `<lowest`, `a-b` or `>=highest`; `[AMOUNT]` for a
    value that is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return WITHHELD_AMOUNT
    if value < bounds[0]:
        return f"<{_number(bounds[0])}"
    for low, high in itertools.pairwise(bounds):
        if low <= value < high:
            return f"{_number(low)}-{_number(high)}"
    return f">={_number(bounds[-1])}"


def _grouped(whole: int, separator: str) -> str:
    return f"{whole:,}".replace(",", separator)


def amount_renderings(value: Any) -> set[str]:
    """How an amount is commonly written in text (decision 47 D): `1234.56`, `1234,56`,
    `1,234.56`, `1.234,56`, `1 234,56`, and its whole part (`1234`); only renderings of at
    least `MIN_AMOUNT_DIGITS` digits, not all zeros (it would match everywhere)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return set()
    if not math.isfinite(value) or abs(value) >= 1e15:
        return set()
    magnitude = abs(value)
    whole = int(magnitude)
    wholes = {str(whole)}
    if whole >= 1000:
        wholes |= {_grouped(whole, separator) for separator in (",", ".", " ", "'")}
    fractions = {"00"} if float(magnitude).is_integer() else set()
    if not float(magnitude).is_integer():
        fractions.add(f"{magnitude:.2f}".split(".")[1])
        shortest = repr(float(magnitude))
        if "." in shortest and "e" not in shortest:
            fractions.add(shortest.split(".")[1])
    found = set(wholes)
    for text, fraction, mark in itertools.product(wholes, fractions, (".", ",")):
        if mark not in text:
            found.add(f"{text}{mark}{fraction}")
    return {
        text
        for text in found
        if len(_digits(text)) >= MIN_AMOUNT_DIGITS and _digits(text).strip("0")
    }


def _name_parts(text: str) -> list[str]:
    """The parts of a person's name matched on their own: letter (and apostrophe) parts of
    at least `MIN_NAME_PART_LETTERS` letters, split at white space and hyphens."""
    parts = re.split(rf"[{_SPACE}\-\u2010-\u2015]+", _clean(text))
    if len(parts) < 2:
        return []
    return [
        part
        for part in parts
        if sum(char.isalpha() for char in part) >= MIN_NAME_PART_LETTERS
        and all(char.isalpha() or char in "'\u2019" for char in part)
    ]


# --- one input, pseudonymized ------------------------------------------------------------------


@dataclass
class Pseudonymized:
    """A viewed input, pseudonymized, and the in-memory mapping to put real values back."""

    data: Any
    """What the model is sent instead of the viewed input."""
    counts: dict[str, int]
    """How many distinct values got a pseudonym, per class; how many amounts were bucketed
    or replaced in text (`amount`); how many known values were replaced in text
    (`free_text`); how many pseudonym-shaped strings of the input were neutralized
    (`neutralized`) and classed values withheld for their length (`withheld`)."""
    _values: dict[str, str] = field(repr=False)
    """Pseudonym to its original value's text: never stored, logged or recorded."""
    _growth: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._growth = {
            token: max(0, len(json.dumps(value, ensure_ascii=False).encode()) - 2 - len(token))
            for token, value in self._values.items()
        }

    def __repr__(self) -> str:  # never print the mapping
        return f"Pseudonymized(counts={self.counts!r})"

    def summary(self) -> dict[str, Any]:
        """What the run record keeps (`llm[].request.deid`): the version, the digest of what
        was sent and the counts; never a value."""
        return {
            "version": DEID_VERSION,
            "input_sha256": sha256_hex(self.data),
            "counts": dict(sorted(self.counts.items())),
        }

    def growth(self, value: Any) -> int:
        """How many bytes (UTF-8, JSON-escaped) re-identifying `value` adds, computed without
        substituting (finding 1: the engine checks the bound first)."""
        total = 0
        for text in _strings(value):
            for match in TOKEN.finditer(text):
                total += self._growth.get(match.group(0), 0)
        return total

    def reidentify(self, value: Any) -> tuple[Any, list[str]]:
        """`value` (plain JSON) with its pseudonyms replaced by the real values, and the
        pseudonyms left unresolved (unknown or mangled, at most `MAX_NOTES`, sorted)."""
        notes: set[str] = set()
        result = self._walk(value, notes)
        return result, sorted(notes)[:MAX_NOTES]

    def _walk(self, value: Any, notes: set[str]) -> Any:
        if isinstance(value, str):
            return self._string(value, notes)
        if isinstance(value, list):
            return [self._walk(item, notes) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, child in value.items():
                real = self._string(key, notes)
                out[real if real not in out and real not in value else key] = self._walk(
                    child, notes
                )
            return out
        return value

    def _string(self, text: str, notes: set[str]) -> str:
        for loose in _LOOSE.finditer(text):
            if loose.group(0) not in self._values:
                notes.add(loose.group(0)[:MAX_NOTE_CHARS])

        def put_back(match: re.Match[str]) -> str:
            return self._values.get(match.group(0), match.group(0))

        return TOKEN.sub(put_back, text)


def _strings(value: Any) -> Iterator[str]:
    """Every string and object key of plain JSON `value`."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            yield from item
            stack.extend(item.values())


@dataclass(frozen=True)
class _Known:
    """A value with a reversible pseudonym: its class, its token symbols and its text."""

    name: str
    symbols: tuple[str, ...]
    text: str


class _Assigner:
    """Pseudonyms per class, in order of first occurrence."""

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, tuple[str, ...]], str] = {}
        self.values: dict[str, str] = {}
        self.numbers = dict.fromkeys(TOKEN_CLASSES, 0)

    def token(self, known: _Known) -> str:
        key = (known.name, known.symbols)
        token = self.tokens.get(key)
        if token is None:
            self.numbers[known.name] += 1
            token = f"[{_LABELS[known.name]}_{self.numbers[known.name]}]"
            self.tokens[key] = token
            self.values[token] = known.text
        return token


_END = ""
"""The trie key of what a complete known value maps to (a symbol is never empty)."""


class _Trie:
    """Known values by their token symbols: a `_Known` (a pseudonym) or a `str` (an amount's
    bucket label). The first value inserted for a sequence wins."""

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self.size = 0

    def add(self, symbols: tuple[str, ...], value: "_Known | str") -> None:
        if not symbols or len(symbols) > MAX_TERM_TOKENS:
            return
        node = self.root
        for symbol in symbols:
            node = node.setdefault(symbol, {})
        if _END not in node:
            node[_END] = value
            self.size += 1

    def matches(self, symbols: list[str]) -> Iterator[tuple[int, int, "_Known | str"]]:
        """`(start, end, value)` of the leftmost-longest, non-overlapping matches. Linear in
        `symbols` times the longest known value (at most `MAX_TERM_TOKENS` tokens)."""
        count, start = len(symbols), 0
        while start < count:
            node = self.root.get(symbols[start])
            best: tuple[int, Any] | None = None
            index = start
            while node is not None:
                if _END in node:
                    best = (index + 1, node[_END])
                index += 1
                if index >= count:
                    break
                node = node.get(symbols[index])
            if best is None:
                start += 1
                continue
            yield start, best[0], best[1]
            start = best[0]


class _Pseudonymizer:
    def __init__(self, tree: _Node, vocabulary: Vocabulary | None) -> None:
        self.tree = tree
        self.vocabulary = vocabulary
        self.assigner = _Assigner()
        self.counts: dict[str, int] = {}
        self.known: list[_Known] = []
        self.amounts: list[tuple[Any, tuple[float, ...]]] = []
        self.trie = _Trie()
        self.phones: dict[str, _Known] = {}

    def count(self, what: str, number: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + number

    # pass 1: the classed values, in canonical order

    def _classed(self, name: str, value: Any) -> _Known | None:
        """The known value of a token-class scalar, or `None` (empty, or withheld)."""
        if not _is_scalar(value) or value == "":
            return None
        text = _text(value)
        if len(text) > MAX_VALUE_CHARS:
            return None
        return _Known(name, _symbols(text), text)

    def collect(self, value: Any, node: _Node | None, spec: Spec | None) -> None:
        if node is not None and node.spec is not None:
            spec, node = node.spec, None
        if isinstance(value, dict):
            for key in sorted(value):
                if spec is not None and spec[0] in TOKEN_CLASSES:
                    self._remember(self._classed(spec[0], key))
                child = None if node is None else node.keys.get(key)
                self.collect(value[key], child, spec)
        elif isinstance(value, list):
            child = None if node is None else node.items
            for item in value:
                self.collect(item, child, spec)
        elif spec is not None and spec[0] in TOKEN_CLASSES:
            self._remember(self._classed(spec[0], value))
        elif spec is not None and spec[0] == AMOUNT:
            self.amounts.append((value, spec[1]))

    def _remember(self, known: _Known | None) -> None:
        if known is None or not known.symbols:
            return
        if (known.name, known.symbols) not in self.assigner.tokens:
            self.known.append(known)
        self.assigner.token(known)

    def build(self) -> None:
        """The trie and the phone keys: classed values first, then the vocabulary, then name
        parts, then amount renderings (the first one inserted for a sequence wins)."""
        terms = list(self.known)
        for name, term in () if self.vocabulary is None else self.vocabulary.terms:
            terms.append(_Known(name, _symbols(term), term))
        for known in terms:
            if len(known.text) >= MIN_TERM_CHARS:
                self.trie.add(known.symbols, known)
            if known.name == "phone":
                digits = _digits(known.text)
                if len(digits) >= MIN_PHONE_DIGITS:
                    self.phones.setdefault(digits[-MIN_PHONE_DIGITS:], known)
        for known in terms:
            if known.name == "person":
                for part in _name_parts(known.text):
                    self.trie.add(_symbols(part), known)
        for value, bounds in self.amounts:
            label = bucket(value, bounds)
            for text in sorted(amount_renderings(value)):
                self.trie.add(_symbols(text), label)

    # pass 2: the rewritten input

    def rewrite(self, value: Any, node: _Node | None, spec: Spec | None) -> Any:
        if node is not None and node.spec is not None:
            spec, node = node.spec, None
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key in sorted(value):
                if spec is not None and spec[0] in TOKEN_CLASSES:
                    new_key = self.token_leaf(spec[0], key)
                else:
                    new_key = self.scan(key)
                if new_key in out:
                    raise DeidError("two object keys of the input are the same once pseudonymized")
                child = None if node is None else node.keys.get(key)
                out[new_key] = self.rewrite(value[key], child, spec)
            return out
        if isinstance(value, list):
            child = None if node is None else node.items
            return [self.rewrite(item, child, spec) for item in value]
        if spec is not None and spec[0] in TOKEN_CLASSES:
            return self.token_leaf(spec[0], value)
        if spec is not None and spec[0] == AMOUNT:
            if value is None:
                return value
            self.count(AMOUNT)
            return bucket(value, spec[1])
        if isinstance(value, str):
            return self.scan(value)
        return value

    def token_leaf(self, name: str, value: Any) -> Any:
        if not _is_scalar(value) or value == "":
            return value
        known = self._classed(name, value)
        if known is None or not known.symbols:
            self.count("withheld")
            return f"[{_LABELS[name]}]"
        return self.assigner.token(known)

    def scan(self, text: str) -> str:
        """`text` with its known values replaced (module docstring); `text` itself when
        nothing is found."""
        cleaned = _clean(text)
        cleaned, neutralized = _TOKEN_SHAPED.subn(r"(\1_\2)", cleaned)
        if neutralized:
            self.count("neutralized", neutralized)
        pieces: list[str] = []
        found = neutralized > 0
        position = 0
        # T10 belt and braces (decision 47 J): a match overlapping a resolved secret is not
        # replaced, so a secret is never sent in part (the engine refuses it before this).
        secrets = current_redactor().text_spans(cleaned)
        for start, end, known in self._phone_spans(cleaned):
            if _overlaps(start, end, secrets):
                continue
            found |= self._terms(cleaned[position:start], pieces, position, secrets)
            pieces.append(self.assigner.token(known))
            self.count(FREE_TEXT)
            position, found = end, True
        found |= self._terms(cleaned[position:], pieces, position, secrets)
        return "".join(pieces) if found else text

    def _terms(
        self, text: str, pieces: list[str], base: int, secrets: list[tuple[int, int, str]]
    ) -> bool:
        """`text` (at offset `base` of the scanned string) with its known values replaced,
        appended to `pieces`, except the matches overlapping a span of `secrets`."""
        if not text:
            return False
        if not self.trie.size:
            pieces.append(text)
            return False
        matched = list(_TOKENS.finditer(text))
        tokens = [match.group() for match in matched]
        position, found = 0, False
        for start, end, value in self.trie.matches([_symbol(token) for token in tokens]):
            first, last = base + matched[start].start(), base + matched[end - 1].end()
            if _overlaps(first, last, secrets):
                continue
            pieces.extend(tokens[position:start])
            if isinstance(value, str):
                pieces.append(value)
                self.count(AMOUNT)
            else:
                pieces.append(self.assigner.token(value))
                self.count(FREE_TEXT)
            position, found = end, True
        pieces.extend(tokens[position:])
        return found

    def _phone_spans(self, text: str) -> Iterator[tuple[int, int, _Known]]:
        """Spans of `text` holding a known phone number's last `MIN_PHONE_DIGITS` digits,
        whatever the separators, with up to four digits of prefix (country or trunk code)."""
        if not self.phones:
            return
        for run in _PHONE_RUN.finditer(text):
            positions = [run.start() + i for i, char in enumerate(run.group()) if char.isdecimal()]
            digits = _digits(run.group())
            index, floor = 0, run.start()
            while index + MIN_PHONE_DIGITS <= len(digits):
                known = self.phones.get(digits[index : index + MIN_PHONE_DIGITS])
                if known is None:
                    index += 1
                    continue
                start, end = positions[index], positions[index + MIN_PHONE_DIGITS - 1] + 1
                prefix = 0
                while start > floor:
                    char = text[start - 1]
                    if char.isdecimal():
                        if prefix == 4:
                            break
                        prefix += 1
                    elif not (char.isspace() or char in _PHONE_SEPARATORS):
                        break  # pragma: no cover - a run holds digits and separators only
                    start -= 1
                while text[start] not in "+(" and not text[start].isdecimal():
                    start += 1
                yield start, end, known
                index += MIN_PHONE_DIGITS
                floor = end


def _overlaps(start: int, end: int, spans: list[tuple[int, int, str]]) -> bool:
    """Whether `[start, end)` overlaps one of `spans`."""
    return any(start < span_end and span_start < end for span_start, span_end, _ in spans)


def pseudonymize(
    data: Any,
    view_options: Mapping[str, Mapping[str, Any]],
    vocabulary: Vocabulary | None = None,
) -> Pseudonymized:
    """`data` (the viewed input) with its classed fields pseudonymized and its other strings
    scanned for known values (module docstring). Raises `DeidError`."""
    work = _Pseudonymizer(_tree(view_options), vocabulary)
    work.collect(data, work.tree, None)
    work.build()
    sent = work.rewrite(data, work.tree, None)
    counts = dict(work.counts)
    for name, number in work.assigner.numbers.items():
        if number:
            counts[name] = number
    return Pseudonymized(sent, counts, dict(work.assigner.values))


# --- the instance's policy -----------------------------------------------------------------------


class Deidentifier:
    """The instance's de-identification policy: which aliases are cloud, and the vocabulary.

    `vocabulary_error` is why the declared vocabulary could not be loaded: de-identification
    then fails closed (`DeidError`), so nothing is sent without it."""

    def __init__(
        self,
        models: Mapping[str, ModelAlias],
        vocabulary: Vocabulary | None = None,
        *,
        vocabulary_error: str | None = None,
    ) -> None:
        self.models = dict(models)
        self.vocabulary = vocabulary
        self.vocabulary_error = vocabulary_error

    def __repr__(self) -> str:
        return f"Deidentifier({sorted(self.models)}, {self.vocabulary!r})"

    def applies(self, model_alias: str) -> bool:
        """Whether `model_alias` is a cloud alias (an alias not declared counts as cloud)."""
        spec = self.models.get(model_alias)
        return spec is None or not spec.is_local

    def cache_settings(self, model_alias: str | None) -> dict[str, Any] | None:
        """What joins the LLM cache's stored key for `model_alias` (decision 47 F): `None`
        for a local alias (its key is unchanged), else the version and the vocabulary's
        digest."""
        if model_alias is None or not self.applies(model_alias):
            return None
        digest = None if self.vocabulary is None else self.vocabulary.digest
        if self.vocabulary_error is not None:
            digest = "unavailable"
        return {"version": DEID_VERSION, "vocabulary": digest}

    def pseudonymize(
        self, model_alias: str, view_options: Mapping[str, Mapping[str, Any]], data: Any
    ) -> Pseudonymized | None:
        """`data` pseudonymized for `model_alias`, or `None` when nothing is to be done (a
        local alias, or no field class declared). Raises `DeidError`."""
        if not field_classes(view_options) or not self.applies(model_alias):
            return None
        if self.vocabulary_error is not None:
            raise DeidError(self.vocabulary_error)
        return pseudonymize(data, view_options, self.vocabulary)


def load_deidentifier(
    models: Mapping[str, ModelAlias], secrets: SecretResolver, vocabulary: str | None
) -> Deidentifier:
    """The instance's policy: `models` and, when `deidentification.vocabulary` names one,
    the vocabulary file of the secrets directory, read like a secret file
    (`SecretResolver.read_file`). A vocabulary that cannot be read or parsed is not an error
    here: the policy then fails closed on the first step that needs it (`DeidError`), and
    `horae doctor` reports it (`vocabulary_problem`)."""
    if vocabulary is None:
        return Deidentifier(models)
    problem = vocabulary_problem(secrets, vocabulary)
    if isinstance(problem, Vocabulary):
        return Deidentifier(models, problem)
    return Deidentifier(models, vocabulary_error=problem)


def vocabulary_problem(secrets: SecretResolver, file_name: str) -> "Vocabulary | str":
    """The vocabulary `file_name` of the secrets directory, or why it cannot be used (a
    message naming the file, never its content)."""
    try:
        data = secrets.read_file(file_name)
    except SecretError as error:
        return f"the de-identification vocabulary cannot be read: {error.reason}"
    try:
        return Vocabulary.parse(data)
    except DeidError as error:
        return f"the de-identification vocabulary {file_name} is invalid: {error}"
    finally:
        del data


__all__ = [
    "AMOUNT",
    "DEFAULT_BUCKETS",
    "DEID_NOTE",
    "DEID_VERSION",
    "FREE_TEXT",
    "MAX_VALUE_CHARS",
    "TOKEN",
    "TOKEN_CLASSES",
    "DeidError",
    "Deidentifier",
    "Pseudonymized",
    "Vocabulary",
    "amount_renderings",
    "bucket",
    "field_classes",
    "load_deidentifier",
    "pseudonymize",
    "vocabulary_problem",
]
