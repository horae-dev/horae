# SPDX-License-Identifier: Apache-2.0
"""Redaction of log records (#24, generalised by #53, schemas decision 43 B).

Horae's own loggers, the MCP SDK's, the HTTP clients' and the Anthropic SDK's can all carry a
secret: the SDK logs "Failed to validate notification" with the pydantic error as `exc_info`
(whose text holds the offending input values), an HTTP client at `DEBUG` logs headers, and a
core message may format a value it was handed.

`install()` (idempotent; called by the CLI entry point, by `Runner.run` and when a
`PluginHost` opens) wraps the logging **record factory**, so every record of the `LOGGERS`
(and of their children) is scrubbed when it is created, before any filter or handler sees
it, wherever that handler is attached (a handler on a logger itself, as pytest adds to
non-propagating loggers, would bypass a handler- or propagation-based scheme):

- its message is formatted and redacted with the process registry's redactor
  (`horae.secrets.redact.current()`: every secret this process resolved) and the redactor
  of every registered source (an open plugin host), and its `args` are dropped;
- its `exc_info` and `stack_info` are dropped (a traceback may hold data); only the
  exception's type name is appended to the message.

The record keeps its logger name, level and location. Records of other loggers are untouched.
`install()` also pins the `HTTP_LOGGERS` (and their children already created) to at least
`WARNING`, so request headers are never even formatted.
"""

import logging
import threading
import weakref
from typing import Any

from horae.secrets.redact import Redactor, current

LOGGERS = (
    "horae",
    "mcp",
    "client",
    "server",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "anthropic",
)
"""The loggers scrubbed (and their children): Horae's, the MCP SDK's (`mcp`, and the bare
`client` and `server` loggers it also uses), the HTTP clients' and the Anthropic SDK's."""
HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")
"""Pinned to `WARNING` by `install()`: at `DEBUG` they log request headers."""

_PREFIXES = tuple(f"{name}." for name in LOGGERS)
_sources: "weakref.WeakSet[Any]" = weakref.WeakSet()
"""Open plugin hosts (each has a `redactor()` method returning a `Redactor`)."""
_lock = threading.Lock()
_installed = False


def covered(name: str) -> bool:
    """Whether records of logger `name` are scrubbed."""
    return name in LOGGERS or name.startswith(_PREFIXES)


def scrub(record: logging.LogRecord) -> None:
    """Redact `record` in place (see the module docstring)."""
    try:
        message = record.getMessage()
    except Exception:  # a malformed format string: show nothing of the arguments
        message = str(record.msg)
    if record.exc_info and record.exc_info[0] is not None:
        message = f"{message} ({record.exc_info[0].__name__}, details withheld)"
    message, _ = current().redact_text(message)
    for source in list(_sources):
        redactor: Redactor = source.redactor()
        message, _ = redactor.redact_text(message)
    record.msg, record.args = message, None
    record.exc_info, record.exc_text, record.stack_info = None, None, None


def pin_http_loggers() -> None:
    """Pin `HTTP_LOGGERS` and their children already created to at least `WARNING`."""
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:  # NOTSET (0) included
            logger.setLevel(logging.WARNING)
    prefixes = tuple(f"{name}." for name in HTTP_LOGGERS)
    for name, child in list(logging.root.manager.loggerDict.items()):
        if (
            isinstance(child, logging.Logger)
            and name.startswith(prefixes)
            and child.level != logging.NOTSET
            and child.level < logging.WARNING
        ):
            child.setLevel(logging.WARNING)


def install() -> None:
    """Wrap the record factory once and pin the HTTP loggers (see the module docstring)."""
    global _installed
    pin_http_loggers()
    with _lock:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            if covered(record.name):
                scrub(record)
            return record

        logging.setLogRecordFactory(factory)
        _installed = True


def register(source: Any) -> None:
    """Also redact with `source.redactor()` while `source` is open (the plugin host)."""
    install()
    _sources.add(source)


def unregister(source: Any) -> None:
    _sources.discard(source)


__all__ = [
    "HTTP_LOGGERS",
    "LOGGERS",
    "covered",
    "install",
    "pin_http_loggers",
    "register",
    "scrub",
    "unregister",
]
