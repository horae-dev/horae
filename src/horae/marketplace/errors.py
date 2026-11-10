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


class InstallError(MarketplaceError):
    """A plugin could not be fetched or installed (network, git, disk). Nothing is left."""


class InstallRefused(InstallError):
    """A plugin was fetched but refused: the tag and the pinned SHA disagree, the tree breaks
    a limit or escapes, or `plugin.yaml` is invalid or names another plugin or version."""


class AlreadyInstalled(InstallRefused):
    """The instance already has a plugin of that name (upgrading is `horae plugin upgrade`)."""


class TierRefused(InstallRefused):
    """The plugin's trust tier may not be installed yet: before M5 only `first-party`
    marketplace plugins install (docs/security.md T04, decision 70 E)."""


class ApprovalRefused(InstallRefused):
    """The permissions were not approved (declined, no terminal, or `--yes` without a
    matching `horae.lock` entry). Nothing is installed."""


class NotInstalled(MarketplaceError):
    """`upgrade` or `remove` names a plugin that is not installed by `horae plugin add` (no
    `horae.lock` entry and no tree, or a tree without an install record)."""


class DowngradeRefused(InstallRefused):
    """`upgrade` would install a lower version than the installed one (`--allow-downgrade`)."""


class PluginEnabled(MarketplaceError):
    """`remove` names a plugin still listed under `plugins:` in `horae.yaml`."""


class LockInvalid(MarketplaceError):
    """`horae.lock` exists but cannot be read or does not validate: it is never rewritten."""


class LockBusy(MarketplaceError):
    """Another command holds the instance's `horae.lock` writer lock."""
