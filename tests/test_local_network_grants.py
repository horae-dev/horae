# SPDX-License-Identifier: Apache-2.0
"""Local-network grants in `horae.lock` (`permissions.network_local`, decision 66, #132)."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from horae.contracts import LocalEndpoint, Lockfile, PluginManifest
from horae.plugins.host import HostRegistry, PluginHost, RegistryError
from horae.plugins.host.session import NETWORK_LOCAL_META, PluginProcess
from horae.secrets import SecretResolver
from plugin_fixtures import COMMIT, PROBE_TOKEN, probe_manifest, secrets_dir

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

VALID = [
    "127.0.0.1:1143",  # Proton Mail Bridge
    "127.255.255.254:1",
    "[::1]:1143",
    "10.0.0.1:65535",
    "172.16.0.1:80",
    "172.31.255.255:80",
    "192.168.1.10:8123",  # Home Assistant by address
    "169.254.10.20:80",
    "[fd12:3456:789a::1]:443",
    "[fc00::]:80",
    "[fe80::1]:80",
    "[febf::1]:80",
    "homeassistant.local:8123",
    "ntfy.lan:80",
    "nas.home.arpa:5000",
    "ntfy.internal:8080",
    "a.b.c.lan:1",
    "127.0.0.1.lan:80",  # a name under `.lan`, not an IP literal
    "xn--bcher-kva.lan:80",
]
INVALID = [
    "127.0.0.1",  # no port
    "ntfy.lan",
    "[::1]",
    "127.0.0.1:0",
    "127.0.0.1:65536",
    "127.0.0.1:080",
    "127.0.0.1:",
    "8.8.8.8:53",  # public addresses
    "1.1.1.1:80",
    "172.32.0.1:80",
    "172.15.0.1:80",
    "192.169.0.1:80",
    "100.64.0.1:80",  # CGNAT is not in the list
    "0.0.0.0:80",
    "255.255.255.255:80",
    "[2001:db8::1]:80",
    "[2606:4700::1111]:443",
    "[::]:80",
    "[fec0::1]:80",  # site-local (deprecated), outside fe80::/10
    "127.000.000.001:80",  # non-canonical spellings
    "127.0.0.01:80",
    "2130706433:80",
    "0x7f.0.0.1:80",
    "[0:0:0:0:0:0:0:1]:80",
    "[::0001]:80",
    "[FE80::1]:80",
    "[fd12:3456:789a:0::1]:80",
    "[::ffff:127.0.0.1]:80",  # IPv4-mapped
    "[::ffff:7f00:1]:80",
    "[fe80::1%eth0]:80",  # zone id
    "::1:1143",  # unbracketed IPv6
    "169.254.169.254:80",  # cloud metadata
    "169.254.170.2:80",
    "[fd00:ec2::254]:80",
    "169.254.170.23:80",
    "[fd00:ec2::23]:80",
    "localhost:80",
    "foo.localhost:80",
    "lan:80",  # a bare suffix
    "local:80",
    "home.arpa:80",
    ".lan:80",
    "ntfy:80",  # single-label name
    "ntfy.example.com:80",  # public names
    "ntfy.lan.example.com:80",
    "127.0.0.1.nip.io:80",
    "ntfy.lan.:80",
    "NTFY.lan:80",
    "ntfy.LAN:80",
    "*.lan:80",  # wildcards
    "*:80",
    "ntfy.lan:*",
    "http://ntfy.lan:80",  # scheme, path, user info
    "ntfy.lan:80/",
    "user@ntfy.lan:80",
    "ntfy.lan:80 ",
    " ntfy.lan:80",
    "ntfy.lan:80\n",
    "xn--zz.lan:80",
    "bücher.lan:80",
    "",
]

_ADAPTER = TypeAdapter(LocalEndpoint)

METADATA_NAMES = [
    "169.254.169.254:80",  # the addresses too, EKS Pod Identity (169.254.170.23) included
    "169.254.170.2:80",
    "169.254.170.23:80",
    "[fd00:ec2::254]:80",
    "[fd00:ec2::23]:80",
    "metadata.google.internal:80",
    "metadata:80",
    "metadata.internal:80",
    "metadata.lan:80",
    "instance-data:80",
    "instance-data.internal:80",
    "instance-data.ec2.internal:80",
    "instance-data.eu-west-1.compute.internal:80",
]


@pytest.mark.parametrize("entry", METADATA_NAMES)
def test_a_cloud_metadata_name_is_refused(entry: str) -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(entry)


@pytest.mark.parametrize("schema", ["plugin-manifest/v1.json", "lockfile/v1.json"])
@pytest.mark.parametrize("entry", METADATA_NAMES)
def test_the_json_schema_refuses_a_cloud_metadata_name(schema: str, entry: str) -> None:
    pattern, metadata = _schema_pattern(schema)
    assert re.search(pattern, entry) is None or re.search(metadata, entry) is not None


def test_a_name_merely_containing_metadata_is_allowed() -> None:
    assert _ADAPTER.validate_python("my-metadata.lan:80") == "my-metadata.lan:80"


@pytest.mark.parametrize("entry", VALID)
def test_a_local_endpoint_is_accepted(entry: str) -> None:
    assert _ADAPTER.validate_python(entry) == entry


@pytest.mark.parametrize("entry", INVALID)
def test_anything_else_is_refused(entry: str) -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(entry)


def test_a_long_name_is_refused() -> None:
    name = ".".join(["a" * 63] * 4) + ".lan"
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(f"{name}:80")


def _schema_pattern(schema: str) -> tuple[str, str]:
    document = json.loads((SCHEMAS / schema).read_text(encoding="utf-8"))
    local = document["$defs"]["Permissions"]["properties"]["network_local"]["items"]
    return local["pattern"], local["not"]["pattern"]


@pytest.mark.parametrize("schema", ["plugin-manifest/v1.json", "lockfile/v1.json"])
@pytest.mark.parametrize("entry", VALID + INVALID)
def test_the_json_schema_never_accepts_what_the_model_refuses_by_shape(
    schema: str, entry: str
) -> None:
    """The JSON Schema checks the shape; the model adds the range and canonical-form checks.
    Every valid entry passes the schema; no public name, wildcard or port-less entry does."""
    pattern, metadata = _schema_pattern(schema)
    shaped = re.search(pattern, entry) is not None and re.search(metadata, entry) is None
    if entry in VALID:
        assert shaped
    elif ":" not in entry.lstrip("[").rpartition("]")[2] or "*" in entry or "example" in entry:
        assert not shaped


def _lock(network_local: list[str], network: list[str] | None = None) -> dict[str, Any]:
    permissions: dict[str, Any] = {"network": network or [], "network_local": network_local}
    return {
        "schema_version": 1,
        "plugins": {
            "imap": {
                "version": "0.1.0",
                "repo": "https://github.com/horae-dev/plugin-imap",
                "commit": COMMIT,
                "permissions": permissions,
            }
        },
    }


def test_a_hand_written_lock_takes_local_grants() -> None:
    text = """\
