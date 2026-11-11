# SPDX-License-Identifier: Apache-2.0
"""Plugins that are not first-party run only in a container (#191, schemas decision 79).

The install gate is tested with `add` (test_plugin_approval.py, test_plugin_direct.py) and
`upgrade` (test_plugin_upgrade.py, test_plugin_direct.py). Here: the plugin host's own gate,
which learns the tier from `horae.lock` (and an install record that says less), never from
the manifest, and refuses the process runtime even when horae.yaml changed after the install;
and `horae plugin audit`'s drift. Local repositories and synthetic hosts only."""

import asyncio
import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from horae.config import load_instance
from horae.contracts.instance import PluginDefaults
from horae.contracts.lockfile import Lockfile
from horae.contracts.plugin import PluginManifest
from horae.marketplace import approval
from horae.marketplace.install import INSTALL_RECORD
from horae.plugins.host import HostRegistry, PluginCallError, PluginHost
from horae.plugins.host.container import ContainerRuntime
from horae.secrets import SecretResolver
from marketplace_fixtures import A, set_runtime
from plugin_fixtures import lock_for
from upgrade_fixtures import (
    Market,
    add,
    cli,
    inst,  # noqa: F401
    lock,
    market,  # noqa: F401
)

DEMO: dict[str, Any] = {
    "name": "demo",
    "version": "1.0.0",
    "horae": ">=0",
    "provides": {"sources": ["demo.fetch"]},
    "permissions": {},
    "runtime": {"protocol": "mcp-stdio", "command": ["python3", "server.py"]},
}
COMMUNITY = {"tier": "community", "index": "https://git.example.test/someone/marketplace"}


def _lock(**origin: str) -> Lockfile:
    entry = lock_for(DEMO).plugins["demo"].model_dump(mode="json")
    return Lockfile.model_validate({"schema_version": 1, "plugins": {"demo": entry | origin}})


def _registry(tmp_path: Path, lock: Lockfile, record: dict[str, Any] | None = None) -> HostRegistry:
    root = tmp_path / "plugins" / "demo"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(json.dumps(DEMO), encoding="utf-8")
    if record is not None:
        entry = lock.plugins["demo"]
        base = {"name": "demo", "version": entry.version, "repo": entry.repo}
        (root / INSTALL_RECORD).write_text(
            json.dumps({**base, "commit": entry.commit, **record}), encoding="utf-8"
        )
    return HostRegistry.from_directory(lock, tmp_path / "plugins")


# --- where the host learns the tier ------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "record", "first_party"),
    [
        ({}, None, True),  # a built-in of `horae init`, or a first-party lock entry
        ({}, {"source": "marketplace", "tier": "first-party", "index": A}, False),  # not trusted
        ({}, {"source": "marketplace"}, True),  # a record written before it held the tier
        ({}, {"source": "direct"}, False),  # the lock lost `source: direct`: the record says
        ({}, {"source": "marketplace", **COMMUNITY}, False),
        ({"source": "direct"}, None, False),
        (COMMUNITY, None, False),
        (COMMUNITY, {"source": "marketplace", "tier": "first-party", "index": A}, False),
    ],
)
def test_the_host_learns_the_tier_from_the_lock_and_a_record_only_takes_it_away(
    tmp_path: Path,
    origin: dict[str, str],
    record: dict[str, Any] | None,
    first_party: bool,
) -> None:
    """The second case: `A` is not the default marketplace here, so its `first-party` claim
    is not believed. The manifest is never asked (it carries no tier)."""
    registry = _registry(tmp_path, _lock(**origin), record)
    assert registry.installed("demo").first_party is first_party


def test_a_record_of_the_default_marketplace_keeps_first_party(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval, "FIRST_PARTY_INDEXES", frozenset({A}))
    record = {"source": "marketplace", "tier": "first-party", "index": A}
    assert _registry(tmp_path, _lock(), record).installed("demo").first_party is True


def test_a_registry_built_without_a_lock_entry_marker_is_first_party_only_from_the_lock() -> None:
    manifests = {"demo": PluginManifest.model_validate(DEMO)}
    assert HostRegistry(_lock(), manifests).installed("demo").first_party is True
    direct = HostRegistry(_lock(source="direct"), manifests)
    assert direct.installed("demo").first_party is False


# --- the run-time gate ---------------------------------------------------------------------------


