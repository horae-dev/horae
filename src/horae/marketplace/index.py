# SPDX-License-Identifier: Apache-2.0
"""Fetch marketplace indexes into a local cache, and load them offline (#43).

Layout, under a cache directory (`<state_dir>/marketplaces`), one directory per index URL
(named by the SHA-256 of the URL):

    repo.git/         the shallow bare clone of the index repository (updated on each fetch)
    marketplace.json  the last copy that passed the `MarketplaceIndex` contract
    fetched.json      {"url", "fetched_at"} of that copy

The cached copy is only ever replaced by a newer valid one, so a failed or tampered fetch
leaves the last good copy to fall back on. The copy is validated again when read.
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from horae.contracts.lockfile import GitRepoUrl
from horae.contracts.marketplace import MarketplaceIndex
from horae.marketplace.cachefs import LOCK_WAIT_S, clone_lock, make_private_dirs
from horae.marketplace.errors import FetchError, NoCacheError
from horae.marketplace.git import GIT_TIMEOUT_S, HTTPS, GitError, run_git

logger = logging.getLogger(__name__)

INDEX_FILE = "marketplace.json"
FETCHED_FILE = "fetched.json"
REPO_DIR = "repo.git"
HEAD_REF = "refs/horae/head"
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_PROBLEMS = 3

_url_adapter: TypeAdapter[str] = TypeAdapter(GitRepoUrl)


@dataclass(frozen=True)
class LoadedIndex:
    """One validated index."""

    url: str
    index: MarketplaceIndex
    fetched_at: datetime
    from_cache: bool
    """True when the index could not be refreshed and this is the cached copy."""


@dataclass(frozen=True)
class Loaded:
    """The indexes, in priority order, and the warnings raised while loading them."""

    indexes: tuple[LoadedIndex, ...]
    warnings: tuple[str, ...] = ()


def describe_problems(exc: ValidationError) -> str:
    """The first problems of an invalid index: where and what, never the offending value."""
    errors = exc.errors(include_input=False, include_url=False)
    shown = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in errors]
    more = f" (and {len(shown) - MAX_PROBLEMS} more)" if len(shown) > MAX_PROBLEMS else ""
    return "; ".join(shown[:MAX_PROBLEMS]) + more


def parse_index(text: str) -> MarketplaceIndex:
    """Validate the text of a `marketplace.json`; `ValueError` with a short reason."""
    try:
        return MarketplaceIndex.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(describe_problems(exc)) from None


class MarketplaceClient:
    """Fetches and caches the indexes of an instance.

    `allow_protocol` is the one git transport to allow. It is `https` and it is never
    configurable from `horae.yaml`: only tests pass `file`, to clone local fixture repositories.
    """

    def __init__(
        self,
        cache_dir: Path,
        urls: Sequence[str],
        *,
        allow_protocol: str = HTTPS,
        timeout_s: float = GIT_TIMEOUT_S,
        lock_wait_s: float = LOCK_WAIT_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cache_dir = cache_dir
        self.urls = tuple(urls)
        self.allow_protocol = allow_protocol
        self.timeout_s = timeout_s
        self.lock_wait_s = lock_wait_s
        self._clock = clock

    # --- loading ---------------------------------------------------------------------------

    def load(self, *, offline: bool = False) -> Loaded:
        """Every index, in the order of `urls`. Each is refreshed unless `offline`; when the
        refresh fails the cached copy is used and a warning is returned. `NoCacheError` when
        an index can be neither refreshed nor read from the cache (an index is never skipped:
        it could hide a name conflict)."""
        loaded: list[LoadedIndex] = []
        warnings: list[str] = []
        for url in self.urls:
            cached = self._read_cache(url)
            if offline:
                if cached is None:
                    raise NoCacheError(
                        f"offline, and marketplace {url} has no cached copy: "
                        "run once without --offline"
                    )
                loaded.append(cached)
                continue
            try:
                loaded.append(self.refresh(url))
            except FetchError as exc:
                if cached is None:
                    raise NoCacheError(
                        f"cannot fetch marketplace {url} ({exc}) and it has no cached copy: "
                        "check the network and retry"
                    ) from None
                message = (
                    f"cannot fetch marketplace {url} ({exc}); using the copy cached at "
                    f"{cached.fetched_at.isoformat(timespec='seconds')}"
                )
                logger.warning(message)
                warnings.append(message)
                loaded.append(cached)
        return Loaded(tuple(loaded), tuple(warnings))

    def remote(self, url: str) -> str:
        """The address git is given for `url`: `url` itself. A seam for tests, which point an
        `https://` name at a local repository (with `allow_protocol="file"`)."""
        return url

    # --- fetching --------------------------------------------------------------------------

    def refresh(self, url: str) -> LoadedIndex:
        """Clone or update `url`, validate its `marketplace.json`, and cache it."""
        if self.allow_protocol == HTTPS:
            try:
                _url_adapter.validate_python(url)
            except ValidationError:
                raise FetchError("not an https git URL") from None
        folder = self._folder(url)
        repo = folder / REPO_DIR
        try:
            make_private_dirs(folder)
        except OSError as exc:
            raise FetchError(f"cannot write {folder}: {exc.strerror}") from None
        # One writer per clone: a second `horae plugin` waits, then fails with `LockBusy`.
        with clone_lock(self.cache_dir, folder.name, f"marketplace {url}", wait_s=self.lock_wait_s):
            text = self._fetch_text(url, folder, repo)
        try:
            index = parse_index(text)
        except ValueError as exc:
            raise FetchError(f"{INDEX_FILE} is invalid: {exc}") from None
        fetched_at = self._clock()
        self._write_cache(folder, url, text, fetched_at)
        return LoadedIndex(url, index, fetched_at, from_cache=False)

    def _fetch_text(self, url: str, folder: Path, repo: Path) -> str:
        """Clone or update the repository and return its `marketplace.json`."""
        try:
            if not repo.is_dir():
                self._clone(url, folder, repo)
            run_git(
                repo,
                "fetch",
                "--quiet",
                "--force",
                "--depth=1",
                "--no-tags",
                "--",
                self.remote(url),
                f"+HEAD:{HEAD_REF}",
                allow=self.allow_protocol,
                timeout_s=self.timeout_s,
                network=True,
            )
            size = run_git(
                repo, "cat-file", "-s", f"{HEAD_REF}:{INDEX_FILE}", allow=self.allow_protocol
            )
            if not size.isdigit() or int(size) > MAX_INDEX_BYTES:
                raise FetchError(f"{INDEX_FILE} is larger than {MAX_INDEX_BYTES} bytes")
            text = run_git(repo, "show", f"{HEAD_REF}:{INDEX_FILE}", allow=self.allow_protocol)
        except GitError as exc:
            raise FetchError(str(exc)) from None
        return text

    def _clone(self, url: str, folder: Path, repo: Path) -> None:
        """Clone into a scratch directory and move it into place only when complete, so a
        clone killed by the timeout never leaves a broken `repo.git` that every later fetch
        would trip over. Shallow (the tip only): a hostile index cannot make us download its
        whole history."""
        scratch = Path(tempfile.mkdtemp(dir=folder, prefix=".clone-"))
        try:
            run_git(
                folder,
                "clone",
                "--bare",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "--single-branch",
                "--",
                self.remote(url),
                str(scratch),
                allow=self.allow_protocol,
                timeout_s=self.timeout_s,
                network=True,
            )
            scratch.replace(repo)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # --- cache -----------------------------------------------------------------------------

    def _folder(self, url: str) -> Path:
        return self.cache_dir / hashlib.sha256(url.encode()).hexdigest()[:24]

    def _write_cache(self, folder: Path, url: str, text: str, fetched_at: datetime) -> None:
        meta = json.dumps({"url": url, "fetched_at": fetched_at.isoformat()})
        for name, content in ((INDEX_FILE, text), (FETCHED_FILE, meta)):
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content + "\n")
                Path(tmp).replace(folder / name)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def _read_cache(self, url: str) -> LoadedIndex | None:
        """The cached copy of `url`, or `None` when absent or not valid."""
        folder = self._folder(url)
        try:
            meta = json.loads((folder / FETCHED_FILE).read_text(encoding="utf-8"))
            if meta["url"] != url:
                return None
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
            size = (folder / INDEX_FILE).stat().st_size
            if size > MAX_INDEX_BYTES:
                return None
            index = parse_index((folder / INDEX_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if fetched_at.tzinfo is None:
            return None
        return LoadedIndex(url, index, fetched_at, from_cache=True)
