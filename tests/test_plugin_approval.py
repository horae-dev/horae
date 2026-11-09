# SPDX-License-Identifier: Apache-2.0
"""Permission approval and `horae.lock` (#46, schemas decision 70), over local fixture
repositories (no network)."""

import difflib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.config import load_instance
from horae.contracts.instance import DEFAULT_MARKETPLACE
from horae.contracts.intents import INTENT_TYPES
from horae.contracts.lockfile import Lockfile
from horae.marketplace import LockInvalid, MarketplaceClient, TierRefused, approval
from horae.marketplace.approval import INTENT_MEANINGS, require_installable
from horae.marketplace.install import INSTALL_RECORD
from horae.marketplace.lock import DEFAULT_HEADER, dump_lock, guard, read_lock
from horae.plugins.host import HostRegistry, RegistryError
from marketplace_fixtures import (
    PLUGIN_URL,
    A,
    LocalClient,
    LocalInstaller,
    entry,
    plugin_manifest,
    plugin_repo,
    publish,
)

runner = CliRunner()
COMMUNITY_URL = "https://example.org/x/plugin-other"

MANIFEST = plugin_manifest(
    provides={
        "sources": ["demo.fetch"],
        "personal_fields": {"demo.fetch": ["children[].first_name"]},
    },
    permissions={
        "network": ["api.example.org"],
        "network_local": ["192.168.1.10:8123"],
        "secrets": ["DEMO_TOKEN"],
        "optional_secrets": ["DEMO_EXTRA"],
        "intents": ["notify.push", "note.create"],
        "paths": {"read": ["/srv/inbox"], "write": ["/srv/outbox"]},
    },
)


