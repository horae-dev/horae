# SPDX-License-Identifier: Apache-2.0
"""The free-text allowlist of the store's secret gate (#53, schemas decision 43 B and C).

A run record (and a step row) mixes **free text** (what a plugin, a model or a user wrote:
data, parameters, messages, stderr, note content…) with **identifiers** the system needs as
they are (the run id, the pipeline name, timestamps, statuses, kinds, plugin names and refs,
tokens, digests, targets, fingerprints). The gate redacts the free text only, at the paths
listed here; a secret found anywhere else is **flagged, not rewritten** (rewriting an
identifier would make the record invalid, or break undo, replay or the run's own id). A
number whose text is a secret's value is flagged and kept (its type never changes).

Free-text paths of a run record (JSON, `model_dump(mode="json")`):

- `context.vars.<name>` (the values; the names are identifiers);
- `inputs[*].params`, `inputs[*].data`, `inputs[*].intents`;
- every step's free text (below);
- `llm[*].request`, `llm[*].response`, `llm[*].intents`, `llm[*].repair[*]`,
  `llm[*].schema_errors[*]`;
- `intents[*].intent.<field>` for the payload fields `content`, `heading`, `title` and `body`
  (not `type`, `path`, `block`, `fingerprint`, `task`, `status`, dates, `url`, `link`);
- `violations[*].detail`, `error`.

Free-text paths of a step: `output`, `error.message`, `stderr`, `calls[*].params`,
`calls[*].intents`. Inside a free-text JSON value, string leaves and object keys are redacted
(`Redactor.scrub`: a key that becomes equal to another is made unique and reported).
"""

from dataclasses import dataclass, field
from typing import Any

from horae.secrets.redact import Redactor

INTENT_TEXT_FIELDS = ("content", "heading", "title", "body")
"""The intent payload fields that are free text."""


@dataclass
class Findings:
    """What the gate did to one value."""

    redacted: set[str] = field(default_factory=set)
    """Secrets replaced in free text."""
    flagged: set[str] = field(default_factory=set)
    """Secrets found in identifiers or numbers, kept as written."""
    collisions: set[str] = field(default_factory=set)
    """Secrets whose redaction made two object keys equal (the later one renamed)."""


class _Walker:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.found = Findings()

    def text(self, holder: dict[str, Any] | list[Any], key: Any) -> None:
        value = holder[key]
        if isinstance(value, str):
            holder[key], names = self.redactor.redact_text(value)
            self.found.redacted |= names

    def data(self, holder: dict[str, Any] | list[Any], key: Any) -> None:
        result = self.redactor.scrub(holder[key], numbers="flag")
        holder[key] = result.value
        self.found.redacted |= result.redacted
        self.found.flagged |= result.flagged
        self.found.collisions |= result.collisions

    def step(self, step: dict[str, Any]) -> None:
        if "output" in step:
            self.data(step, "output")
        if isinstance(step.get("error"), dict):
            self.text(step["error"], "message")
        if "stderr" in step:
            self.text(step, "stderr")
        for call in step.get("calls") or ():
            for key in ("params", "intents"):
                if key in call:
                    self.data(call, key)

    def record(self, record: dict[str, Any]) -> None:
        context = record.get("context")
        if isinstance(context, dict) and isinstance(context.get("vars"), dict):
            for name in context["vars"]:
                self.text(context["vars"], name)
        for snapshot in record.get("inputs") or ():
            for key in ("params", "data", "intents"):
                if key in snapshot:
                    self.data(snapshot, key)
        for step in record.get("steps") or ():
            self.step(step)
        for call in record.get("llm") or ():
            for key in ("request", "response", "intents"):
                if key in call:
                    self.data(call, key)
            for key in ("repair", "schema_errors"):
                for index in range(len(call.get(key) or ())):
                    self.text(call[key], index)
        for proposed in record.get("intents") or ():
            intent = proposed.get("intent")
            if isinstance(intent, dict):
                for key in INTENT_TEXT_FIELDS:
                    if key in intent:
                        self.data(intent, key)
        for violation in record.get("violations") or ():
            self.text(violation, "detail")
        if "error" in record:
            self.text(record, "error")

    def identifiers(self, value: Any) -> None:
        """Flag what is left (identifiers and numbers): the free text was redacted already."""
        self.found.flagged |= self.redactor.find(value)


def scrub_record(data: dict[str, Any], redactor: Redactor) -> Findings:
    """Redact the free text of the JSON run record `data` in place; flag the rest."""
    walker = _Walker(redactor)
    if redactor:
        walker.record(data)
        walker.identifiers(data)
    return walker.found


def scrub_step(data: dict[str, Any], redactor: Redactor) -> Findings:
    """Redact the free text of the JSON step record `data` in place; flag the rest."""
    walker = _Walker(redactor)
    if redactor:
        walker.step(data)
        walker.identifiers(data)
    return walker.found


__all__ = ["INTENT_TEXT_FIELDS", "Findings", "scrub_record", "scrub_step"]
