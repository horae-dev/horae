# SPDX-License-Identifier: Apache-2.0
"""Redaction of the MCP SDK's own log records (#24).

The SDK logs about what a plugin sent: "Failed to validate notification" with the pydantic
error as `exc_info`, whose text holds the offending **input values**, so a malformed
notification carrying a secret would put it in Horae's logs, outside every redacted channel.

`install()` (called when a `PluginHost` opens; idempotent) wraps the logging **record
factory**, so every record of the SDK's loggers (`mcp` and its children, and the bare
`client` and `server` loggers the SDK also uses) is scrubbed when it is created, before any
filter or handler sees it, wherever that handler is attached (a handler on a logger itself, as
pytest adds to non-propagating loggers, would bypass a handler- or propagation-based scheme):

- its message is formatted and redacted with the redactor of every open host, and its `args`
  are dropped;
- its `exc_info` and `stack_info` are dropped (a traceback of plugin data is never logged);
  only the exception's type name is appended to the message.

The record keeps its logger name, level and location. Records of other loggers are untouched.
"""

import logging
import weakref
from typing import Any

from horae.plugins.host.redact import Redactor

LOGGERS = ("mcp", "client", "server")
"""The loggers the SDK writes to (and, for `mcp`, its children)."""

_sources: "weakref.WeakSet[Any]" = weakref.WeakSet()
"""Every open host (each has a `redactor()` method returning a `Redactor`)."""
_installed = False


def _from_sdk(name: str) -> bool:
    return name in LOGGERS or name.startswith("mcp.")


def scrub(record: logging.LogRecord) -> None:
    """Redact `record` in place (see the module docstring)."""
    try:
        message = record.getMessage()
    except Exception:  # a malformed format string: show nothing of the arguments
        message = str(record.msg)
    if record.exc_info and record.exc_info[0] is not None:
        message = f"{message} ({record.exc_info[0].__name__}, details withheld)"
    for source in list(_sources):
        redactor: Redactor = source.redactor()
        message, _ = redactor.redact_text(message)
    record.msg, record.args = message, None
    record.exc_info, record.exc_text, record.stack_info = None, None, None


def install() -> None:
    """Wrap the record factory once (see the module docstring)."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if _from_sdk(record.name):
            scrub(record)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def register(source: Any) -> None:
    """Redact the SDK's records with `source.redactor()` while `source` is open."""
    install()
    _sources.add(source)


def unregister(source: Any) -> None:
    _sources.discard(source)


__all__ = ["LOGGERS", "install", "register", "scrub", "unregister"]
