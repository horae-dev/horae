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

- `person`, `email`, `phone`, `place`: every scalar leaf becomes a pseudonym, `[PERSON_1]`,
  `[EMAIL_2]`… (reversible);
- `free_text`: the vocabulary's terms and the values of the classed fields above, matched as
  whole words (case-insensitive, NFC), become their pseudonym (reversible);
- `amount`: a bucket label (`50-200`, `<0`, `>=1000`; the `buckets:` option, else powers of
  ten); a value that is not a number becomes `[AMOUNT]` (not reversible).

**Stable per run.** Pseudonyms are numbered per class in order of first occurrence: first the
classed leaves in canonical order (sorted keys, array order), then the free-text matches.
The same value (NFC, case-folded) always gets the same pseudonym within the input, and the
same input always gives the same pseudonymized request: a replay, or a cache hit, recomputes
the same mapping from the same input (no key, no stored mapping).

**The mapping stays in memory.** It lives only in the `Pseudonymized` object of the step; the
run record keeps what the provider saw (`llm[].response` and `llm[].intents` are the
pseudonymized output, `llm[].request.deid` a digest and counts), the LLM cache stores the
pseudonymized output (re-identified on a hit), and the usage ledger holds no data.

**Re-identification** replaces every pseudonym of this input found in a string (or an object
key) of the output and of the raw intents; a string that is exactly one pseudonym gets the
original value back with its JSON type. A pseudonym the input never issued, or one the model
mangled (`Person 1`, `PERSON1`), is left as it is and noted (`unresolved`); no repair retry
is made (a retry cannot fix what the model invented). Bucketed amounts stay buckets.

**Limits** (documented, decision 47): values in fields the skill did not classify are sent as
they are (the input view is the primary control); free-text matching over-matches a term that
is also a common word and misses nicknames and typos; object keys are never pseudonymized.
"""

import hashlib
import itertools
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.instance import ModelAlias
from horae.contracts.skill import view_steps
from horae.engine.canonical import canonical_json, sha256_hex
from horae.engine.ports import DeidUnavailable
from horae.secrets import SecretError, SecretResolver

DEID_VERSION = 1
"""The version of the transforms and of the pseudonym format: part of the LLM cache's stored
key for a cloud alias (decision 47 F)."""

TOKEN_CLASSES = ("person", "email", "phone", "place")
"""The classes replaced by a reversible pseudonym."""
FREE_TEXT = "free_text"
AMOUNT = "amount"
_LABELS = {name: name.upper() for name in TOKEN_CLASSES}
_CLASS_OF = {label: name for name, label in _LABELS.items()}

TOKEN = re.compile(r"\[(PERSON|EMAIL|PHONE|PLACE)_([1-9][0-9]{0,5})\]")
"""A pseudonym: `[PERSON_1]`."""
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
"""Shorter values and terms are not matched in free text (they would match everywhere)."""
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
    message never holds a vocabulary term."""


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _key(text: str) -> str:
    return _norm(text).casefold()


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
            entry = (name, term)
            if (name, _key(term)) in seen:
                continue
            seen.add((name, _key(term)))
            terms.append(entry)
            if len(terms) > MAX_VOCABULARY_TERMS:
                raise DeidError(f"the vocabulary has more than {MAX_VOCABULARY_TERMS} terms")
        return cls(tuple(terms), hashlib.sha256(data).hexdigest())


# --- the classes of the input view ------------------------------------------------------------


@dataclass
class _Node:
    spec: tuple[str, tuple[float, ...]] | None = None
    """`(class, buckets)` of the value this node ends on."""
    keys: dict[str, "_Node"] = field(default_factory=dict)
    items: "_Node | None" = None


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
    root = _Node()
    for path, name in field_classes(view_options).items():
        node = root
        for step in view_steps(path):
            if step == "[]":
                if node.items is None:
                    node.items = _Node()
                node = node.items
            else:
                node = node.keys.setdefault(step, _Node())
        node.spec = (name, _buckets(view_options[path]))
    return root


def _leaves(value: Any, node: _Node | None, spec: tuple[str, tuple[float, ...]] | None) -> Any:
    """Yield `(class, buckets, leaf)` for every scalar leaf in canonical order."""
    if node is not None and node.spec is not None:
        spec, node = node.spec, None
    if isinstance(value, dict):
        for key in sorted(value):
            child = None if node is None else node.keys.get(key)
            yield from _leaves(value[key], child, spec)
    elif isinstance(value, list):
        child = None if node is None else node.items
        for item in value:
            yield from _leaves(item, child, spec)
    elif spec is not None:
        yield spec[0], spec[1], value


def _rewrite(
    value: Any,
    node: _Node | None,
    spec: tuple[str, tuple[float, ...]] | None,
    leaf: Any,
) -> Any:
    if node is not None and node.spec is not None:
        spec, node = node.spec, None
    if isinstance(value, dict):
        return {
            key: _rewrite(value[key], None if node is None else node.keys.get(key), spec, leaf)
            for key in sorted(value)
        }
    if isinstance(value, list):
        child = None if node is None else node.items
        return [_rewrite(item, child, spec, leaf) for item in value]
    if spec is None:
        return value
    return leaf(spec[0], spec[1], value)


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


# --- one input, pseudonymized ------------------------------------------------------------------