@pytest.fixture
def market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An index with `demo` (first-party, 1.0.0 and 1.1.0) and `other` (community)."""
    repo, first = plugin_repo(tmp_path / "plugin-demo", manifest=MANIFEST)
    _, second = plugin_repo(repo, version="1.1.0", manifest=MANIFEST.replace('"1.0.0"', '"1.1.0"'))
    other, other_sha = plugin_repo(tmp_path / "plugin-other", name="other")
    index = publish(
        tmp_path / "index",
        [
            entry("demo", {"1.0.0": first, "1.1.0": second}, repo=PLUGIN_URL, tier="first-party"),
            entry("other", {"1.0.0": other_sha}, repo=COMMUNITY_URL, tier="community"),
        ],
    )

    def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
        return LocalClient(cache_dir, urls, {A: index})

    monkeypatch.setattr(cli_plugin, "make_client", make_client)
    monkeypatch.setattr(
        cli_plugin,
        "make_installer",
        lambda where: LocalInstaller(where, {PLUGIN_URL: repo, COMMUNITY_URL: other}),
    )
    monkeypatch.setattr(cli_plugin, "interactive", lambda: True)
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({A}))
    # The fixture plugins have no dependencies: no venv to build here (#45 is tested apart).
    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: None)
    return {"first": first, "second": second}


@pytest.fixture
def inst(instance_dir: Path) -> Path:
    config = instance_dir / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(f"{text}\nmarketplaces: [{A}]\n", encoding="utf-8")
    return instance_dir


def _add(instance: Path, *args: str, answer: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", "add", *args, "-i", str(instance)], input=answer)
    return result.exit_code, result.stdout, result.stderr


def _lock(instance: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((instance / "horae.lock").read_text(encoding="utf-8"))
    return data


def _json(out: str) -> Any:
    """The JSON document of stdout (CliRunner echoes the typed answer there; a terminal
    does not)."""
    return json.loads(out[out.index("{") :])


def _error(out: str) -> dict[str, str]:
    error: dict[str, str] = _json(out)["error"]
    return error


def _without_lock(instance: Path) -> None:
    """Delete horae.lock (and the horae.yaml settings of the plugin it pinned)."""
    (instance / "horae.lock").unlink()
    config = instance / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("plugins:\n  github: { call_timeout_s: 20 }\n", ""), "utf-8")


def _nothing_installed(instance: Path) -> bool:
    plugins = instance / "plugins"
    return not plugins.exists() or not list(plugins.iterdir())


# --- approval --------------------------------------------------------------------------------


def test_the_prompt_groups_and_labels_every_permission_and_writes_the_lock(
    inst: Path, market: dict[str, str]
) -> None:
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    for text in (
        "demo@1.0.0 asks for these permissions:",
        "network (https hosts):\n    - api.example.org",
        "local network (plain http allowed):\n    - 192.168.1.10:8123",
        "secrets (required):\n    - DEMO_TOKEN",
        "secrets (optional):\n    - DEMO_EXTRA",
        f"    - note.create: {INTENT_MEANINGS['note.create']}",
        f"    - notify.push: {INTENT_MEANINGS['notify.push']}",
        "paths (read):\n    - /srv/inbox",
        "paths (read and write):\n    - /srv/outbox",
        "they are de-identified before cloud models",
        "    - demo.fetch: children[].first_name",
        "Approve these permissions and install demo?",
        "approved: horae.lock pins it",
    ):
        assert text in out
    locked = _lock(inst)["plugins"]["demo"]
    assert locked == {
        "commit": market["first"],
        "permissions": {
            "intents": ["note.create", "notify.push"],
            "network": ["api.example.org"],
            "network_local": ["192.168.1.10:8123"],
            "optional_secrets": ["DEMO_EXTRA"],
            "paths": {"read": ["/srv/inbox"], "write": ["/srv/outbox"]},
            "secrets": ["DEMO_TOKEN"],
        },
        "personal_fields": {"demo.fetch": ["children[].first_name"]},
        "repo": PLUGIN_URL,
        "version": "1.0.0",
    }
    assert "github" in _lock(inst)["plugins"]  # the other entries are kept
    lock = load_instance(inst).lock
    registry = HostRegistry.from_directory(
        Lockfile(schema_version=1, plugins={"demo": lock.plugins["demo"]}), inst / "plugins"
    )
    assert registry.installed("demo").commit == market["first"]


def test_declining_installs_nothing_and_keeps_the_lock(inst: Path, market: dict[str, str]) -> None:
    before = (inst / "horae.lock").read_bytes()
    for answer in ("n\n", "", None):  # no, empty (the default is no), end of input
        code, out, _ = _add(inst, "demo@1.0.0", "--json", answer=answer)
        assert code == 3, out
        assert _error(out)["kind"] == "approval_refused"
        assert _nothing_installed(inst)
        assert (inst / "horae.lock").read_bytes() == before


def test_without_a_terminal_and_without_yes_the_install_is_refused(
    inst: Path, market: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_plugin, "interactive", lambda: False)
    before = (inst / "horae.lock").read_bytes()
    code, out, err = _add(inst, "demo@1.0.0", "--json", answer="y\n")
    assert code == 3
    error = _error(out)
    assert error["kind"] == "approval_refused"
    assert "--yes" in error["message"]
    assert "asks for these permissions" in err  # shown on stderr with --json
    assert _nothing_installed(inst)
    assert (inst / "horae.lock").read_bytes() == before


def test_the_default_interactive_check_is_a_terminal_check() -> None:
    assert cli_plugin.interactive() is False  # pytest's stdin is not a terminal


def test_json_output_reports_the_approved_permissions(inst: Path, market: dict[str, str]) -> None:
    code, out, err = _add(inst, "demo@1.0.0", "--json", answer="y\n")
    assert code == 0, out + err
    report = _json(out)
    assert report["approved"] is True
    assert report["pending"] == []
    assert report["permissions"]["network_local"] == ["192.168.1.10:8123"]
    assert "Approve these permissions" in err


# --- --yes -----------------------------------------------------------------------------------


def _approved_then_removed(inst: Path, *grants: str) -> bytes:
    """Approve demo@1.0.0 interactively, then delete its tree (as a fresh checkout of an
    instance repository whose horae.lock is committed and plugins/ ignored)."""
    code, out, _ = _add(inst, "demo@1.0.0", *grants, answer="y\n")
    assert code == 0, out
    shutil.rmtree(inst / "plugins" / "demo")
    return (inst / "horae.lock").read_bytes()


def test_yes_reinstalls_exactly_what_the_lock_approves(inst: Path, market: dict[str, str]) -> None:
    before = _approved_then_removed(inst)
    code, out, _ = _add(inst, "demo@1.0.0", "--yes")  # no input: never asks
    assert code == 0, out
    assert "Approve these permissions" not in out
    assert (inst / "plugins" / "demo" / "plugin.yaml").is_file()
    assert (inst / "horae.lock").read_bytes() == before


def _edit_lock(inst: Path, change: Any) -> None:
    data = _lock(inst)
    change(data["plugins"]["demo"])
    (inst / "horae.lock").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda e: e.clear(), "no entry"),
        (lambda e: e.update(version="1.1.0"), "pins version 1.1.0"),
        (lambda e: e.update(commit="f" * 40), "pins commit"),
        (lambda e: e["permissions"]["network"].append("more.example.org"), "network"),
        (lambda e: e["permissions"].update(intents=["note.create"]), "intents"),
        (lambda e: e["permissions"].pop("network_local"), "network_local"),
        (lambda e: e["permissions"]["paths"].update(write=[]), "paths"),
        (lambda e: e["permissions"].update(optional_secrets=[]), "optional_secrets"),
    ],
    ids=[
        "no-entry",
        "other-version",
        "other-commit",
        "lock-grants-more",
        "lock-grants-fewer-intents",
        "lock-lacks-local",
        "lock-lacks-path",
        "lock-lacks-optional-secret",
    ],
)
def test_yes_is_refused_unless_the_lock_entry_matches_exactly(
    inst: Path, market: dict[str, str], change: Any, why: str
) -> None:
    _approved_then_removed(inst)
    if why == "no entry":
        data = _lock(inst)
        del data["plugins"]["demo"]
        (inst / "horae.lock").write_text(yaml.safe_dump(data), encoding="utf-8")
    else:
        _edit_lock(inst, change)
    before = (inst / "horae.lock").read_bytes()
    code, out, _ = _add(inst, "demo@1.0.0", "--yes", "--json")
    assert code == 3, out
    error = _error(out)
    assert error["kind"] == "approval_refused"
    assert why in error["message"]
    assert "--yes approves only what horae.lock already approves" in error["message"]
    assert _nothing_installed(inst)
    assert (inst / "horae.lock").read_bytes() == before


def test_yes_without_any_lock_is_refused(inst: Path, market: dict[str, str]) -> None:
    _without_lock(inst)
    code, out, _ = _add(inst, "demo@1.0.0", "--yes", "--json")
    assert code == 3
    assert "no entry" in _error(out)["message"]
    assert not (inst / "horae.lock").exists()
    assert _nothing_installed(inst)


def test_yes_for_another_resolved_version_is_refused(inst: Path, market: dict[str, str]) -> None:
    _approved_then_removed(inst)
    code, out, _ = _add(inst, "demo", "--yes", "--json")  # resolves 1.1.0
    assert code == 3
    assert "pins version 1.0.0, not 1.1.0" in _error(out)["message"]


def test_yes_needs_the_same_extra_grants(inst: Path, market: dict[str, str]) -> None:
    grants = ("--allow-host", "ha.example.org", "--allow-local", "192.168.1.20:8123")
    before = _approved_then_removed(inst, *grants)
    code, out, _ = _add(inst, "demo@1.0.0", "--yes", "--json")
    assert code == 3
    assert "network, network_local differ" in _error(out)["message"]
    code, out, _ = _add(inst, "demo@1.0.0", "--yes", *grants)
    assert code == 0, out
    assert (inst / "horae.lock").read_bytes() == before


# --- extra grants ----------------------------------------------------------------------------


def test_extra_grants_are_shown_and_written_to_the_lock(inst: Path, market: dict[str, str]) -> None:
    code, out, _ = _add(
        inst,
        "demo@1.0.0",
        "--allow-host",
        "ha.example.org",
        "--allow-host",
        "ha.example.org",
        "--allow-local",
        "homeassistant.local:8123",
        answer="y\n",
    )
    assert code == 0, out
    assert "    - ha.example.org  (added by you)" in out
    assert "    - homeassistant.local:8123  (added by you)" in out
    assert "    - api.example.org\n" in out
    permissions = _lock(inst)["plugins"]["demo"]["permissions"]
    assert permissions["network"] == ["api.example.org", "ha.example.org"]
    assert permissions["network_local"] == ["192.168.1.10:8123", "homeassistant.local:8123"]
    # The host accepts a lock granting more than the manifest declares (decision 66).
    lock = load_instance(inst).lock
    HostRegistry.from_directory(
        Lockfile(schema_version=1, plugins={"demo": lock.plugins["demo"]}), inst / "plugins"
    )


@pytest.mark.parametrize(
    "grant",
    [
        ("--allow-host", "http://ha.example.org"),
        ("--allow-host", "192.168.1.10"),
        ("--allow-host", "localhost"),
        ("--allow-local", "192.168.1.10"),  # no port
        ("--allow-local", "8.8.8.8:53"),  # not local
        ("--allow-local", "169.254.169.254:80"),  # metadata endpoint
    ],
)
def test_invalid_extra_grants_are_a_usage_error(
    inst: Path, market: dict[str, str], grant: tuple[str, str]
) -> None:
    code, out, _ = _add(inst, "demo@1.0.0", *grant, "--json", answer="y\n")
    assert code == 2, out
    assert _error(out)["kind"] == "usage"
    assert grant[0] in _error(out)["message"]
    assert _nothing_installed(inst)


# --- tier (threat model T04) -----------------------------------------------------------------


def test_a_community_plugin_is_refused_before_m5(inst: Path, market: dict[str, str]) -> None:
    before = (inst / "horae.lock").read_bytes()
    code, out, _ = _add(inst, "other", "--json", answer="y\n")
    assert code == 3
    error = _error(out)
    assert error["kind"] == "tier_refused"
    assert "'community'" in error["message"]
    assert "M5" in error["message"]
    assert _nothing_installed(inst)
    assert (inst / "horae.lock").read_bytes() == before


def test_a_first_party_claim_of_an_untrusted_index_is_refused_before_any_clone(
    inst: Path, market: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({"https://example.org/x"}))
    before = (inst / "horae.lock").read_bytes()
    code, out, _ = _add(inst, "demo", "--json", answer="y\n")
    assert code == 3
    assert _error(out)["kind"] == "tier_refused"
    assert _nothing_installed(inst)
    assert (inst / "horae.lock").read_bytes() == before


def test_the_tier_rule_is_shared_with_direct_installs() -> None:
    require_installable("demo", "first-party", DEFAULT_MARKETPLACE)
    with pytest.raises(TierRefused, match="M5"):
        require_installable("demo", "community", DEFAULT_MARKETPLACE)
    with pytest.raises(TierRefused, match="direct install"):
        require_installable("demo", None, None)  # #48: a git URL has no tier
    for tier in ("First-Party", "first-party ", "FIRST-PARTY", "unknown", ""):
        with pytest.raises(TierRefused):
            require_installable("demo", tier, DEFAULT_MARKETPLACE)  # type: ignore[arg-type]


def test_an_added_index_cannot_vouch_for_its_own_first_party_tier() -> None:
    with pytest.raises(TierRefused, match="not the Horae marketplace"):
        require_installable("demo", "first-party", "https://example.org/evil/market")
    with pytest.raises(TierRefused):
        require_installable("demo", "first-party", None)


def test_every_intent_has_a_meaning() -> None:
    assert set(INTENT_MEANINGS) == set(INTENT_TYPES)


# --- the lock file ---------------------------------------------------------------------------


def _shuffled(data: Any) -> Any:
    """`data` with every mapping's keys and every list reversed."""
    if isinstance(data, dict):
        return {k: _shuffled(v) for k, v in reversed(list(data.items()))}
    if isinstance(data, list):
        return [_shuffled(v) for v in reversed(data)]
    return data


