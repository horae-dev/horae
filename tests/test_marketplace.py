# SPDX-License-Identifier: Apache-2.0
"""Marketplace resolution (#43): contract, git hardening, cache, offline, priority, conflicts."""

import itertools
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from horae.contracts import InstanceConfig, MarketplaceIndex
from horae.contracts.instance import DEFAULT_MARKETPLACE
from horae.marketplace import (
    ConflictError,
    FetchError,
    InvalidReference,
    MarketplaceClient,
    NoCacheError,
    UnknownPlugin,
    UnknownVersion,
    parse_ref,
    resolve,
    search,
)
from horae.marketplace import (
    index as index_module,
)
from horae.marketplace.git import GitError, git_command, git_env, run_git
from horae.marketplace.semver import compare, highest, sorted_desc
from marketplace_fixtures import A, B, LocalClient, entry, publish, sha

# --- the contract ----------------------------------------------------------------------------


def _index(**changes: Any) -> dict[str, Any]:
    plugin = entry("github", {"0.1.0": sha(1)}, tier="first-party")
    plugin.update(changes)
    return {"plugins": [plugin]}


def test_a_valid_index_is_accepted() -> None:
    parsed = MarketplaceIndex.model_validate(_index())
    assert parsed.plugins[0].versions == {"0.1.0": sha(1)}


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": 1},
        {"versions": {"0.1.0": "3f2c9e1"}},  # short sha
        {"versions": {"0.1.0": "A" * 40}},  # uppercase
        {"versions": {"0.1.0": "a" * 64}},  # the marketplace pins SHA-1 only
        {"versions": {"v0.1.0": sha(1)}},
        {"versions": {}},
        {"repo": "http://github.com/horae-dev/plugin-github"},
        {"repo": "ext::sh -c id"},
        {"repo": "file:///tmp/plugin"},
        {"repo": "git@github.com:horae-dev/plugin-github.git"},
        {"repo": "https://user@github.com/horae-dev/plugin-github"},
        {"repo": "https://github.com/horae-dev/../x"},
        {"repo": "https://localhost/x"},
        {"tier": "verified"},
        {"name": "Bad Name"},
    ],
)
def test_a_malformed_or_tampered_index_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MarketplaceIndex.model_validate(_index(**changes))


def test_extra_top_level_keys_and_duplicate_names_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MarketplaceIndex.model_validate({**_index(), "version": 1})
    plugin = _index()["plugins"][0]
    with pytest.raises(ValidationError, match="duplicate"):
        MarketplaceIndex.model_validate({"plugins": [plugin, plugin]})


# --- the instance config -----------------------------------------------------------------------


def test_marketplaces_default_to_the_horae_marketplace() -> None:
    config = InstanceConfig()
    assert config.marketplaces == [DEFAULT_MARKETPLACE]
    assert DEFAULT_MARKETPLACE == "https://github.com/horae-dev/marketplace"


def test_marketplaces_are_an_ordered_https_list() -> None:
    assert InstanceConfig(marketplaces=[B, A]).marketplaces == [B, A]
    for bad in ("http://example.org/a/b", "file:///tmp/x", "ext::sh -c id", "git@example.org:a/b"):
        with pytest.raises(ValidationError):
            InstanceConfig(marketplaces=[bad])
    with pytest.raises(ValidationError):
        InstanceConfig(marketplaces=[])
    with pytest.raises(ValidationError, match="twice"):
        InstanceConfig(marketplaces=[A, A])


def test_a_pin_must_name_a_configured_marketplace() -> None:
    assert InstanceConfig(marketplaces=[A, B], marketplace_pins={"github": B}).marketplace_pins
    with pytest.raises(ValidationError, match="not one of"):
        InstanceConfig(marketplaces=[A], marketplace_pins={"github": B})


# --- semver --------------------------------------------------------------------------------------


def test_semver_precedence_follows_the_spec() -> None:
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    for low, high in itertools.pairwise(ordered):
        assert compare(low, high) == -1
        assert compare(high, low) == 1
    assert sorted_desc(ordered) == ordered[::-1]
    assert compare("1.0.0+a", "1.0.0+b") == 0


def test_the_highest_version_is_numeric_not_textual() -> None:
    assert highest(["0.9.0", "0.10.0", "0.2.0"]) == "0.10.0"
    assert highest(["1.0.0+a", "1.0.0+b"]) == "1.0.0+b"  # a tie is broken deterministically


def test_prereleases_are_skipped_unless_allowed() -> None:
    versions = ["1.0.0", "2.0.0-rc.1"]
    assert highest(versions) == "1.0.0"
    assert highest(versions, allow_prerelease=True) == "2.0.0-rc.1"
    assert highest(["2.0.0-rc.1"]) is None
    assert highest(["2.0.0-rc.1", "2.0.0"], allow_prerelease=True) == "2.0.0"


