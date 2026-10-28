# SPDX-License-Identifier: Apache-2.0
"""Redaction of log records, warnings and uncaught exceptions (#24, generalised by #53,
schemas decision 43 E).

Any logger can carry a secret: the MCP SDK logs "Failed to validate notification" with the
pydantic error as `exc_info` (whose text holds the offending input values), an HTTP client at
`DEBUG` logs headers, `asyncio` logs a task's exception, `uvicorn` a request, `urllib3` a URL,
and a core message may format a value it was handed. So `install()` (idempotent; called by the
CLI entry point, by `Runner.run` and when a `PluginHost` opens) scrubs **every** record, of
every logger, the root included:

- it wraps the logging **record factory**, so a record is scrubbed when it is created, before
  any filter or handler sees it, wherever that handler is attached (a handler on a logger
  itself, as pytest adds to non-propagating loggers, would bypass a handler- or
  propagation-based scheme). Its message is formatted and redacted with the process
  registry's redactor (`horae.secrets.redact.current()`: every secret this process resolved)
  and the redactor of every registered source (an open plugin host), and its `args` are
  dropped; its `exc_info` and `stack_info` are dropped (a traceback may hold data) and only the
  exception's type name is appended to the message. The record keeps its logger name, level
  and location;
- it calls `logging.captureWarnings(True)`, so a `warnings.warn` goes to the `py.warnings`
  logger (hence through the factory) instead of being written to `stderr` as it is;
- it sets `sys.excepthook` and `threading.excepthook` to redacting hooks (`excepthook`,
  `thread_excepthook`): an uncaught exception's report (type, message and traceback, as the
  default hooks format it) is redacted before it is written to `stderr` (only the text; the
  exception object itself is not changed);
- it pins the `HTTP_LOGGERS` (and their children already created) to at least `WARNING`, so
  request headers are never even formatted.
"""

import logging
import sys
import threading
import traceback
import warnings
import weakref
from types import TracebackType
from typing import Any

from horae.secrets.redact import Redactor, current

HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")
"""Pinned to `WARNING` by `install()`: at `DEBUG` they log request headers."""

_sources: "weakref.WeakSet[Any]" = weakref.WeakSet()
"""Open plugin hosts (each has a `redactor()` method returning a `Redactor`)."""
_lock = threading.Lock()
_installed = False


def covered(name: str) -> bool:
    """Whether records of logger `name` are scrubbed: every logger is (decision 43 E)."""
    return True


def redact_all(text: str) -> str:
    """`text` redacted with the process registry and every registered source."""
    text, _ = current().redact_text(text)
    for source in list(_sources):
        redactor: Redactor = source.redactor()
        text, _ = redactor.redact_text(text)
    return text


def scrub(record: logging.LogRecord) -> None:
    """Redact `record` in place (see the module docstring)."""
    try:
        message = record.getMessage()
    except Exception:  # a malformed format string: show nothing of the arguments
        message = str(record.msg)
    if record.exc_info and record.exc_info[0] is not None:
        message = f"{message} ({record.exc_info[0].__name__}, details withheld)"
    record.msg, record.args = redact_all(message), None
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


def _report(
    exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType | None
) -> str:
    return redact_all("".join(traceback.format_exception(exc_type, exc, tb)))


def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    """`sys.excepthook`, redacted."""
    sys.stderr.write(_report(exc_type, exc, tb))


def thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """`threading.excepthook`, redacted (a `SystemExit` is ignored, as by the default)."""
    if args.exc_type is SystemExit:
        return
    name = "?" if args.thread is None else args.thread.name
    head = redact_all(f"Exception in thread {name}:\n")
    sys.stderr.write(head + _report(args.exc_type, args.exc_value, args.exc_traceback))


def _captured_showwarning() -> Any:
    """The `showwarning` that `logging.captureWarnings(True)` installs."""
    return getattr(logging, "_showwarning", None)


def install() -> None:
    """Wrap the record factory and the exception hooks once, capture warnings, and pin the
    HTTP loggers (see the module docstring)."""
    global _installed
    pin_http_loggers()
    with _lock:
        # Applied again on every call: a `warnings.catch_warnings()` block, or a test
        # harness, may have restored the previous `showwarning` or hooks since.
        if warnings.showwarning is not _captured_showwarning():
            logging.captureWarnings(False)  # forget a stale saved `showwarning`, then
            logging.captureWarnings(True)  # capture again (it does nothing when "on")
        sys.excepthook = excepthook
        threading.excepthook = thread_excepthook
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
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
    "covered",
    "excepthook",
    "install",
    "pin_http_loggers",
    "redact_all",
    "register",
    "scrub",
    "thread_excepthook",
    "unregister",
]
