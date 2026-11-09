# SPDX-License-Identifier: Apache-2.0
"""Errors of the marketplace client. Messages name the indexes by URL and say what to do."""

from collections.abc import Sequence


class MarketplaceError(Exception):
    """Base of every marketplace error."""


class FetchError(MarketplaceError):
    """An index could not be fetched or is invalid; the cache, if any, is the fallback."""


class NoCacheError(MarketplaceError):
    """An index could not be fetched (or `offline` was asked) and there is no usable cache."""


class InvalidReference(MarketplaceError):
    """`name[@version]` is malformed."""


class UnknownPlugin(MarketplaceError):
    """No searched index lists the plugin."""


class UnknownVersion(MarketplaceError):
    """The plugin has no such version (or no version that can be chosen)."""


class ConflictError(MarketplaceError):
    """Several indexes list one name under different repositories."""

    def __init__(self, name: str, listings: Sequence[tuple[str, str]]) -> None:
        self.name = name
        self.listings = tuple(listings)
        where = "; ".join(f"{index} -> {repo}" for index, repo in listings)
        first = listings[0][0]
        super().__init__(
            f"plugin {name!r} is listed by more than one marketplace with different "
            f"repositories: {where}. Choose one by adding to horae.yaml: "
            f"marketplace_pins: {{{name}: {first}}}"
        )
