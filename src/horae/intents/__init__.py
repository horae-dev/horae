# SPDX-License-Identifier: Apache-2.0
"""Intent validation: the core's check of the writes a plugin proposes (ADR 0002).

The intent models themselves live in `horae.contracts.intents`; this package only checks a
plugin's proposals against them and against the plugin's manifest. Applying intents is the
sinks' job.
"""

from horae.intents.validate import (
    MAX_INTENTS,
    IntentRejected,
    IntentRejection,
    RejectionCode,
    safe_excerpt,
    validate_intents,
)

__all__ = [
    "MAX_INTENTS",
    "IntentRejected",
    "IntentRejection",
    "RejectionCode",
    "safe_excerpt",
    "validate_intents",
]
