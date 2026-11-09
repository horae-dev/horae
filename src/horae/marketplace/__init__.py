# SPDX-License-Identifier: Apache-2.0
"""The marketplace client (#43): fetch the configured indexes, cache them, resolve plugin
names, and install what a name resolves to (#44)."""

from horae.marketplace.errors import (
    AlreadyInstalled,
    ApprovalRefused,
    ConflictError,
    FetchError,
    InstallError,
    InstallRefused,
    InvalidReference,
    LockBusy,
    LockInvalid,
    MarketplaceError,
    NoCacheError,
    TierRefused,
    UnknownPlugin,
    UnknownVersion,
)
from horae.marketplace.index import Loaded, LoadedIndex, MarketplaceClient
from horae.marketplace.install import Installed, PluginInstaller
from horae.marketplace.resolve import Found, Resolved, parse_ref, resolve, search

__all__ = [
    "AlreadyInstalled",
    "ApprovalRefused",
    "ConflictError",
    "FetchError",
    "Found",
    "InstallError",
    "InstallRefused",
    "Installed",
    "InvalidReference",
    "Loaded",
    "LoadedIndex",
    "LockBusy",
    "LockInvalid",
    "MarketplaceClient",
    "MarketplaceError",
    "NoCacheError",
    "PluginInstaller",
    "Resolved",
    "TierRefused",
    "UnknownPlugin",
    "UnknownVersion",
    "parse_ref",
    "resolve",
    "search",
]
