# SPDX-License-Identifier: Apache-2.0
"""Path grants the user adds (#205): `--allow-path` / `--allow-read-path` on `horae plugin
add` and `upgrade`, recorded in `horae.lock` (`user_grants.paths`) and kept by an upgrade like
the hosts of `--allow-host` (schemas decision 73 D). Local fixture repositories, no network."""

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.contracts.lockfile import LockedPlugin, UserGrants
from horae.contracts.plugin import Permissions
from horae.marketplace import InvalidReference, approval
from horae.marketplace.upgrade import user_grants
from marketplace_fixtures import PLUGIN_URL
from upgrade_fixtures import (
    PERMISSIONS,
    Market,
    add,
    as_json,
    cli,
    inst,  # noqa: F401
    lock,
    manifest,
    market,  # noqa: F401
    snapshot,
)

VAULT = "/srv/vault"


def _with(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(PERMISSIONS)
    data.update(changes)
    return data


def _entry(instance: Path) -> dict[str, Any]:
    entry: dict[str, Any] = lock(instance)["plugins"]["demo"]
    return entry


def _edit_lock(instance: Path, change: Any) -> None:
    data = lock(instance)
    change(data["plugins"]["demo"])
    (instance / "horae.lock").write_text(yaml.safe_dump(data), encoding="utf-8")


# --- the regression of #205 ------------------------------------------------------------------


def test_an_upgrade_keeps_the_path_the_user_granted(inst: Path, market: Market) -> None:  # noqa: F811
    """The reported case: the vault granted to the plugin, a new version with the same path
    request (and, like obsidian 0.2.0, a new intent): the grant must survive the upgrade."""
    add(inst, "demo@1.0.0", "--allow-path", VAULT)
    assert VAULT in _entry(inst)["permissions"]["paths"]["write"]
    assert _entry(inst)["user_grants"] == {"paths": {"write": [VAULT]}}
    market.release("1.1.0", manifest("1.1.0", _with(intents=["note.create", "notify.push"])))
    code, out, _ = cli(inst, "upgrade", "demo", answer="y\n")
    assert code == 0, out
    assert f"keeping the read and write path you added: {VAULT}" in out
    assert f"    - {VAULT}\n" not in out  # not removed in the diff
    assert f"    - {VAULT}  (added by you)" in out  # the full request (an intent is new)
    entry = _entry(inst)
    assert entry["version"] == "1.1.0"
    assert entry["permissions"]["paths"] == {
        "read": ["/srv/inbox"],
        "write": ["/srv/outbox", VAULT],
    }
    assert entry["user_grants"] == {"paths": {"write": [VAULT]}}


def test_a_kept_path_needs_no_new_approval(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst, "demo@1.0.0", "--allow-path", VAULT, "--allow-read-path", "/srv/archive")
    market.release("1.1.0", manifest("1.1.0"))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "keeping the read path you added: /srv/archive" in out
    paths = _entry(inst)["permissions"]["paths"]
    assert paths == {"read": ["/srv/archive", "/srv/inbox"], "write": ["/srv/outbox", VAULT]}
    # and once more: the record carries it over every time
    market.release("1.2.0", manifest("1.2.0"))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert _entry(inst)["user_grants"] == {"paths": {"read": ["/srv/archive"], "write": [VAULT]}}


def test_a_path_set_by_hand_is_dropped_named_and_can_be_recorded(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    """A lock edited by hand (the only way before #205) has no record of the grant: it is never
    inferred, so the upgrade drops it, shows it in the diff and says how to keep it."""
    add(inst)
    _edit_lock(inst, lambda e: e["permissions"]["paths"]["write"].append(VAULT))
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", answer="n\n")
    assert code == 3, out
    assert f"    - {VAULT}\n" in out
    assert f"dropped: {VAULT}" in out
    assert f"--allow-path {VAULT}" in out
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-path", VAULT, "--yes")
    assert code == 0, out  # the lock already grants it: nothing widens
    entry = _entry(inst)
    assert entry["permissions"]["paths"]["write"] == ["/srv/outbox", VAULT]
    assert entry["user_grants"] == {"paths": {"write": [VAULT]}}


def test_a_grant_flag_at_the_installed_version_records_it(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    _edit_lock(inst, lambda e: e["permissions"]["paths"]["write"].append(VAULT))
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-path", VAULT, "--yes")
    assert code == 0, out
    assert "nothing to upgrade" not in out
    assert _entry(inst)["user_grants"] == {"paths": {"write": [VAULT]}}


# --- never wider -----------------------------------------------------------------------------


def test_a_path_the_new_version_no_longer_asks_for_is_dropped(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)  # 1.0.0 asks to write /srv/outbox (its own request, not a user grant)
    market.release("1.1.0", manifest("1.1.0", _with(paths={"read": ["/srv/inbox"]})))
    code, out, _ = cli(inst, "upgrade", "demo", "--yes")
    assert code == 0, out
    assert "    - /srv/outbox\n" in out
    assert "keeping the read and write path" not in out
    assert _entry(inst)["permissions"]["paths"] == {"read": ["/srv/inbox"], "write": []}


def test_a_new_path_at_upgrade_needs_approval(inst: Path, market: Market) -> None:  # noqa: F811
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    code, out, err = cli(inst, "upgrade", "demo", "--allow-path", VAULT, "--yes", "--json")
    assert code == 3, out
    assert as_json(out)["error"]["kind"] == "approval_refused"
    assert f"+ {VAULT}  (new: needs approval)" in err
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-path", VAULT, answer="y\n")
    assert code == 0, out
    assert f"    - {VAULT}  (added by you)" in out
    assert _entry(inst)["user_grants"] == {"paths": {"write": [VAULT]}}


def test_a_record_the_permissions_do_not_give_is_never_kept() -> None:
    entry = LockedPlugin.model_validate(
        {
            "version": "1.0.0",
            "repo": PLUGIN_URL,
            "commit": "a" * 40,
            "permissions": {"paths": {"write": [VAULT]}},
            "user_grants": {"paths": {"write": [VAULT]}},
        }
    )
    assert user_grants(entry).paths.write == [VAULT]
    assert user_grants(entry.model_copy(update={"user_grants": None})) == UserGrants()
    with pytest.raises(ValueError, match="which permissions do not grant"):
        LockedPlugin.model_validate(
            {
                **entry.model_dump(mode="json"),
                "user_grants": {"paths": {"read": ["/srv/other"]}},
            }
        )


# --- the flags -------------------------------------------------------------------------------

BAD_PATHS = ["vault", "./vault", "/srv/../etc", "/srv/vault/", "/", "//srv", "/srv/./vault"]


@pytest.mark.parametrize("value", BAD_PATHS)
@pytest.mark.parametrize("flag", ["--allow-path", "--allow-read-path"])
def test_an_invalid_path_is_a_usage_error_on_add(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    flag: str,
    value: str,
) -> None:
    before = snapshot(inst)
    code, out, _ = cli(inst, "add", "demo@1.0.0", flag, value, "--json", answer="y\n")
    assert code == 2, out
    error = as_json(out)["error"]
    assert error["kind"] == "usage"
    assert flag in error["message"]
    assert "normalised absolute path" in error["message"]
    assert snapshot(inst) == before


@pytest.mark.parametrize("value", BAD_PATHS)
def test_an_invalid_path_is_a_usage_error_on_upgrade(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    value: str,
) -> None:
    add(inst)
    market.release("1.1.0", manifest("1.1.0"))
    before = snapshot(inst)
    code, out, _ = cli(inst, "upgrade", "demo", "--allow-path", value, "--json", answer="y\n")
    assert code == 2, out
    assert as_json(out)["error"]["kind"] == "usage"
    assert snapshot(inst) == before


def test_add_shows_and_records_path_grants(inst: Path, market: Market) -> None:  # noqa: F811
    code, out, _ = cli(
        inst,
        "add",
        "demo@1.0.0",
        "--allow-path",
        VAULT,
        "--allow-path",
        VAULT,
        "--allow-read-path",
        VAULT,
        "--allow-read-path",
        "/srv/archive",
        answer="y\n",
    )
    assert code == 0, out
    assert f"    - {VAULT}  (added by you)" in out
    assert "    - /srv/archive  (added by you)" in out
    assert "    - /srv/outbox\n" in out  # declared by the manifest: not marked
    entry = _entry(inst)
    # a path given with both flags is granted read and write, once
    assert entry["user_grants"] == {"paths": {"read": ["/srv/archive"], "write": [VAULT]}}
    assert entry["permissions"]["paths"] == {
        "read": ["/srv/archive", "/srv/inbox"],
        "write": ["/srv/outbox", VAULT],
    }


def test_add_over_an_entry_with_a_path_says_it_drops_it(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    """`add` over an existing entry (a fresh checkout: the lock committed, `plugins/` not)
    replaces it with what the manifest and the flags grant: `--yes` refuses a lock granting
    more, and an interactive add names what it drops and the flag that keeps it."""
    add(inst, "demo@1.0.0", "--allow-path", VAULT)
    shutil.rmtree(inst / "plugins" / "demo")
    before = snapshot(inst)
    code, out, _ = cli(inst, "add", "demo@1.0.0", "--yes", "--json")
    assert code == 3, out
    assert "paths differ" in as_json(out)["error"]["message"]
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "add", "demo@1.0.0", answer="n\n")
    assert code == 3, out
    assert f"run again with --allow-path {VAULT}" in out
    assert snapshot(inst) == before
    code, out, _ = cli(inst, "add", "demo@1.0.0", "--yes", "--allow-path", VAULT)
    assert code == 0, out
    assert (inst / "horae.lock").read_bytes() == before["horae.lock"]


# --- units -----------------------------------------------------------------------------------


def test_parse_grants_validates_and_normalises() -> None:
    grants = approval.parse_grants([], [], ["/b", "/a", "/a"], ["/a", "/c"])
    assert grants.paths.write == ["/a", "/b"]
    assert grants.paths.read == ["/c"]
    with pytest.raises(InvalidReference, match="--allow-read-path 'x'"):
        approval.parse_grants([], [], [], ["x"])
    assert not approval.parse_grants([], [])
    assert approval.recorded_grants(approval.parse_grants([], [])) is None


def test_dropped_grants_name_the_flag_that_keeps_them() -> None:
    existing = LockedPlugin.model_validate(
        {
            "version": "1.0.0",
            "repo": PLUGIN_URL,
            "commit": "a" * 40,
            "permissions": {
                "network": ["a.example.org"],
                "paths": {"read": ["/r", "/w"], "write": ["/w"]},
            },
        }
    )
    granted = Permissions.model_validate({"paths": {"read": ["/w"]}})
    dropped = approval.dropped_grants(existing, granted)
    assert dropped == [
        ("/r", "--allow-read-path"),
        ("/w", "--allow-path"),  # still readable, no longer writable
        ("a.example.org", "--allow-host"),
    ]
    assert approval.describe_dropped(dropped).startswith("--allow-read-path /r")


def test_a_record_without_paths_is_written_as_before() -> None:
    entry = LockedPlugin.model_validate(
        {
            "version": "1.0.0",
            "repo": PLUGIN_URL,
            "commit": "a" * 40,
            "permissions": {"network": ["a.example.org"]},
            "user_grants": {"network": ["a.example.org"]},
        }
    )
    assert entry.model_dump(mode="json")["user_grants"] == {"network": ["a.example.org"]}