schema_version: 1
plugins:
  imap:
    version: 0.1.0
    repo: https://github.com/horae-dev/plugin-imap
    commit: "0000000000000000000000000000000000000000"
    permissions:
      network: [imap.example.org]
      network_local: ["127.0.0.1:1143", "[::1]:1143", "homeassistant.local:8123"]
      secrets: [IMAP_PASSWORD]
"""
    lock = Lockfile.model_validate(yaml.safe_load(text))
    permissions = lock.plugins["imap"].permissions
    assert permissions.network == ["imap.example.org"]
    assert permissions.network_local == ["127.0.0.1:1143", "[::1]:1143", "homeassistant.local:8123"]


@pytest.mark.parametrize("entry", ["8.8.8.8:80", "*.lan:80", "ntfy.example.org:80", "ntfy.lan"])
def test_a_lock_cannot_grant_a_public_host_or_a_wildcard_as_local(entry: str) -> None:
    with pytest.raises(ValidationError):
        Lockfile.model_validate(_lock([entry]))


def test_a_local_endpoint_is_not_a_network_host() -> None:
    """`network` stays public names only: no IP literal, no port, so no plain-http route."""
    for entry in ("127.0.0.1", "127.0.0.1:1143", "[::1]"):
        with pytest.raises(ValidationError):
            Lockfile.model_validate(_lock([], [entry]))


def test_duplicates_are_refused() -> None:
    with pytest.raises(ValidationError):
        Lockfile.model_validate(_lock(["127.0.0.1:1143", "127.0.0.1:1143"]))


def test_the_lock_is_written_sorted_and_without_an_empty_local_list() -> None:
    lock = Lockfile.model_validate(_lock(["ntfy.lan:80", "127.0.0.1:1143"]))
    written = lock.model_dump(mode="json")["plugins"]["imap"]["permissions"]
    assert written["network_local"] == ["127.0.0.1:1143", "ntfy.lan:80"]
    bare = Lockfile.model_validate(_lock([])).model_dump(mode="json")
    assert "network_local" not in bare["plugins"]["imap"]["permissions"]
    assert Lockfile.model_validate(bare) == Lockfile.model_validate(_lock([]))


# --- the plugin host ---------------------------------------------------------------------------


def _host(root: Path, manifest: dict[str, Any], locked: list[str]) -> PluginHost:
    permissions = {**manifest["permissions"], "network_local": locked}
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


def test_the_meta_carries_the_locked_local_endpoints(tmp_path: Path) -> None:
    manifest = probe_manifest()
    manifest["permissions"] = {**manifest["permissions"], "network_local": ["127.0.0.1:1143"]}
    data = _echo(tmp_path, manifest, ["127.0.0.1:1143", "[::1]:1143"])
    assert data["meta_network_local"] == ["127.0.0.1:1143", "[::1]:1143"]
    assert data["meta_network"] == []


def test_no_local_grant_gives_an_empty_list(tmp_path: Path) -> None:
    assert _echo(tmp_path, probe_manifest(), [])["meta_network_local"] == []


def test_a_manifest_asking_for_an_unapproved_local_endpoint_is_refused(tmp_path: Path) -> None:
    manifest = probe_manifest()
    manifest["permissions"] = {**manifest["permissions"], "network_local": ["192.168.1.10:8123"]}
    with pytest.raises(RegistryError, match="network_local"):
        _host(tmp_path, manifest, ["192.168.1.10:80"])


def test_a_plugin_cannot_influence_the_local_meta(tmp_path: Path) -> None:
    hostile = {NETWORK_LOCAL_META: ["192.168.1.1:22"], "horae": {"network_local": ["x"]}}
    data = _echo(tmp_path, probe_manifest(), ["127.0.0.1:1143"], input=hostile)
    assert data["meta_network_local"] == ["127.0.0.1:1143"]
    assert data["input"] == hostile


def test_the_default_of_a_call_is_an_empty_local_list() -> None:
    import inspect

    default = inspect.signature(PluginProcess.call).parameters["network_local"].default
    assert list(default) == []
