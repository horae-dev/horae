# SPDX-License-Identifier: Apache-2.0
"""The log record scrubber, moved to `horae.secrets.logs` (#53); re-exported here for the
host, which registers itself while it is open (its redactor joins the process registry's)."""

from horae.secrets.logs import install, register, scrub, unregister

__all__ = ["install", "register", "scrub", "unregister"]
