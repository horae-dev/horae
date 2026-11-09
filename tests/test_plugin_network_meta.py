# SPDX-License-Identifier: Apache-2.0
"""The locked `permissions.network` travels in every call's `_meta["horae/network"]` (#131)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import Lockfile, PluginManifest
from horae.plugins.host import HostRegistry, PluginHost, RegistryError
from horae.plugins.host.session import NETWORK_META, PluginProcess
from horae.secrets import SecretResolver
from plugin_fixtures import COMMIT, PROBE_TOKEN, probe_manifest, secrets_dir


def _host(root: Path, manifest: dict[str, Any], locked_network: list[str]) -> PluginHost:
    """A host whose lock approves `locked_network` (a superset of the manifest's)."""
    permissions = {**manifest["permissions"], "network": locked_network}
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                manifest["name"]: {
                    "version": manifest["version"],
                    "repo": "https://example.org/plugin-probe",
                    "commit": COMMIT,
                    "permissions": permissions,
                }
            },
        }
    )
    registry = HostRegistry(lock, {manifest["name"]: PluginManifest.model_validate(manifest)})
    resolver = SecretResolver({}, secrets_dir(root, PROBE_TOKEN=PROBE_TOKEN))
    return PluginHost(registry, secrets=resolver, work_dir=root / "work")


def _echo(root: Path, manifest: dict[str, Any], locked: list[str], **kw: Any) -> Any:
    async def go() -> Any:
        async with _host(root, manifest, locked) as host:
            return (await host.call("probe.echo", {}, kw.get("input"))).data

    return asyncio.run(go())


def test_the_meta_carries_the_locked_list_not_the_manifests(tmp_path: Path) -> None:
    manifest = probe_manifest()
    manifest["permissions"] = {**manifest["permissions"], "network": ["a.example.org"]}
    data = _echo(tmp_path, manifest, ["a.example.org", "b.example.org"])
    assert data["meta_network"] == ["a.example.org", "b.example.org"]


def test_a_manifest_asking_for_more_than_the_lock_is_refused(tmp_path: Path) -> None:
    manifest = probe_manifest()
    manifest["permissions"] = {**manifest["permissions"], "network": ["evil.example.org"]}
    with pytest.raises(RegistryError):
        _host(tmp_path, manifest, ["a.example.org"])


def test_a_lock_granting_no_host_gives_an_empty_list(tmp_path: Path) -> None:
    assert _echo(tmp_path, probe_manifest(), [])["meta_network"] == []


def test_a_plugin_without_a_lock_entry_is_not_started(tmp_path: Path) -> None:
    manifest = probe_manifest()
    lock = Lockfile.model_validate({"schema_version": 1, "plugins": {}})
    with pytest.raises(RegistryError):
        HostRegistry(lock, {"probe": PluginManifest.model_validate(manifest)})


def test_the_default_of_a_call_is_an_empty_list_never_everything() -> None:
    import inspect

    default = inspect.signature(PluginProcess.call).parameters["network"].default
    assert list(default) == []


def test_a_plugin_cannot_influence_the_meta(tmp_path: Path) -> None:
    """A transform's input (untrusted data) and parameters travel under other keys."""
    manifest = probe_manifest()
    hostile = {NETWORK_META: ["evil.example.org"], "horae": {"network": ["evil.example.org"]}}
    data = _echo(tmp_path, manifest, ["a.example.org"], input=hostile)
    assert data["meta_network"] == ["a.example.org"]
    assert data["input"] == hostile
