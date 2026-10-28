# SPDX-License-Identifier: Apache-2.0
"""The built-in provider backends, by name (`models.<alias>.backend` in `horae.yaml`).

`anthropic` (#28), `openai-compatible` and `ollama` (#30) are implemented; `claude-cli` (#29)
adds its factory to `FACTORIES` when it lands. `horae doctor` reports a model alias
used by a pipeline whose backend is not in `FACTORIES`."""

from collections.abc import Mapping

from horae.contracts.instance import ModelAlias
from horae.llm.backends.anthropic import AnthropicBackend, anthropic_backend
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
}
"""The available backends."""


def model_backends(
    models: Mapping[str, ModelAlias],
    secrets: SecretResolver,
    *,
    prompts: PromptSource | None = None,
    validate: Validator | Unvalidated | None = None,
) -> ModelBackends:
    """A `ModelBackends` with the built-in `FACTORIES` and, unless another is given, the
    production validator `horae.llm.validate.validate_output` (decisions 38 G and 44)."""
    return ModelBackends(
        models=models,
        secrets=secrets,
        factories=FACTORIES,
        prompts=prompts or RequestPrompts(),
        validate=validate_output if validate is None else validate,
    )


__all__ = [
    "FACTORIES",
    "AnthropicBackend",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "model_backends",
]
