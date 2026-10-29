# SPDX-License-Identifier: Apache-2.0
"""The HTTP API (#37, schemas decision 51): `/healthz` and the read-only `/runs`.

See `horae.api._app` for the routes, the redaction and the headers, and `horae.api._server`
for serving it in `horae serve`'s event loop. Imported lazily by `horae serve` only: the
other commands never load FastAPI."""

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
