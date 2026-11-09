# SPDX-License-Identifier: Apache-2.0
"""The public replay backend (#134, schemas decision 64): recorded LLM output instead of a
model call.

`ReplayBackend` wraps a real provider backend: its `render` is the real one (so the request,
its digests and its cost bound are the ones a live call would have), and its `send` returns a
recording instead of calling the provider. Nothing leaves the host and no secret is read. It
replaces the private `horae.evals._harness._Replay`.

What answers is either one fixed `CompletionResult` (the eval harness: the recording of the
case under test) or `Recordings`, a library of `RecordedResponse` files looked up by **request
digest**: the SHA-256 of the skill's prompt, of the viewed input and of the output schema
(`horae.contracts.evals.RecordedRequest`, what `horae eval --live --record` writes). A request
no recording answers is an `LlmError` of kind `replay_miss`, never a live call.

`replay_backends(models, recordings)` is the `ModelBackends` that `horae run --llm-replay
<recordings>` gives the runner.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from horae.contracts.evals import RecordedRequest, RecordedResponse
from horae.contracts.instance import ModelAlias
from horae.engine.canonical import canonical_json
from horae.engine.ports import LlmError
from horae.evals._cases import EvalError, load_recorded
from horae.llm.backends import FACTORIES
from horae.llm.base import (
    BackendFactory,
    Completion,
    CompletionResult,
    ModelBackends,
    PromptSource,
    ProviderBackend,
    RequestPrompts,
    Unvalidated,
    Validator,
    output_attempts,
    production_validator,
    summary,
)
from horae.secrets import SecretResolver

REPLAY_MISS = "replay_miss"
"""The `LlmError` kind of a request no recording answers."""
MAX_RECORDINGS = 4096
"""Most recording files read from a directory."""
MAX_DEPTH = 6
"""Deepest folder (below the given one) recordings are read from."""
_NO_SECRETS = SecretResolver({}, Path("/nonexistent"), {})


def completion_digest(completion: Completion) -> RecordedRequest:
    """The request digest of `completion` (what a recording keeps as `request`)."""
    shown = summary(completion, "anthropic", b"")  # the three digests do not depend on either
    return RecordedRequest(
        system_sha256=shown["system_sha256"],
        input_sha256=shown["input_sha256"],
        schema_sha256=shown["schema_sha256"],
    )


def _key(request: RecordedRequest) -> tuple[str, str, str]:
    return (request.system_sha256, request.input_sha256, request.schema_sha256)


@dataclass
class Recordings:
    """Recorded responses, found by request digest."""

    responses: list[tuple[str, RecordedResponse]] = field(default_factory=list)
    """`(where it was read from, the recording)`."""

    def add(self, source: str, response: RecordedResponse) -> None:
        for other_source, other in self.responses:
            same = _key(other.request) == _key(response.request) and other.backend == (
                response.backend
            )
            if same and canonical_json(other.output) != canonical_json(response.output):
                raise EvalError(
                    f"{source}: answers the same request as {other_source} "
                    f"(backend {response.backend}) with a different output"
                )
        self.responses.append((source, response))

    def find(self, request: RecordedRequest, backend: str) -> RecordedResponse | None:
        """The recording of `request`, preferring one made for `backend`."""
        found = [r for _, r in self.responses if _key(r.request) == _key(request)]
        for response in found:
            if response.backend == backend:
                return response
        return found[0] if found else None

    def near(self, request: RecordedRequest) -> list[str]:
        """What a recording of another request shares with `request` (for a miss's message)."""
        shared: set[str] = set()
        for _, response in self.responses:
            for name, mine, theirs in (
                ("prompt", request.system_sha256, response.request.system_sha256),
                ("schema", request.schema_sha256, response.request.schema_sha256),
            ):
                if mine == theirs:
                    shared.add(name)
        return sorted(shared)

    @classmethod
    def load(cls, path: Path) -> "Recordings":
        """The recordings at `path`: one recording file, or a folder searched for `*.json`
        recordings (`evals/recorded/<backend>/<case>.json` of a skill, or a folder of several
        skills; dotfiles and links are skipped, at most `MAX_RECORDINGS` files, `MAX_DEPTH`
        folders deep). A file that is not a recording is an `EvalError` naming it; so is a
        path with no recording at all."""
        found = cls()
        if path.is_file() and not path.is_symlink():
            files = [path]
        elif path.is_dir() and not path.is_symlink():
            files = _json_files(path)
        else:
            raise EvalError(f"{path}: not a recording file or a folder of recordings")
        for file in files:
            response = load_recorded(file)
            if response is not None:
                found.add(str(file), response)
        if not found.responses:
            raise EvalError(f"{path}: no recorded response (*.json) found")
        return found


def _json_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for folder, names, entries in os.walk(root, followlinks=False):
        depth = len(Path(folder).relative_to(root).parts)
        names[:] = sorted(n for n in names if not n.startswith(".")) if depth < MAX_DEPTH else []
        for entry in sorted(entries):
            if entry.startswith(".") or not entry.endswith(".json"):
                continue
            file = Path(folder) / entry
            if file.is_symlink():
                continue
            files.append(file)
            if len(files) > MAX_RECORDINGS:
                raise EvalError(f"{root}: more than {MAX_RECORDINGS} recordings")
    return files


class ReplayBackend:
    """A `ProviderBackend` whose `render` is `inner`'s and whose `send` returns a recording.

    `source` is a fixed `CompletionResult`, or `Recordings` looked up by the request's digest.
    `sent` keeps every payload `send` was asked for (what a live call would have sent), for
    tests."""

    def __init__(self, inner: ProviderBackend, source: CompletionResult | Recordings) -> None:
        self._inner, self._source = inner, source
        self._digests: dict[str, RecordedRequest] = {}
        self.sent: list[Mapping[str, Any]] = []

    @property
    def name(self) -> Any:
        return self._inner.name

    @property
    def may_write_cache(self) -> bool:
        return getattr(self._inner, "may_write_cache", False) is True

    @property
    def output_attempts(self) -> int:
        return output_attempts(self._inner)

    def render(self, completion: Completion) -> dict[str, Any]:
        payload = self._inner.render(completion)
        if isinstance(self._source, Recordings):
            self._digests[_payload_key(payload)] = completion_digest(completion)
        return payload

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.sent.append(payload)
        if not isinstance(self._source, Recordings):
            return self._source
        digest = self._digests.get(_payload_key(payload))
        if digest is None:
            raise LlmError(REPLAY_MISS, "replay: a request that was not rendered here")
        response = self._source.find(digest, str(self.name))
        if response is None:
            shared = self._source.near(digest)
            hint = (
                f" (a recording shares the {' and '.join(shared)}: the input changed)"
                if shared
                else ""
            )
            raise LlmError(
                REPLAY_MISS,
                "no recording answers this request: "
                f"prompt {digest.system_sha256[:12]}, input {digest.input_sha256[:12]}, "
                f"schema {digest.schema_sha256[:12]}{hint}; "
                "re-record with `horae eval --live --record`",
            )
        return CompletionResult(
            output=response.output, usage=response.usage, stop_reason=response.stop_reason
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _payload_key(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload))).hexdigest()


def replay_backends(
    models: Mapping[str, ModelAlias],
    source: CompletionResult | Recordings,
    *,
    factories: Mapping[str, BackendFactory] = FACTORIES,
    validate: Validator | Unvalidated | None = None,
    prompts: PromptSource | None = None,
    secrets: SecretResolver = _NO_SECRETS,
) -> ModelBackends:
    """`ModelBackends` over `models` whose every provider backend is a `ReplayBackend` of
    `source`: the output goes through the same validator, intents split and redaction as a
    live one. No secret is resolved (the default resolver knows none)."""

    def wrap(factory: BackendFactory) -> BackendFactory:
        def build(name: str, spec: ModelAlias, resolver: SecretResolver) -> ProviderBackend:
            return ReplayBackend(factory(name, spec, resolver), source)

        return build

    return ModelBackends(
        models=models,
        secrets=secrets,
        factories={backend: wrap(factory) for backend, factory in factories.items()},
        prompts=prompts or RequestPrompts(),
        validate=production_validator() if validate is None else validate,
    )


__all__ = [
    "MAX_DEPTH",
    "MAX_RECORDINGS",
    "REPLAY_MISS",
    "Recordings",
    "ReplayBackend",
    "completion_digest",
    "replay_backends",
]
