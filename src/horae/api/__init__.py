# SPDX-License-Identifier: Apache-2.0
"""The HTTP API (#37, schemas decision 51): `/healthz` and the read-only `/runs`.

See `horae.api._app` for the routes, the redaction and the headers, and `horae.api._server`
for serving it in `horae serve`'s event loop. Imported lazily by `horae serve` only: the
other commands never load FastAPI."""

from horae.api._app import (
    API_SCHEMA_VERSION,
    DEFAULT_PAGE,
    MAX_CURSOR,
    MAX_PAGE,
    SECURITY_HEADERS,
    ApiContext,
    RedactedJSON,
    create_app,
    decode_cursor,
    encode_cursor,
)
from horae.api._server import GRACEFUL_SHUTDOWN_S, ApiServer, BindError, bind, build_server

__all__ = [
    "API_SCHEMA_VERSION",
    "DEFAULT_PAGE",
    "GRACEFUL_SHUTDOWN_S",
    "MAX_CURSOR",
    "MAX_PAGE",
    "SECURITY_HEADERS",
    "ApiContext",
    "ApiServer",
    "BindError",
    "RedactedJSON",
    "bind",
    "build_server",
    "create_app",
    "decode_cursor",
    "encode_cursor",
]