def test_the_lock_text_is_canonical_and_byte_stable(inst: Path, market: dict[str, str]) -> None:
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    text = (inst / "horae.lock").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.startswith("# horae.lock as written by")  # the existing header is kept
    lock, header = read_lock(inst / "horae.lock")
    assert dump_lock(lock, header) == text
    reparsed = Lockfile.model_validate(_shuffled(yaml.safe_load(text)))
    assert dump_lock(reparsed, header) == text  # any order in, the same bytes out
    lines = text.splitlines()
    assert all(not ln.endswith("]") or ln.endswith(": []") for ln in lines)  # block style
    body = yaml.safe_load(text)
    assert list(body) == sorted(body)
    assert list(body["plugins"]["demo"]) == sorted(body["plugins"]["demo"])


def test_adding_a_plugin_changes_only_its_own_entry(inst: Path, market: dict[str, str]) -> None:
    lock, header = read_lock(inst / "horae.lock")
    (inst / "horae.lock").write_text(dump_lock(lock, header), encoding="utf-8")  # canonical
    before = (inst / "horae.lock").read_text(encoding="utf-8").splitlines()
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    after = (inst / "horae.lock").read_text(encoding="utf-8").splitlines()
    changed = [
        line
        for line in difflib.unified_diff(before, after, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed
    assert all(line.startswith("+") for line in changed)
    added = [line[1:] for line in changed]
    assert added[0] == "  demo:"
    assert all(line.startswith("    ") for line in added[1:])


def test_a_new_lock_gets_the_default_header(inst: Path, market: dict[str, str]) -> None:
    _without_lock(inst)
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    text = (inst / "horae.lock").read_text(encoding="utf-8")
    assert text.startswith(DEFAULT_HEADER)
    assert list(Lockfile.model_validate(yaml.safe_load(text)).plugins) == ["demo"]


def test_the_lock_horae_init_writes_is_canonical(tmp_path: Path) -> None:
    root = tmp_path / "new"
    result = runner.invoke(app, ["init", str(root)])
    assert result.exit_code == 0, result.output
    text = (root / "horae.lock").read_text(encoding="utf-8")
    lock, header = read_lock(root / "horae.lock")
    assert header.startswith("# horae.lock written by `horae init`")
    assert dump_lock(lock, header) == text


def test_an_invalid_lock_is_never_rewritten(inst: Path, market: dict[str, str]) -> None:
    (inst / "horae.lock").write_text("schema_version: 1\nplugins: {demo: {}}\n", encoding="utf-8")
    before = (inst / "horae.lock").read_bytes()
    code, out, _ = _add(inst, "demo@1.0.0", "--json", answer="y\n")
    assert code == 1
    assert _error(out)["kind"] == "config_invalid"  # the instance is checked first
    assert (inst / "horae.lock").read_bytes() == before
    assert _nothing_installed(inst)


def test_read_lock_refuses_an_invalid_or_odd_file(tmp_path: Path) -> None:
    path = tmp_path / "horae.lock"
    path.write_text("schema_version: 1\nplugins: {demo: {}}\n", encoding="utf-8")
    with pytest.raises(LockInvalid, match="invalid, fix it first"):
        read_lock(path)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(LockInvalid, match="not a regular file"):
        read_lock(path)
    path.unlink()
    assert read_lock(path) == (Lockfile(schema_version=1), DEFAULT_HEADER)


def test_a_second_writer_is_refused(inst: Path, market: dict[str, str]) -> None:
    with guard(load_instance(inst).state_dir):
        code, out, _ = _add(inst, "demo@1.0.0", "--json", answer="y\n")
    assert code == 5
    assert _error(out)["kind"] == "lock_busy"
    assert _nothing_installed(inst)


def test_a_failed_lock_write_undoes_the_install(
    inst: Path, market: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (inst / "horae.lock").read_bytes()

    real = os.replace

    def fail(src: Any, dst: Any) -> None:
        if Path(dst).name == "horae.lock":
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr("horae.marketplace.lock.os.replace", fail)
    code, out, _ = _add(inst, "demo@1.0.0", "--json", answer="y\n")
    assert code == 1
    assert _error(out)["kind"] == "lock_write_failed"
    assert (inst / "horae.lock").read_bytes() == before
    assert sorted(p.name for p in inst.iterdir() if p.name.startswith(".horae.lock")) == []
    assert _nothing_installed(inst)


def test_the_lock_keeps_its_file_mode(inst: Path, market: dict[str, str]) -> None:
    os.chmod(inst / "horae.lock", 0o640)
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    assert os.stat(inst / "horae.lock").st_mode & 0o777 == 0o640


# --- run refuses what the lock does not approve ----------------------------------------------


def _installed(inst: Path) -> tuple[Lockfile, Path]:
    code, out, _ = _add(inst, "demo@1.0.0", answer="y\n")
    assert code == 0, out
    lock = load_instance(inst).lock
    return Lockfile(schema_version=1, plugins={"demo": lock.plugins["demo"]}), inst / "plugins"


def _rewrite_record(plugins: Path, **changes: Any) -> None:
    path = plugins / "demo" / INSTALL_RECORD
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, **changes}), encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"commit": "e" * 40}, "commit"),
        ({"version": "1.1.0"}, "version"),
        ({"repo": "https://example.org/x/elsewhere"}, "repo"),
        ({"name": "other"}, "name"),
    ],
)
def test_run_refuses_a_tree_that_is_not_the_locked_one(
    inst: Path, market: dict[str, str], changes: dict[str, str], field: str
) -> None:
    lock, plugins = _installed(inst)
    _rewrite_record(plugins, **changes)
    with pytest.raises(RegistryError, match=f"installed with {field}"):
        HostRegistry.from_directory(lock, plugins)


