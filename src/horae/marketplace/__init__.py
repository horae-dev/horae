# SPDX-License-Identifier: Apache-2.0
"""The marketplace client (#43): fetch the configured indexes, cache them, resolve plugin
names. Installing what a name resolves to is #44."""

from horae.marketplace.errors import (
    ConflictError,
    FetchError,
    InvalidReference,
    MarketplaceError,
    NoCacheError,
    UnknownPlugin,
    UnknownVersion,
)
from horae.marketplace.index import Loaded, LoadedIndex, MarketplaceClient
from horae.marketplace.resolve import Found, Resolved, parse_ref, resolve, search

__all__ = [
    "ConflictError",
    "FetchError",
    "Found",
    "InvalidReference",
    "Loaded",
    "LoadedIndex",
    "MarketplaceClient",
    "MarketplaceError",
    "NoCacheError",
    "Resolved",
    "UnknownPlugin",
    "UnknownVersion",
    "parse_ref",
    "resolve",
    "search",
]
