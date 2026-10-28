# SPDX-License-Identifier: Apache-2.0
"""Intent validation: the core's check of the writes a step proposes (ADR 0002).

The intent models themselves live in `horae.contracts.intents`; this package only checks a
step's proposals against them and against the proposer's permissions. Applying intents is
the sinks' job.
"""

from horae.intents.validate import (
    MAX_BODY_DEPTH,
    MAX_INTENTS,
    STEP_ERROR_KIND,
    IntentRejected,
    IntentRejection,
    RejectionCode,
    safe_excerpt,
    validate_intents,
    validate_plugin_intents,
)

__all__ = [
    "MAX_BODY_DEPTH",
    "MAX_INTENTS",
    "STEP_ERROR_KIND",
    "IntentRejected",
    "IntentRejection",
    "RejectionCode",
    "safe_excerpt",
    "validate_intents",
    "validate_plugin_intents",
]
