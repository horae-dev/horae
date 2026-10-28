# SPDX-License-Identifier: Apache-2.0
"""The built-in provider backends, by name (`models.<alias>.backend` in `horae.yaml`).

`anthropic` (#28), `openai-compatible` and `ollama` (#30) and `claude-cli` (#29) are
implemented. `horae doctor` reports a model alias used by a pipeline whose backend is not in
`FACTORIES` (and, for `claude-cli`, whether its executable is found at a supported
version)."""

from collections.abc import Mapping

from horae.contracts.instance import ModelAlias
from horae.llm.backends.anthropic import AnthropicBackend, anthropic_backend
from horae.llm.backends.claude_cli import ClaudeCliBackend, claude_cli_backend
from horae.llm.backends.ollama import OllamaBackend, ollama_backend
from horae.llm.backends.openai_compatible import (
    OpenAICompatibleBackend,
    openai_compatible_backend,
)
from horae.llm.base import BackendFactory, ModelBackends, PromptSource, RequestPrompts, Validator
from horae.secrets import SecretResolver

FACTORIES: Mapping[str, BackendFactory] = {
    "anthropic": anthropic_backend,
    "openai-compatible": openai_compatible_backend,
    "ollama": ollama_backend,
    "claude-cli": claude_cli_backend,
}
"""The available backends."""


def model_backends(
    models: Mapping[str, ModelAlias],
    secrets: SecretResolver,
    *,
    prompts: PromptSource | None = None,
    validate: Validator | None = None,
) -> ModelBackends:
    """A `ModelBackends` with the built-in `FACTORIES`."""
    return ModelBackends(
        models=models,
        secrets=secrets,
        factories=FACTORIES,
        prompts=prompts or RequestPrompts(),
        validate=validate,
    )


__all__ = [
    "FACTORIES",
    "AnthropicBackend",
    "ClaudeCliBackend",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "model_backends",
]
