# SPDX-License-Identifier: Apache-2.0
"""Local-network grants reach plugin code and match exactly (decision 66, #132)."""

import asyncio
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

import horae_sdk
from horae.contracts import LocalEndpoint
from horae_sdk import Applied, ApplyRequest, Call, Network, Notifier, Paths, Permissions, Plugin
from horae_sdk.manifest import ManifestError
from horae_sdk.protocol import NETWORK_LOCAL_META, local_endpoint
from horae_sdk.testing import call as replay_call
from horae_sdk.testing import operation

HOSTS = ("ntfy.example.org",)
LOCAL = (
    "127.0.0.1:1143",
    "[::1]:1143",
    "192.168.1.10:8123",
    "homeassistant.local:8123",
    "ntfy.lan:80",
    "nas.home.arpa:443",
    "[fd12::1]:8080",
)
NETWORK = Network(HOSTS, LOCAL)

ALLOWED = [
    "http://127.0.0.1:1143",
    "http://127.0.0.1:1143/",
    "https://127.0.0.1:1143/x",
    "http://[::1]:1143/",  # IPv6 loopback
    "http://192.168.1.10:8123/api/states",  # RFC 1918
    "https://192.168.1.10:8123/?a=b",
    "http://homeassistant.local:8123/api",
    "http://ntfy.lan/topic",  # no port: 80 for http
    "http://ntfy.lan:80/topic",
    "https://nas.home.arpa/",  # no port: 443 for https
    "http://[fd12::1]:8080/",
    "https://ntfy.example.org/",  # a public host keeps its decision 63 rules
    "https://ntfy.example.org:8443/",
    "http://127.0.0.1:1143/@evil.test",  # `@` in the path is the path
]
REFUSED = [
    # port mismatch
    "http://127.0.0.1/",
    "http://127.0.0.1:1144/",
    "https://127.0.0.1/",
    "http://[::1]/",
    "http://192.168.1.10/",
    "http://homeassistant.local/",
    "https://ntfy.lan/",  # https means 443, the grant is 80
    "http://ntfy.lan:8080/",
    "http://nas.home.arpa/",
    # another host or spelling of the same address
    "http://127.0.0.2:1143/",
    "http://localhost:1143/",
    "http://127.000.000.001:1143/",
    "http://2130706433:1143/",
    "http://0x7f000001:1143/",
    "http://[0:0:0:0:0:0:0:1]:1143/",
    "http://[::ffff:127.0.0.1]:1143/",
    "http://[::1%25lo0]:1143/",
    "http://[fd12:0::1]:8080/",
    "http://HOMEASSISTANT.local:8123/",
    "http://homeassistant.local.:8123/",
    "http://192.168.1.100:8123/",
    # DNS-rebinding style: a public name never matches a local grant
    "http://127.0.0.1.nip.io:1143/",
    "http://ntfy.lan.evil.test/",
    "http://homeassistant.local.evil.test:8123/",
    "http://192.168.1.10.sslip.io:8123/",
    "http://evil.test/ntfy.lan",
    "http://evil.test/?http://ntfy.lan",
    # user info tricks
    "http://user@127.0.0.1:1143/",
    "http://127.0.0.1:1143@evil.test/",
    "http://evil.test@127.0.0.1:1143/",
    "http://127.0.0.1:1143\\@evil.test/",
    "http://ntfy.lan:80@evil.test/",
    "http://ntfy.lan#@evil.test/",
    "http://[::1]:1143@evil.test/",
    # scheme rules
    "ftp://127.0.0.1:1143/",
    "imap://127.0.0.1:1143/",
    "HTTP://127.0.0.1:1143/",
    "ws://127.0.0.1:1143/",
    "//127.0.0.1:1143/",
    "127.0.0.1:1143",
    "http://ntfy.example.org/",  # plain http to a public host: never
    "http:/127.0.0.1:1143/",
    # malformed
    "http://127.0.0.1:1143 /",
    "http://127.0.0.1:1143/\n",
    "http://127.0.0.1:01143/",
    "http://127.0.0.1:0/",
    "http://127.0.0.1:99999/",
    "http://127.0.0.1:1143/#x",
    "",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_an_approved_local_endpoint_is_allowed(url: str) -> None:
    assert NETWORK.allows(url) is True


@pytest.mark.parametrize("url", REFUSED)
def test_anything_else_is_refused(url: str) -> None:
    assert NETWORK.allows(url) is False


def test_semantics_true_false_none() -> None:
    assert Network.from_wire(None, None).allows("http://127.0.0.1:1143/") is None
    assert Network.from_wire([], None).allows("http://127.0.0.1:1143/") is False
    assert Network.from_wire([], []).allows("http://127.0.0.1:1143/") is False
    assert Network.from_wire([], ["127.0.0.1:1143"]).allows("http://127.0.0.1:1143/") is True


def test_a_core_with_decision_63_only_grants_no_local_endpoint() -> None:
    network = Network.from_wire(["ntfy.example.org"])
    assert network.known
    assert network.local == ()
    assert network.allows("http://127.0.0.1:1143/") is False
    assert network.allows("https://ntfy.example.org/") is True


def test_a_malformed_local_list_fails_closed() -> None:
    for value in ("127.0.0.1:1143", {"127.0.0.1:1143": 1}, 3, True):
        network = Network.from_wire([], value)
        assert network.known
        assert network.local == ()
        assert network.allows("http://127.0.0.1:1143/") is False
    assert Network.from_wire(None, "x").hosts == ()  # no network list but a local key: known


@pytest.mark.parametrize(
    "entry",
    ["8.8.8.8:80", "*.lan:80", "ntfy.example.org:80", "ntfy.lan", "127.0.0.1", "localhost:80"],
)
def test_a_public_or_wildcard_entry_never_becomes_a_local_grant(entry: str) -> None:
    network = Network.from_wire([], [entry])
    assert network.local == ()
    host = entry.rpartition(":")[0] or entry
    assert network.allows(f"http://{host}:80/") is False
    assert network.allows(f"http://{entry}/") is False


def test_a_public_host_entry_does_not_allow_plain_http() -> None:
    network = Network.from_wire(["ntfy.example.org"], [])
    assert network.allows("http://ntfy.example.org/") is False
    assert network.allows("http://ntfy.example.org:443/") is False


def test_address_allowed_for_other_protocols() -> None:
    assert NETWORK.address_allowed("127.0.0.1", 1143) is True
    assert NETWORK.address_allowed("::1", 1143) is True  # brackets optional
    assert NETWORK.address_allowed("[::1]", 1143) is True
    assert NETWORK.address_allowed("127.0.0.1", 993) is False
    assert NETWORK.address_allowed("ntfy.example.org", 993) is True  # any port on a host
    assert NETWORK.address_allowed("evil.test", 993) is False
    assert NETWORK.address_allowed("127.0.0.1", True) is False
    assert NETWORK.address_allowed("127.0.0.1", "1143") is False
    assert NETWORK.address_allowed("127.0.0.1", 0) is False
    assert NETWORK.address_allowed(None, 1143) is False
    assert Network().address_allowed("127.0.0.1", 1143) is None


# --- the same rules as the core's `LocalEndpoint` ------------------------------------------------

_CORE = TypeAdapter(LocalEndpoint)
CANDIDATES = [
    *LOCAL,
    "127.255.255.254:1",
    "10.0.0.1:65535",
    "172.16.0.1:80",
    "172.31.255.255:80",
    "169.254.10.20:80",
    "[fe80::1]:80",
    "[febf::1]:80",
    "[fc00::]:80",
    "127.0.0.1.lan:80",
    "xn--bcher-kva.lan:80",
    "xn--zz.lan:80",
    "xn--ls8h.lan:80",
    "8.8.8.8:53",
    "172.32.0.1:80",
    "100.64.0.1:80",
    "[2001:db8::1]:80",
    "[::]:80",
    "[fec0::1]:80",
    "127.0.0.01:80",
    "[::0001]:80",
    "[FE80::1]:80",
    "[::ffff:127.0.0.1]:80",
    "[fe80::1%eth0]:80",
    "169.254.169.254:80",
    "169.254.170.2:80",
    "[fd00:ec2::254]:80",
    "localhost:80",
    "lan:80",
    "ntfy:80",
    "ntfy.lan.:80",
    "NTFY.lan:80",
    "*.lan:80",
    "ntfy.lan:0",
    "ntfy.lan:65536",
    "ntfy.lan:08",
    "ntfy.lan",
    "",
    "metadata.google.internal:80",
    "metadata:80",
    "metadata.internal:80",
    "instance-data:80",
    "instance-data.internal:80",
    "instance-data.ec2.internal:80",
    "my-metadata.lan:80",
]


@pytest.mark.parametrize(
    "entry",
    [
        "metadata.google.internal:80",
        "metadata:80",
        "metadata.internal:80",
        "instance-data:80",
        "instance-data.internal:80",
        "instance-data.ec2.internal:80",
    ],
)
def test_a_cloud_metadata_name_is_never_a_local_grant(entry: str) -> None:
    assert local_endpoint(entry) is None
    network = Network.from_wire([], [entry])
    assert network.local == ()
    assert network.allows(f"http://{entry}/") is False


def _core_accepts(entry: str) -> bool:
    try:
        _CORE.validate_python(entry)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("entry", CANDIDATES)
def test_the_sdk_reads_local_endpoints_as_the_core_does(entry: str) -> None:
    assert (local_endpoint(entry) is not None) == _core_accepts(entry)


# --- reaching plugin code ------------------------------------------------------------------------


def probe() -> Plugin:
    plugin = Plugin("probe", permissions=Permissions(intents=["notify.push"]))

    @plugin.source("probe.local")
    def local(call: Call, url: str = "http://127.0.0.1:1143/") -> dict[str, Any]:
        endpoints = call.local_endpoints
        return {
            "local": None if endpoints is None else list(endpoints),
            "allowed": call.host_allowed(url),
            "address": call.address_allowed("127.0.0.1", 1143),
        }

    return plugin


def test_a_call_carries_the_locked_local_endpoints() -> None:
    got = asyncio.run(
        replay_call(probe(), "probe.local", network=[], network_local=["127.0.0.1:1143"])
    ).data
    assert got == {"local": ["127.0.0.1:1143"], "allowed": True, "address": True}


def test_the_harness_sends_an_empty_local_list_with_the_network_list() -> None:
    got = asyncio.run(replay_call(probe(), "probe.local")).data
    assert got == {"local": [], "allowed": False, "address": False}


def test_an_old_core_gives_unknown() -> None:
    got = asyncio.run(replay_call(probe(), "probe.local", network=None)).data
    assert got == {"local": None, "allowed": None, "address": None}


def test_a_plugin_cannot_influence_the_local_list() -> None:
    hostile = {NETWORK_LOCAL_META: ["127.0.0.1:1143"]}
    got = asyncio.run(replay_call(probe(), "probe.local", input=hostile)).data
    assert got == {"local": [], "allowed": False, "address": False}


class Ntfy(Notifier):
    def __init__(self) -> None:
        super().__init__(["ntfy.push"])

    def identity(self, paths: Paths) -> str:
        return "ntfy"

    def apply(self, request: ApplyRequest) -> Applied:
        url = request.params["server"]
        return Applied("x", details={"allowed": request.host_allowed(url)})


def test_a_sink_operation_gets_the_local_list_too() -> None:
    plugin = Plugin("ntfy", permissions=Permissions(intents=["notify.push"]))
    plugin.add(Ntfy())
    args = {"paths": {"read": [], "write": []}, "intent": {"type": "notify.push", "body": "x"}}

    def apply(server: str, local: list[str] | None, network: list[str] | None = None) -> Any:
        arguments = {**args, "params": {"server": server}}
        result = operation(plugin, "ntfy.apply", arguments, network=network, network_local=local)
        return asyncio.run(result)["details"]["allowed"]

    assert apply("http://ntfy.lan/", ["ntfy.lan:80"], []) is True
    assert apply("http://ntfy.lan/", [], []) is False
    assert apply("http://ntfy.lan:8080/", ["ntfy.lan:80"], []) is False
    assert apply("http://ntfy.lan/", None, None) is None


# --- the manifest helper --------------------------------------------------------------------------


def test_the_manifest_writes_local_endpoints() -> None:
    permissions = Permissions(network_local=["127.0.0.1:1143", "[::1]:1143"])
    permissions.check()
    assert permissions.to_dict() == {"network_local": ["127.0.0.1:1143", "[::1]:1143"]}


@pytest.mark.parametrize("entry", ["8.8.8.8:80", "127.0.0.1", "*.lan:80", "ntfy.example.org:80"])
def test_the_manifest_refuses_a_bad_local_endpoint(entry: str) -> None:
    with pytest.raises(ManifestError, match="network_local"):
        Permissions(network_local=[entry]).check()


def test_the_version_is_0_6_0() -> None:
    assert horae_sdk.__version__ == "0.6.0"