@dataclass
class Pseudonymized:
    """A viewed input, pseudonymized, and the in-memory mapping to put real values back."""

    data: Any
    """What the model is sent instead of the viewed input."""
    counts: dict[str, int]
    """How many distinct values got a pseudonym, per class, and how many amounts were
    bucketed (`amount`) and free-text matches replaced (`free_text`)."""
    _values: dict[str, Any] = field(repr=False)
    """Pseudonym to its original value: never stored, logged or recorded."""

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

    def reidentify(self, value: Any) -> tuple[Any, list[str]]:
        """`value` (plain JSON) with its pseudonyms replaced by the real values, and the
        pseudonyms left unresolved (unknown or mangled, at most `MAX_NOTES`, sorted)."""
        notes: set[str] = set()
        result = self._walk(value, notes)
        return result, sorted(notes)[:MAX_NOTES]

    def _walk(self, value: Any, notes: set[str]) -> Any:
        if isinstance(value, str):
            return self._string(value, notes, whole=True)
        if isinstance(value, list):
            return [self._walk(item, notes) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, child in value.items():
                real = self._string(key, notes, whole=False)
                out[real if real not in out and real not in value else key] = self._walk(
                    child, notes
                )
            return out
        return value

    def _string(self, text: str, notes: set[str], *, whole: bool) -> Any:
        if whole and text in self._values:
            return self._values[text]
        for loose in _LOOSE.finditer(text):
            if loose.group(0) not in self._values:
                notes.add(loose.group(0)[:MAX_NOTE_CHARS])

        def put_back(match: re.Match[str]) -> str:
            token = match.group(0)
            if token not in self._values:
                return token
            return _text(self._values[token])

        return TOKEN.sub(put_back, text)


class _Assigner:
    """Pseudonyms per class, in order of first occurrence."""

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str], str] = {}
        self.values: dict[str, Any] = {}
        self.numbers = dict.fromkeys(TOKEN_CLASSES, 0)

    def token(self, name: str, value: Any) -> str:
        key = (name, _key(_text(value)))
        token = self.tokens.get(key)
        if token is None:
            self.numbers[name] += 1
            token = f"[{_LABELS[name]}_{self.numbers[name]}]"
            self.tokens[key] = token
            self.values[token] = _norm(value) if isinstance(value, str) else value
        return token


def _matcher(terms: Iterable[tuple[str, str]]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    """One whole-word, case-insensitive pattern for `terms` (longest first), and the class of
    each term by its case-folded form (the first one given wins)."""
    classes: dict[str, str] = {}
    surfaces: dict[str, str] = {}
    for name, term in terms:
        if len(term) < MIN_TERM_CHARS:
            continue
        folded = _key(term)
        classes.setdefault(folded, name)
        surfaces.setdefault(folded, term)
    if not surfaces:
        return None, classes
    ordered = sorted(surfaces.values(), key=lambda term: (-len(term), term))
    alternation = "|".join(re.escape(term) for term in ordered)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE), classes


def _class_of(match: str, classes: Mapping[str, str]) -> str | None:
    found = classes.get(_key(match))
    if found is not None:
        return found
    for folded, name in classes.items():  # pragma: no cover - a case pair casefold() splits
        if re.fullmatch(re.escape(folded), match, re.IGNORECASE):
            return name
    return None  # pragma: no cover


def pseudonymize(
    data: Any,
    view_options: Mapping[str, Mapping[str, Any]],
    vocabulary: Vocabulary | None = None,
) -> Pseudonymized:
    """`data` (the viewed input) with its classed fields pseudonymized (module docstring)."""
    tree = _tree(view_options)
    assigner = _Assigner()
    counts: dict[str, int] = {}
    known: list[tuple[str, str]] = []
    for name, _bounds, value in _leaves(data, tree, None):
        if name in TOKEN_CLASSES and _is_scalar(value) and value != "":
            if (name, _key(_text(value))) not in assigner.tokens:
                known.append((name, _text(value)))
            assigner.token(name, value)
    terms = known + list(vocabulary.terms if vocabulary is not None else ())
    pattern, classes = _matcher(terms)

    def replace_terms(text: str) -> str:
        if pattern is None:
            return text

        def one(match: re.Match[str]) -> str:
            name = _class_of(match.group(0), classes)
            if name is None:  # pragma: no cover - every match comes from a known term
                return match.group(0)
            counts[FREE_TEXT] = counts.get(FREE_TEXT, 0) + 1
            return assigner.token(name, match.group(0))

        return pattern.sub(one, _norm(text))

    # The free-text matches are numbered after every classed leaf, in canonical order.
    for name, _bounds, value in _leaves(data, tree, None):
        if name == FREE_TEXT and isinstance(value, str):
            replace_terms(value)
    counts.clear()

    def leaf(name: str, bounds: tuple[float, ...], value: Any) -> Any:
        if name in TOKEN_CLASSES:
            if not _is_scalar(value) or value == "":
                return value
            return assigner.token(name, value)
        if name == AMOUNT:
            if value is None:
                return value
            counts[AMOUNT] = counts.get(AMOUNT, 0) + 1
            return bucket(value, bounds)
        if name == FREE_TEXT and isinstance(value, str):
            return replace_terms(value)
        return value

    sent = _rewrite(data, tree, None, leaf)
    for name, number in assigner.numbers.items():
        if number:
            counts[name] = number
    return Pseudonymized(sent, counts, dict(assigner.values))


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
    "TOKEN",
    "TOKEN_CLASSES",
    "DeidError",
    "Deidentifier",
    "Pseudonymized",
    "Vocabulary",
    "bucket",
    "field_classes",
    "load_deidentifier",
    "pseudonymize",
    "vocabulary_problem",
]
