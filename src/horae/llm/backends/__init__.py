# SPDX-License-Identifier: Apache-2.0
"""The built-in provider backends, by name (`models.<alias>.backend` in `horae.yaml`).

Only `anthropic` (#28) is implemented; `claude-cli` (#29), `openai-compatible` and `ollama`
(#30) add their factory to `FACTORIES` when they land. `horae doctor` reports a model alias
used by a pipeline whose backend is not in `FACTORIES`."""

from collections.abc import Mapping

from horae.contracts.instance import ModelAlias
from horae.llm.backends.anthropic import AnthropicBackend, anthropic_backend
from horae.llm.base import BackendFactory, ModelBackends, PromptSource, RequestPrompts, Validator
from horae.secrets import SecretResolver

FACTORIES: Mapping[str, BackendFactory] = {"anthropic": anthropic_backend}
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


__all__ = ["FACTORIES", "AnthropicBackend", "model_backends"]
