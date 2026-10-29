# SPDX-License-Identifier: Apache-2.0
"""The built-in provider backends, by name (`models.<alias>.backend` in `horae.yaml`).

`anthropic` (#28), `openai-compatible` and `ollama` (#30) and `claude-cli` (#29) are
implemented. `horae doctor` reports a model alias used by a pipeline whose backend is not in
`FACTORIES` (and, for `claude-cli`, whether its executable is found at a supported
version)."""

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from horae.contracts.instance import ModelAlias
from horae.llm.backends._shared import (
    REFUSAL_STOP_REASONS,
    TRUNCATION_STOP_REASONS,
    UNUSABLE_STOP_REASONS,
)
from horae.llm.backends.anthropic import AnthropicBackend, anthropic_backend
from horae.llm.backends.anthropic import schema_problem as anthropic_schema_problem
from horae.llm.backends.claude_cli import ClaudeCliBackend, claude_cli_backend
from horae.llm.backends.ollama import OllamaBackend, ollama_backend
from horae.llm.backends.openai_compatible import (
    OpenAICompatibleBackend,
    openai_compatible_backend,
)
from horae.llm.base import (
    BackendFactory,
    ModelBackends,
    PromptSource,
    RequestPrompts,
    Unvalidated,
    Validator,
)
from horae.llm.validate import validate_output
from horae.secrets import SecretResolver

FACTORIES: Mapping[str, BackendFactory] = {
    "anthropic": anthropic_backend,
    "openai-compatible": openai_compatible_backend,
    "ollama": ollama_backend,
    "claude-cli": claude_cli_backend,
}
"""The available backends."""

SCHEMA_CHECKS: Mapping[str, Callable[[Mapping[str, Any]], str | None]] = {
    "anthropic": anthropic_schema_problem,
}
"""Per backend, why a skill's output schema cannot be used for its structured output (`None`:
it can); a backend without an entry takes any schema the skill loader accepts (`horae
doctor`, schemas decision 48 D4)."""


def schema_problems(schema: Mapping[str, Any]) -> dict[str, str]:
    """`{backend: why}` for each backend of `SCHEMA_CHECKS` that refuses `schema`."""
    found = {}
    for backend, check in SCHEMA_CHECKS.items():
        problem = check(schema)
        if problem is not None:
            found[backend] = problem
    return found


def model_backends(
    models: Mapping[str, ModelAlias],
    secrets: SecretResolver,
    *,
    prompts: PromptSource | None = None,
    validate: Validator | Unvalidated | None = None,
    redaction_names: tuple[str, ...] = (),
    work_dir: Path | None = None,
) -> ModelBackends:
    """A `ModelBackends` with the built-in `FACTORIES` and, unless another is given, the
    production validator `horae.llm.validate.validate_output` (decisions 38 G and 44).
    `redaction_names`: every secret the instance declares, for the T10 request check.
    `work_dir` is where `claude-cli` makes its per-call workspaces (`<state_dir>/llm-runs` in
    a run, schemas decision 42 C)."""
    factories: Mapping[str, BackendFactory] = FACTORIES
    if work_dir is not None:
        factories = {**FACTORIES, "claude-cli": partial(claude_cli_backend, work_dir=work_dir)}
    return ModelBackends(
        models=models,
        secrets=secrets,
        factories=factories,
        prompts=prompts or RequestPrompts(),
        validate=validate_output if validate is None else validate,
        redaction_names=tuple(redaction_names),
    )


__all__ = [
    "FACTORIES",
    "REFUSAL_STOP_REASONS",
    "SCHEMA_CHECKS",
    "TRUNCATION_STOP_REASONS",
    "UNUSABLE_STOP_REASONS",
    "AnthropicBackend",
    "ClaudeCliBackend",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "model_backends",
    "schema_problems",
]
