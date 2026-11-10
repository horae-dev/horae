# SPDX-License-Identifier: Apache-2.0
"""`horae plugin upgrade` (#47, schemas decision 73): the permission diff, re-approval, the
atomic swap and its rollback, over local fixture repositories (no network)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae import cli_plugin, cli_plugin_manage
from horae.contracts.lockfile import LockedPlugin, UserGrants
from horae.contracts.plugin import PluginManifest
from horae.marketplace import InstallError, approval
from horae.marketplace.upgrade import Swap, permission_diff, user_grants, widens
from marketplace_fixtures import PLUGIN_URL
from upgrade_fixtures import (
    PERMISSIONS,
    PERSONAL,
    Market,
    add,
    as_json,
    cli,
    hidden,
    inst,  # noqa: F401
    lock,
    manifest,
    market,  # noqa: F401
    record,
    snapshot,
)


def _with(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(PERMISSIONS)
    data.update(changes)
    return data


# --- the permission-widening matrix ----------------------------------------------------------

WIDENING: dict[str, tuple[dict[str, Any], dict[str, list[str]], str]] = {
    "new host": (_with(network=["api.example.org", "evil.example.net"]), PERSONAL, "network"),
    "new local endpoint": (
        _with(network_local=["192.168.1.10:8123", "10.0.0.9:80"]),
        PERSONAL,
        "network_local",
    ),
    "new required secret": (_with(secrets=["DEMO_TOKEN", "DEMO_ADMIN"]), PERSONAL, "secrets"),
    "new optional secret": (
        _with(optional_secrets=["DEMO_EXTRA", "DEMO_MORE"]),
        PERSONAL,
        "secrets",
    ),
    "new intent": (_with(intents=["note.create", "http.post"]), PERSONAL, "intents"),
    "new read path": (
        _with(paths={"read": ["/srv/inbox", "/srv/other"], "write": ["/srv/outbox"]}),
        PERSONAL,
        "paths.read",
    ),
    "read path now written": (
        _with(paths={"read": [], "write": ["/srv/inbox", "/srv/outbox"]}),
        PERSONAL,
        "paths.write",
    ),
    "personal field dropped": (
        PERMISSIONS,
        {"demo.fetch": ["children[].first_name"]},
        "personal_fields",
    ),
}

NARROWING: dict[str, tuple[dict[str, Any], dict[str, list[str]]]] = {
    "host removed": (_with(network=[]), PERSONAL),
    "intent removed": (_with(intents=[]), PERSONAL),
    "secret made optional": (
        _with(secrets=[], optional_secrets=["DEMO_EXTRA", "DEMO_TOKEN"]),
        PERSONAL,
    ),
    "write path removed": (_with(paths={"read": ["/srv/inbox"], "write": []}), PERSONAL),
    "personal field added": (
        PERMISSIONS,
        {"demo.fetch": [*PERSONAL["demo.fetch"], "children[].class"]},
    ),
    "nothing changes": (PERMISSIONS, PERSONAL),
}


def _release(where: Market, permissions: dict[str, Any], personal: dict[str, list[str]]) -> str:
    provides = {"sources": ["demo.fetch"], "personal_fields": personal}
    return where.release("1.1.0", manifest("1.1.0", permissions, provides=provides))


@pytest.mark.parametrize("case", sorted(WIDENING))
def test_a_widening_upgrade_is_refused_with_yes_and_changes_nothing(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    case: str,
) -> None:
    add(inst)
    before = snapshot(inst)
    permissions, personal, field = WIDENING[case]
    _release(market, permissions, personal)
    code, out, err = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3, out
    error = as_json(out)["error"]
    assert error["kind"] == "approval_refused"
    assert field in error["message"]
    assert "(new: needs approval)" in err
    assert snapshot(inst) == before  # old tree, venv and lock untouched
    assert hidden(inst) == []


@pytest.mark.parametrize("case", sorted(WIDENING))
def test_a_widening_upgrade_needs_an_interactive_yes(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    case: str,
) -> None:
    add(inst)
    before = snapshot(inst)
    permissions, personal, _field = WIDENING[case]
    second = _release(market, permissions, personal)
    code, out, _ = cli(inst, "upgrade", "demo", answer="n\n")
    assert code == 3, out
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "upgrade", "demo", answer="y\n")
    assert code == 0, out
    assert "Approve and upgrade demo to 1.1.0?" in out
    locked = lock(inst)["plugins"]["demo"]
    assert (locked["version"], locked["commit"]) == ("1.1.0", second)
    assert record(inst)["version"] == "1.1.0"
    assert hidden(inst) == []


@pytest.mark.parametrize("case", sorted(NARROWING))
def test_an_upgrade_that_widens_nothing_passes_with_yes(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    case: str,
) -> None:
    add(inst)
    permissions, personal = NARROWING[case]
    second = _release(market, permissions, personal)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    locked = lock(inst)["plugins"]["demo"]
    assert (locked["version"], locked["commit"]) == ("1.1.0", second)
    assert locked["permissions"]["intents"] == sorted(permissions["intents"])
    # the new tree has its own, fresh venv (#45: never upgraded in place)
    venv = inst / "plugins" / "demo" / ".venv" / "pyvenv.cfg"
    assert venv.read_text(encoding="utf-8") == "version = 1.1.0\n"


def test_without_a_terminal_and_without_yes_an_upgrade_is_refused(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    monkeypatch.setattr(cli_plugin, "interactive", lambda: False)
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--json")
    assert code == 3
    assert as_json(out)["error"]["kind"] == "approval_refused"
    assert snapshot(inst) == before


def test_the_diff_marks_added_removed_and_changed_items(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    permissions = _with(
        network=["evil.example.net"], secrets=[], optional_secrets=["DEMO_EXTRA", "DEMO_TOKEN"]
    )
    _release(market, permissions, PERSONAL)
    code, out, _ = cli(inst, "upgrade", "demo", answer="y\n")
    assert code == 0, out
    assert "upgrade demo: 1.0.0" in out
    assert "    + evil.example.net  (new: needs approval)" in out
    assert "    - api.example.org\n" in out
    assert "    ~ DEMO_TOKEN (now optional)\n" in out


def test_json_reports_the_changes(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    first = market.versions["1.0.0"]
    second = _release(market, _with(intents=["note.create", "notify.push"]), PERSONAL)
    code, out, err = cli(inst, "upgrade", "demo@1.1.0", "--json", answer="y\n")
    assert code == 0, out
    report = as_json(out)
    assert report["from"] == {"version": "1.0.0", "commit": first, "repo": PLUGIN_URL}
    assert report["to"] == {"version": "1.1.0", "commit": second, "repo": PLUGIN_URL}
    assert report["changes"] == [
        {"field": "intents", "item": "notify.push", "change": "added", "widens": True}
    ]
    assert report["widened"] is True
    assert report["upgraded"] is True
    assert report["tier"] == "first-party"
    assert "upgrade demo" in err  # the prompt is on stderr with --json


def test_hosts_the_user_added_are_kept_and_do_not_need_a_new_approval(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst, "demo@1.0.0", "--allow-host", "home.example.org")
    _release(market, PERMISSIONS, PERSONAL)
    code, out, err = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 0, out
    assert "keeping the host you added: home.example.org" in err
    network = lock(inst)["plugins"]["demo"]["permissions"]["network"]
    assert network == ["api.example.org", "home.example.org"]


def test_grants_are_recorded_in_the_lock_and_only_when_given(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    assert "user_grants" not in lock(inst)["plugins"]["demo"]
    _release(market, PERMISSIONS, PERSONAL)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "user_grants" not in lock(inst)["plugins"]["demo"]


def test_explicit_grants_survive_two_upgrades(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst, "demo@1.0.0", "--allow-host", "home.example.org", "--allow-local", "10.0.0.7:80")
    recorded = {"network": ["home.example.org"], "network_local": ["10.0.0.7:80"]}
    assert lock(inst)["plugins"]["demo"]["user_grants"] == recorded
    _release(market, PERMISSIONS, PERSONAL)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "keeping the local endpoint you added: 10.0.0.7:80" in out
    assert lock(inst)["plugins"]["demo"]["user_grants"] == recorded
    market.release("1.2.0", manifest("1.2.0", PERMISSIONS))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    entry = lock(inst)["plugins"]["demo"]
    assert entry["user_grants"] == recorded
    assert entry["permissions"]["network"] == ["api.example.org", "home.example.org"]


def test_a_rewritten_installed_manifest_does_not_turn_declared_hosts_into_user_grants(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)  # v1 declares api.example.org; the user added nothing
    installed = inst / "plugins" / "demo" / "plugin.yaml"
    text = installed.read_text(encoding="utf-8")
    assert text.count("api.example.org") == 1
    installed.write_text(text.replace("api.example.org", "other.example.org"), encoding="utf-8")
    _release(market, _with(network=[]), PERSONAL)  # v1.1.0 drops the host
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "keeping the host" not in out
    assert "note: horae.lock records no host you added" in out
    assert "dropped: api.example.org" in out
    assert lock(inst)["plugins"]["demo"]["permissions"]["network"] == []


def test_a_lock_without_a_record_carries_nothing_and_says_so(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst, "demo@1.0.0", "--allow-host", "home.example.org")
    path = inst / "horae.lock"
    data = lock(inst)
    del data["plugins"]["demo"]["user_grants"]  # a lock written before the record existed
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    _release(market, PERMISSIONS, PERSONAL)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "keeping the host" not in out
    assert "dropped: home.example.org" in out
    assert "--allow-host" in out
    assert lock(inst)["plugins"]["demo"]["permissions"]["network"] == ["api.example.org"]
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-host", "home.example.org", answer="y\n")
    assert code == 0, out
    assert lock(inst)["plugins"]["demo"]["user_grants"] == {"network": ["home.example.org"]}


def test_a_new_grant_at_upgrade_needs_approval(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    _release(market, PERMISSIONS, PERSONAL)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--allow-host", "x.example.org")
    assert code == 3, out
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-host", "x.example.org", answer="y\n")
    assert code == 0, out
    network = lock(inst)["plugins"]["demo"]["permissions"]["network"]
    assert network == ["api.example.org", "x.example.org"]


# --- versions ----------------------------------------------------------------------------------


def test_a_downgrade_is_refused_unless_allowed(inst: Path, market: Market) -> None:  # noqa: F811
    market.release("1.1.0", manifest("1.1.0"))
    add(inst, "demo@1.1.0")
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo@1.0.0", "--yes", "--json")
    assert code == 3
    assert as_json(out)["error"]["kind"] == "downgrade_refused"
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "upgrade", "demo@1.0.0", "--yes", "--allow-downgrade")
    assert code == 0, out
    assert lock(inst)["plugins"]["demo"]["version"] == "1.0.0"


def test_the_current_version_is_a_no_op(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--json")
    assert code == 0, out
    assert as_json(out)["upgraded"] is False
    assert snapshot(inst) == before


def test_a_version_moved_to_another_commit_is_refused(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    before = snapshot(inst)
    market.release("1.0.0", files={"server.py": "moved = True\n"})  # v1.0.0 re-tagged
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3
    error = as_json(out)["error"]
    assert error["kind"] == "install_refused"
    assert "tampered" in error["message"]
    assert snapshot(inst) == before


def test_pre_releases_need_pre(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    market.release("1.1.0-rc.1", manifest("1.1.0-rc.1"))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert (code, as_json(out)["upgraded"]) == (0, False)
    code, out, _ = cli(inst, "upgrade", "demo", "--pre", "--yes")
    assert code == 0, out
    assert lock(inst)["plugins"]["demo"]["version"] == "1.1.0-rc.1"


# --- what may be upgraded ----------------------------------------------------------------------


def test_a_plugin_that_is_not_installed_is_refused(inst: Path, market: Market) -> None:  # noqa: F811
    code, out, _ = cli(inst, "upgrade", "demo", "--json")
    assert code == 3
    assert as_json(out)["error"]["kind"] == "not_installed"


def test_a_plugin_without_an_install_record_is_refused(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    (inst / "plugins" / "demo" / ".horae-install.json").unlink()  # like a built-in plugin
    code, out, _ = cli(inst, "upgrade", "demo", "--json")
    assert code == 3
    error = as_json(out)["error"]
    assert error["kind"] == "not_installed"
    assert "install record" in error["message"]


def test_a_url_is_a_usage_error(inst: Path) -> None:  # noqa: F811
    code, out, _ = cli(inst, "upgrade", f"{PLUGIN_URL}@{'a' * 40}", "--json")
    assert code == 2
    assert as_json(out)["error"]["kind"] == "usage"


def _make_direct(instance: Path) -> None:
    text = (instance / "horae.lock").read_text(encoding="utf-8")
    (instance / "horae.lock").write_text(
        text.replace("    version: 1.0.0\n", "    version: 1.0.0\n    source: direct\n"),
        encoding="utf-8",
    )


def test_a_direct_install_stays_refused_before_m5(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    _make_direct(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3
    assert as_json(out)["error"]["kind"] == "tier_refused"
    assert snapshot(inst) == before


def test_behind_the_gate_a_direct_install_is_replaced_not_upgraded(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval, "DIRECT_INSTALLS", True)
    add(inst)
    _make_direct(inst)
    market.release("1.1.0", manifest("1.1.0"))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3
    error = as_json(out)["error"]
    assert error["kind"] == "install_refused"
    assert "horae plugin remove demo" in error["message"]


def test_a_plugin_whose_tier_is_no_longer_first_party_is_refused(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    market.tier = "community"
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 3
    assert as_json(out)["error"]["kind"] == "tier_refused"
    assert snapshot(inst) == before


# --- atomicity and rollback --------------------------------------------------------------------


def test_a_failed_venv_build_leaves_the_old_version(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)

    def broken(_tree: Path, _manifest: PluginManifest) -> None:
        raise InstallError("uv failed")

    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: broken)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 1
    assert as_json(out)["error"]["kind"] == "install_failed"
    assert snapshot(inst) == before
    assert hidden(inst) == []


def test_a_failed_lock_write_rolls_the_swap_back(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)

    def failing(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli_plugin_manage, "write_lock", failing)
    code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 1
    error = as_json(out)["error"]
    assert error["kind"] == "upgrade_failed"
    assert "previous version is kept" in error["message"]
    assert snapshot(inst) == before  # old tree back in place, venv included, lock unchanged
    assert hidden(inst) == []


def test_a_failed_swap_leaves_the_old_tree(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    real = Swap.move

    def move(self: Swap, source: Path, target: Path) -> None:
        if source.name.startswith(".upgrade-new-"):
            raise OSError(5, "Input/output error")
        real(self, source, target)

    monkeypatch.setattr(Swap, "move", move)
    code, _, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 1
    assert snapshot(inst) == before
    assert hidden(inst) == []


def test_another_writer_of_the_lock_is_refused(inst: Path, market: Market) -> None:  # noqa: F811
    from horae.marketplace.lock import guard

    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    with guard(inst / "state"):
        code, out, _ = cli(inst, "upgrade", "demo", "--yes", "--json")
    assert code == 5
    assert as_json(out)["error"]["kind"] == "lock_busy"


# --- units -------------------------------------------------------------------------------------


def _entry(**permissions: Any) -> LockedPlugin:
    return LockedPlugin.model_validate(
        {
            "version": "1.0.0",
            "repo": PLUGIN_URL,
            "commit": "a" * 40,
            "permissions": permissions,
        }
    )


def test_the_diff_of_pipelines_and_repository() -> None:
    old = _entry().model_copy(update={"pipelines": {"a": "1" * 64, "b": "2" * 64}})
    new = old.model_copy(
        update={
            "repo": "https://example.org/x/moved",
            "pipelines": {"b": "3" * 64, "c": "4" * 64},
        }
    )
    changes = {(c.field, c.item, c.change, c.widens) for c in permission_diff(old, new)}
    assert changes == {
        ("repo", f"{PLUGIN_URL} -> https://example.org/x/moved", "changed", True),
        ("pipelines", "a", "removed", False),
        ("pipelines", "b", "changed", True),
        ("pipelines", "c", "added", True),
    }
    assert not widens(permission_diff(old, old))
    direct = old.model_copy(update={"source": "direct"})
    assert widens(permission_diff(old, direct))


def test_user_grants_are_only_what_the_lock_records() -> None:
    plain = _entry(network=["a.example.org", "b.example.org"], network_local=["10.0.0.1:80"])
    assert user_grants(plain) == ([], [])  # no record: nothing is inferred
    recorded = plain.model_copy(
        update={"user_grants": UserGrants(network=["b.example.org"], network_local=["10.0.0.1:80"])}
    )
    assert user_grants(recorded) == (["b.example.org"], ["10.0.0.1:80"])


def test_a_lock_cannot_record_a_grant_it_does_not_give() -> None:
    with pytest.raises(ValueError, match="which permissions do not grant"):
        LockedPlugin.model_validate(
            {
                "version": "1.0.0",
                "repo": PLUGIN_URL,
                "commit": "a" * 40,
                "permissions": {"network": ["a.example.org"]},
                "user_grants": {"network": ["b.example.org"]},
            }
        )


def test_lock_mismatch_compares_the_source() -> None:
    entry = _entry()
    assert approval.lock_mismatch(entry, entry.model_copy(update={"source": "direct"})) == (
        "horae.lock records a marketplace install, not a direct one"
    )


def test_the_source_is_left_out_of_the_lock_for_a_marketplace_install() -> None:
    entry = _entry()
    assert "source" not in json.loads(entry.model_dump_json())
    assert entry.model_copy(update={"source": "direct"}).model_dump()["source"] == "direct"


def test_a_plugin_that_is_not_a_pack_never_touches_a_pipelines_directory(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    mine = inst / "pipelines" / "demo"
    mine.mkdir()
    (mine / "notes.txt").write_text("mine\n", encoding="utf-8")
    market.release("1.1.0", manifest("1.1.0"))
    code, out, err = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out + err
    assert (mine / "notes.txt").read_text(encoding="utf-8") == "mine\n"