# --- git hardening -----------------------------------------------------------------------------


def test_git_allows_one_protocol_and_ignores_config() -> None:
    cmd = git_command("clone", "--", "x")
    assert cmd[:5] == ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    env = git_env()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "LC_ALL",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_TERMINAL_PROMPT",
    }


def test_real_git_refuses_file_and_ext_by_default(tmp_path: Path) -> None:
    repo = publish(tmp_path / "idx", [entry("one", ["1.0.0"])])
    with pytest.raises(GitError):
        run_git(tmp_path, "clone", "--bare", "--", repo.as_uri(), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()
    marker = tmp_path / "pwned"
    with pytest.raises(GitError):
        run_git(tmp_path, "clone", "--", f"ext::sh -c 'touch {marker}'", str(tmp_path / "out2"))
    assert not marker.exists()


def test_git_times_out(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="timed out"):
        run_git(tmp_path, "version", timeout_s=0.0)


def test_the_default_client_only_ever_uses_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake(cwd: Path, *args: str, allow: str = "", timeout_s: float = 0) -> str:
        calls.append(allow)
        raise GitError("offline")

    monkeypatch.setattr(index_module, "run_git", fake)
    client = MarketplaceClient(tmp_path / "cache", [A])
    assert client.allow_protocol == "https"
    with pytest.raises(NoCacheError):
        client.load()
    assert calls == ["https"]
    for url in (publish(tmp_path / "idx", []).as_uri(), "ext::sh -c id", "http://example.org/a/b"):
        with pytest.raises(FetchError, match="https"):
            client.refresh(url)
    assert calls == ["https"]  # a non-https URL never reached git


# --- fetching and caching ----------------------------------------------------------------------


def _client(tmp_path: Path, **indexes: Path) -> LocalClient:
    urls = {"a": A, "b": B}
    names = list(indexes)
    return LocalClient(
        tmp_path / "cache", [urls[n] for n in names], {urls[n]: p for n, p in indexes.items()}
    )


def test_fetch_validates_caches_and_records_when(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    loaded = client.load()
    (item,) = loaded.indexes
    assert item.url == A
    assert not item.from_cache
    assert item.fetched_at.tzinfo is not None
    assert [p.name for p in item.index.plugins] == ["one"]
    assert loaded.warnings == ()
    folder = client._folder(A)
    assert json.loads((folder / "fetched.json").read_text())["url"] == A
    assert (folder / "repo.git").is_dir()


def test_an_update_is_picked_up(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()
    publish(repo, [entry("one", ["1.0.0", "1.1.0"])])
    (item,) = client.load().indexes
    assert set(item.index.plugins[0].versions) == {"1.0.0", "1.1.0"}


def test_an_index_with_a_different_default_branch_still_updates(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()
    from marketplace_fixtures import git

    git(repo, "checkout", "-q", "-b", "trunk")
    git(repo, "branch", "-q", "-D", "main")
    publish(repo, [entry("two", ["1.0.0"])])
    (item,) = client.load().indexes
    assert [p.name for p in item.index.plugins] == ["two"]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"plugins": [], "extra": 1}',
        json.dumps({"plugins": [entry("x", {"1.0.0": "abc"})]}),
        json.dumps({"plugins": [entry("x", ["1.0.0"], repo="http://example.org/a/b")]}),
        json.dumps({"plugins": [entry("x", ["1.0.0"], repo="ext::sh -c id")]}),
        json.dumps({"plugins": [entry("x", ["1.0.0"], repo="file:///etc")]}),
    ],
)
def test_a_tampered_index_is_rejected_and_the_cache_survives(tmp_path: Path, text: str) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()
    publish(repo, text)
    with pytest.raises(FetchError, match="invalid"):
        client.refresh(A)
    loaded = client.load()
    assert loaded.indexes[0].from_cache
    assert loaded.indexes[0].index.plugins[0].name == "one"
    assert "invalid" in loaded.warnings[0]


def test_an_index_without_the_file_is_an_error(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [])
    from marketplace_fixtures import git

    git(repo, "rm", "-q", "marketplace.json")
    git(repo, "commit", "-q", "-m", "rm")
    with pytest.raises(NoCacheError, match="no cached copy"):
        _client(tmp_path, a=repo).load()


# --- offline -----------------------------------------------------------------------------------


def test_a_failed_fetch_falls_back_to_the_cache_and_warns(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    fresh = client.load().indexes[0]
    shutil.rmtree(repo)  # the network is gone
    loaded = client.load()
    (item,) = loaded.indexes
    assert item.from_cache
    assert item.fetched_at == fresh.fetched_at
    assert item.index == fresh.index
    (warning,) = loaded.warnings
    assert A in warning
    assert "cannot fetch" in warning
    assert "using the copy cached at" in warning
    assert resolve("one", loaded.indexes).version == "1.0.0"  # and it still resolves


def test_offline_uses_the_cache_without_touching_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()

    def boom(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("git was called")

    monkeypatch.setattr(index_module, "run_git", boom)
    loaded = client.load(offline=True)
    assert loaded.indexes[0].from_cache
    assert loaded.warnings == ()


def test_no_cache_and_no_network_is_a_clear_error(tmp_path: Path) -> None:
    client = _client(tmp_path, a=tmp_path / "missing")
    with pytest.raises(NoCacheError, match=r"cannot fetch marketplace .*no cached copy"):
        client.load()
    with pytest.raises(NoCacheError, match=r"offline.*no cached copy.*without --offline"):
        client.load(offline=True)


def test_one_unreachable_index_without_cache_is_never_skipped(tmp_path: Path) -> None:
    good = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=good, b=tmp_path / "missing")
    with pytest.raises(NoCacheError, match=r"example\.org/b"):
        client.load()


def test_a_corrupt_cache_counts_as_no_cache(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()
    (client._folder(A) / "marketplace.json").write_text('{"plugins": [], "x": 1}')
    assert client._read_cache(A) is None
    (client._folder(A) / "marketplace.json").unlink()
    with pytest.raises(NoCacheError):
        client.load(offline=True)


# --- resolution --------------------------------------------------------------------------------


def _loaded(tmp_path: Path, a: list[dict[str, Any]], b: list[dict[str, Any]] | None = None) -> Any:
    repos = {"a": publish(tmp_path / "a", a)}
    if b is not None:
        repos["b"] = publish(tmp_path / "b", b)
    return _client(tmp_path, **repos).load().indexes


def test_the_highest_version_wins_and_everything_is_returned(tmp_path: Path) -> None:
    plugin = entry("github", {"0.9.0": sha(9), "0.10.0": sha(10)}, tier="first-party")
    plugin["repo"] = "https://github.com/horae-dev/plugin-github"
    (index,) = (_loaded(tmp_path, [plugin]),)
    found = resolve("github", index)
    assert (found.name, found.version, found.sha) == ("github", "0.10.0", sha(10))
    assert found.repo == "https://github.com/horae-dev/plugin-github"
    assert found.tier == "first-party"
    assert found.index == A
    assert resolve("github@0.9.0", index).sha == sha(9)


def test_a_prerelease_is_chosen_only_when_allowed_or_named(tmp_path: Path) -> None:
    index = _loaded(tmp_path, [entry("p", ["1.0.0", "2.0.0-rc.1"]), entry("q", ["1.0.0-beta.1"])])
    assert resolve("p", index).version == "1.0.0"
    assert resolve("p", index, allow_prerelease=True).version == "2.0.0-rc.1"
    assert resolve("p@2.0.0-rc.1", index).version == "2.0.0-rc.1"
    with pytest.raises(UnknownVersion, match=r"only pre-releases.*--pre"):
        resolve("q", index)
    assert resolve("q", index, allow_prerelease=True).version == "1.0.0-beta.1"


def test_unknown_name_and_version_are_clear_errors(tmp_path: Path) -> None:
    index = _loaded(tmp_path, [entry("one", ["1.0.0", "1.2.0"])])
    with pytest.raises(UnknownPlugin, match=r"no marketplace lists plugin 'nope'.*example.org/a"):
        resolve("nope", index)
    with pytest.raises(UnknownVersion, match=r"no version 9.9.9.*available: 1.2.0, 1.0.0"):
        resolve("one@9.9.9", index)


@pytest.mark.parametrize("ref", ["", "One", "a@", "a@1", "a@v1.0.0", "a@1.0.0@2", "a b", "--x"])
def test_a_malformed_reference_is_refused(ref: str) -> None:
    with pytest.raises(InvalidReference):
        parse_ref(ref)
    assert parse_ref("a") == ("a", None)
    assert parse_ref("a-b@1.0.0-rc.1") == ("a-b", "1.0.0-rc.1")


def test_a_name_in_several_indexes_with_different_repos_is_a_conflict(tmp_path: Path) -> None:
    index = _loaded(
        tmp_path,
        [entry("tool", ["1.0.0"], repo="https://github.com/a/tool"), entry("only-a", ["1.0.0"])],
        [entry("tool", ["2.0.0"], repo="https://github.com/b/tool"), entry("only-b", ["1.0.0"])],
    )
    with pytest.raises(ConflictError) as raised:
        resolve("tool", index)
    message = str(raised.value)
    assert A in message
    assert B in message
    assert "https://github.com/a/tool" in message
    assert "https://github.com/b/tool" in message
    assert "marketplace_pins" in message  # says how to choose
    assert message.index(A) < message.index(B)  # in priority order
    # names that are not in conflict still resolve, whichever index has them
    assert resolve("only-a", index).index == A
    assert resolve("only-b", index).index == B


def test_a_pin_chooses_the_index_whatever_the_order(tmp_path: Path) -> None:
    index = _loaded(
        tmp_path,
        [entry("tool", ["1.0.0"], repo="https://github.com/a/tool")],
        [entry("tool", ["2.0.0"], repo="https://github.com/b/tool")],
    )
    assert resolve("tool", index, pins={"tool": B}).repo == "https://github.com/b/tool"
    assert resolve("tool", index, pins={"tool": B}).version == "2.0.0"
    assert resolve("tool", index, pins={"tool": A}).version == "1.0.0"
    with pytest.raises(UnknownPlugin, match="not a marketplace"):
        resolve("tool", index, pins={"tool": "https://example.org/c/marketplace"})
    with pytest.raises(UnknownPlugin, match="searched: " + B):
        resolve("other", index, pins={"other": B})
    # the message's own advice works: the pin it suggests names the first index
    suggested = {"tool": A}
    assert resolve("tool", index, pins=suggested).index == A


def test_priority_order_picks_a_mirror_from_the_first_index(tmp_path: Path) -> None:
    same = "https://github.com/x/tool"
    a = [entry("tool", {"1.0.0": sha(1)}, repo=same)]
    b = [entry("tool", {"1.0.0": sha(2), "2.0.0": sha(3)}, repo=same + ".git")]
    ab = _loaded(tmp_path, a, b)
    found = resolve("tool", ab)
    assert (found.index, found.version, found.sha) == (A, "1.0.0", sha(1))  # A wins entirely
    # the same two indexes in the other order: B has priority
    ba = [ab[1], ab[0]]
    found = resolve("tool", ba)
    assert (found.index, found.version, found.sha) == (B, "2.0.0", sha(3))


def test_search_lists_by_priority_then_name(tmp_path: Path) -> None:
    index = _loaded(
        tmp_path,
        [entry("zeta", ["1.0.0", "1.1.0"]), entry("alpha", ["0.1.0"])],
        [entry("alpha", ["0.2.0"], repo="https://github.com/b/alpha"), entry("beta", ["1.0.0"])],
    )
    assert [(f.name, f.index) for f in search(index)] == [
        ("alpha", A),
        ("zeta", A),
        ("alpha", B),
        ("beta", B),
    ]
    assert [f.name for f in search(index, "ET")] == ["zeta", "beta"]
    assert search(index, "zeta")[0].versions == ("1.1.0", "1.0.0")
    assert search(index, "nothing") == []


def test_the_default_config_adapter_matches_the_index_url_pattern() -> None:
    TypeAdapter(InstanceConfig).validate_python({"marketplaces": [DEFAULT_MARKETPLACE]})


# --- hostile index repositories (review of #152) -----------------------------------------------


def test_an_index_that_is_not_utf8_is_a_failed_fetch_and_the_cache_survives(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    client.load()
    (repo / "marketplace.json").write_bytes(b'{"plugins": [], "x": "\xff\xfe"}')
    from marketplace_fixtures import git

    git(repo, "commit", "-q", "-am", "binary")
    with pytest.raises(FetchError, match="UTF-8"):
        client.refresh(A)
    loaded = client.load()
    assert loaded.indexes[0].from_cache
    assert "UTF-8" in loaded.warnings[0]


def test_a_killed_clone_leaves_nothing_that_breaks_the_next_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = _client(tmp_path, a=repo)
    real = run_git

    def killed(cwd: Path, *args: str, **kwargs: Any) -> str:
        if args[0] == "clone":
            Path(args[-1]).joinpath("HEAD").write_text("half a clone")
            raise GitError("git clone timed out after 120s")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(index_module, "run_git", killed)
    with pytest.raises(NoCacheError):
        client.load()
    monkeypatch.setattr(index_module, "run_git", real)
    assert [p.name for p in (client.cache_dir).glob("*/*")] == []
    assert client.load().indexes[0].index.plugins[0].name == "one"


def test_the_clone_is_shallow(tmp_path: Path) -> None:
    from marketplace_fixtures import git

    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    for n in range(3):
        publish(repo, [entry("one", [f"1.0.{n + 1}"])])
    client = _client(tmp_path, a=repo)
    client.load()
    bare = next(client.cache_dir.glob("*/repo.git"))
    assert git(bare, "rev-list", "--count", "refs/horae/head") == "1"