def test_run_refuses_a_lock_pinning_another_commit(inst: Path, market: dict[str, str]) -> None:
    lock, plugins = _installed(inst)
    moved = lock.plugins["demo"].model_copy(update={"commit": "d" * 40})
    with pytest.raises(RegistryError, match=r"horae\.lock pins commit"):
        HostRegistry.from_directory(Lockfile(schema_version=1, plugins={"demo": moved}), plugins)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "x" * (65 * 1024)])
def test_run_refuses_an_unreadable_record(inst: Path, market: dict[str, str], content: str) -> None:
    lock, plugins = _installed(inst)
    (plugins / "demo" / INSTALL_RECORD).write_text(content, encoding="utf-8")
    with pytest.raises(RegistryError, match=INSTALL_RECORD):
        HostRegistry.from_directory(lock, plugins)


def test_run_refuses_a_symlinked_record(inst: Path, market: dict[str, str]) -> None:
    lock, plugins = _installed(inst)
    record = plugins / "demo" / INSTALL_RECORD
    copy = plugins / "record.json"
    record.rename(copy)
    record.symlink_to(copy)
    with pytest.raises(RegistryError, match="not a small regular file"):
        HostRegistry.from_directory(lock, plugins)


def test_run_refuses_a_manifest_asking_for_more_than_the_lock(
    inst: Path, market: dict[str, str]
) -> None:
    lock, plugins = _installed(inst)
    narrower = lock.plugins["demo"].permissions.model_copy(update={"intents": ["note.create"]})
    entry_ = lock.plugins["demo"].model_copy(update={"permissions": narrower})
    with pytest.raises(RegistryError, match="permissions not approved"):
        HostRegistry.from_directory(Lockfile(schema_version=1, plugins={"demo": entry_}), plugins)


def test_a_plugin_without_a_record_is_checked_by_its_manifest_only(
    inst: Path, market: dict[str, str]
) -> None:
    lock, plugins = _installed(inst)
    (plugins / "demo" / INSTALL_RECORD).unlink()
    HostRegistry.from_directory(lock, plugins)  # a built-in or hand-copied plugin


def test_yes_refuses_an_entry_for_another_repository() -> None:
    from horae.contracts.lockfile import LockedPlugin
    from horae.contracts.plugin import Permissions
    from horae.marketplace.approval import lock_mismatch

    sha = "a" * 40
    perms = Permissions()
    existing = LockedPlugin(
        version="1.0.0", repo="https://example.org/a/plugin", commit=sha, permissions=perms
    )
    wanted = existing.model_copy(update={"repo": "https://example.org/b/plugin"})
    why = lock_mismatch(existing, wanted)
    assert why is not None
    assert "repository" in why
    assert lock_mismatch(existing, existing) is None
