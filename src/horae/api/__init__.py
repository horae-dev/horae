# SPDX-License-Identifier: Apache-2.0
"""The HTTP API (#37, schemas decision 51): `/healthz`, the read-only `/runs`, and the signed
single-use action links `/act/<token>` (#38, schemas decision 54).

See `horae.api._app` for the routes, the redaction and the headers, `horae.api._server` for
serving it in `horae serve`'s event loop, and `horae.api.act` for the action links. Imported
lazily by `horae serve` only: the other commands never load FastAPI. The names below are
loaded on first use (PEP 562), so `horae.api.act`'s token and minting code, which the runner
uses when it sends a notification, imports no web framework.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from horae.api._app import (
        API_SCHEMA_VERSION,
        DEFAULT_ALLOWED_HOSTS,
        DEFAULT_PAGE,
        INVALID_HOST_MESSAGE,
        MAX_CURSOR,
        MAX_LOGGED_HOSTS,
        MAX_PAGE,
        SECURITY_HEADERS,
        ApiContext,
        HostCheck,
        RedactedJSON,
        allowed_host,
        create_app,
        decode_cursor,
        encode_cursor,
        host_of,
    )
    from horae.api._server import GRACEFUL_SHUTDOWN_S, ApiServer, BindError, bind, build_server

_APP = (
    "API_SCHEMA_VERSION",
    "DEFAULT_ALLOWED_HOSTS",
    "DEFAULT_PAGE",
    "INVALID_HOST_MESSAGE",
    "MAX_CURSOR",
    "MAX_LOGGED_HOSTS",
    "MAX_PAGE",
    "SECURITY_HEADERS",
    "ApiContext",
    "HostCheck",
    "RedactedJSON",
    "allowed_host",
    "create_app",
    "decode_cursor",
    "encode_cursor",
    "host_of",
)
_SERVER = ("GRACEFUL_SHUTDOWN_S", "ApiServer", "BindError", "bind", "build_server")


def __getattr__(name: str) -> Any:
    if name in _APP:
        return getattr(importlib.import_module("horae.api._app"), name)
    if name in _SERVER:
        return getattr(importlib.import_module("horae.api._server"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "API_SCHEMA_VERSION",
    "DEFAULT_ALLOWED_HOSTS",
    "DEFAULT_PAGE",
    "GRACEFUL_SHUTDOWN_S",
    "INVALID_HOST_MESSAGE",
    "MAX_CURSOR",
    "MAX_LOGGED_HOSTS",
    "MAX_PAGE",
    "SECURITY_HEADERS",
    "ApiContext",
    "ApiServer",
    "BindError",
    "HostCheck",
    "RedactedJSON",
    "allowed_host",
    "bind",
    "build_server",
    "create_app",
    "decode_cursor",
    "encode_cursor",
    "host_of",
]
