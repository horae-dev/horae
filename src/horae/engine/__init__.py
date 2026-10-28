# SPDX-License-Identifier: Apache-2.0
"""The pipeline engine: run a validated `Pipeline` and produce its `RunRecord` (#17).

See `horae.engine.executor` for control flow, data between steps and the step policy,
`horae.engine.template` for parameter templating, `horae.engine.canonical` for the pipeline
digest and `horae.engine.ports` for the plugin and LLM protocols.
"""

from horae.engine.canonical import canonical_json, canonical_pipeline, pipeline_digest
from horae.engine.executor import DEFAULT_LLM_TIMEOUT_S, MAX_LLM_ATTEMPTS, execute
from horae.engine.ports import (
    EngineCallError,
    LlmBackend,
    LlmError,
    LlmRequest,
    LlmResponse,
    PluginRegistry,
    ResolvedPlugin,
    ToolCaller,
    ToolCallError,
    ToolResult,
    UnknownPlugin,
)
from horae.engine.template import TemplateError, make_context, render, render_string, variables

__all__ = [
    "DEFAULT_LLM_TIMEOUT_S",
    "MAX_LLM_ATTEMPTS",
    "EngineCallError",
    "LlmBackend",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "PluginRegistry",
    "ResolvedPlugin",
    "TemplateError",
    "ToolCallError",
    "ToolCaller",
    "ToolResult",
    "UnknownPlugin",
    "canonical_json",
    "canonical_pipeline",
    "execute",
    "make_context",
    "pipeline_digest",
    "render",
    "render_string",
    "variables",
]
