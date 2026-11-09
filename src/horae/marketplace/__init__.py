# SPDX-License-Identifier: Apache-2.0
"""The marketplace client (#43): fetch the configured indexes, cache them, resolve plugin
names, and install what a name resolves to (#44) with its own venv (#45)."""

from horae.marketplace.environment import PluginEnvironment
from horae.marketplace.errors import (
    AlreadyInstalled,
    ConflictError,
    FetchError,
    InstallError,
    InstallRefused,
    InvalidReference,
    MarketplaceError,
    NoCacheError,
    UnknownPlugin,
    UnknownVersion,
)
from horae.marketplace.index import Loaded, LoadedIndex, MarketplaceClient
from horae.marketplace.install import Installed, PluginInstaller, Prepare
from horae.marketplace.resolve import Found, Resolved, parse_ref, resolve, search

__all__ = [
    "AlreadyInstalled",
    "ConflictError",
    "FetchError",
    "Found",
    "InstallError",
    "InstallRefused",
    "Installed",
    "InvalidReference",
    "Loaded",
    "LoadedIndex",
    "MarketplaceClient",
    "MarketplaceError",
    "NoCacheError",
    "PluginEnvironment",
    "PluginInstaller",
    "Prepare",
    "Resolved",
    "UnknownPlugin",
    "UnknownVersion",
    "parse_ref",
    "resolve",
    "search",
]