def _call(tmp_path: Path, registry: HostRegistry, runtime: str) -> PluginCallError:
    defaults = PluginDefaults.model_validate({"runtime": runtime})

    async def go() -> PluginCallError:
        async with PluginHost(
            registry,
            secrets=SecretResolver({}, tmp_path / "secrets"),
            work_dir=tmp_path / "work",
            timeouts=lambda _name: defaults,
        ) as host:
            with pytest.raises(PluginCallError) as caught:
                await host.call("demo.fetch", {}, None)
            return caught.value

    return asyncio.run(go())


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records any attempt to start a process or a container (each fails)."""
    started: list[object] = []

    async def no_process(*args: object, **kwargs: object) -> None:
        started.append(("process", args))
        raise AssertionError("no process may be spawned")

    async def no_container(self: ContainerRuntime, name: str, **kwargs: object) -> None:
        started.append(("container", name))
        raise PluginCallError("runtime_unavailable", "no engine in this test")

    monkeypatch.setattr(anyio, "open_process", no_process)
    monkeypatch.setattr(ContainerRuntime, "spawn", no_container)
    return started


@pytest.mark.parametrize("origin", [{"source": "direct"}, COMMUNITY], ids=["direct", "community"])
def test_a_plugin_that_is_not_first_party_is_refused_under_the_process_runtime(
    tmp_path: Path, no_spawn: list[object], origin: dict[str, str]
) -> None:
    error = _call(tmp_path, _registry(tmp_path, _lock(**origin)), "process")
    assert error.kind == "runtime_refused"
    assert "plugins.demo.runtime: container" in error.message
    assert no_spawn == []


def test_under_the_container_runtime_the_gate_lets_it_through(
    tmp_path: Path, no_spawn: list[object]
) -> None:
    error = _call(tmp_path, _registry(tmp_path, _lock(source="direct")), "container")
    assert error.kind == "runtime_unavailable"  # the container runtime was asked, not refused
    assert no_spawn == [("container", "demo")]


def test_a_first_party_plugin_is_not_refused_under_the_process_runtime(
    tmp_path: Path, no_spawn: list[object]
) -> None:
    error = _call(tmp_path, _registry(tmp_path, _lock()), "process")
    assert error.kind != "runtime_refused"
    assert [started[0] for started in no_spawn] == ["process"]  # type: ignore[index]


def test_after_install_horae_yaml_switched_to_process_never_starts_it(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
    no_spawn: list[object],
) -> None:
    """Installed under `runtime: container` from a community index; then horae.yaml is changed
    to the process runtime. The lock still says community: the host refuses it."""
    market.tier = "community"
    market.publish()
    set_runtime(inst, "container", "demo")
    add(inst)
    assert lock(inst)["plugins"]["demo"]["tier"] == "community"
    set_runtime(inst, "process", "demo")
    instance = load_instance(inst)
    registry = HostRegistry.from_directory(instance.lock, inst / "plugins")

    async def go() -> PluginCallError:
        async with PluginHost(
            registry,
            secrets=SecretResolver({}, inst / "secrets"),
            work_dir=inst / "state" / "work",
            timeouts=instance.config.plugin_timeouts,
        ) as host:
            with pytest.raises(PluginCallError) as caught:
                await host.call("demo.fetch", {}, None)
            return caught.value

    error = asyncio.run(go())
    assert error.kind == "runtime_refused"
    assert no_spawn == []


# --- audit -------------------------------------------------------------------------------------


def _audit(instance: Path) -> tuple[int, dict[str, Any]]:
    code, out, _ = cli(instance, "audit", "--json")
    report: dict[str, Any] = json.loads(out)
    return code, report


def test_audit_flags_a_plugin_that_is_not_first_party_outside_a_container(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    market.tier = "community"
    market.publish()
    set_runtime(inst, "container", "demo")
    add(inst)
    code, report = _audit(inst)
    assert code == 0, report
    (row,) = report["plugins"]
    assert (row["first_party"], row["runtime"], row["tier"]) == (False, "container", "community")
    set_runtime(inst, "process", "demo")
    code, report = _audit(inst)
    assert code == 4, report
    (row,) = report["plugins"]
    assert [d["kind"] for d in row["drift"]] == ["runtime_not_container"]
    assert "plugins.demo.runtime: container" in row["drift"][0]["message"]


def test_audit_does_not_flag_a_first_party_plugin_under_the_process_runtime(
    inst: Path,  # noqa: F811
    market: Market,  # noqa: F811
) -> None:
    add(inst)
    code, report = _audit(inst)
    assert code == 0, report
    (row,) = report["plugins"]
    assert (row["first_party"], row["runtime"]) == (True, "process")
