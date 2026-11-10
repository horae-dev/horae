# SPDX-License-Identifier: Apache-2.0
"""The locked network list reaches plugin code, with the lock's host semantics (#131)."""

import asyncio
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from horae.contracts.types import Hostname, HttpsUrl
from horae.contracts.types import url_host as core_url_host
from horae_sdk import Applied, ApplyRequest, Call, Network, Notifier, Paths, Permissions, Plugin
from horae_sdk.protocol import NETWORK_META, url_host
from horae_sdk.testing import call as replay_call
from horae_sdk.testing import operation

HOSTS = ["api.example.org", "ntfy.example.org", "xn--bcher-kva.example", "coach"]

ALLOWED = [
    "https://api.example.org",
    "https://api.example.org/",
    "https://api.example.org/v1/x?y=1&z=%20",
    "https://api.example.org:8443/v1",  # any port on an approved host (decision 10)
    "https://api.example.org:1",
    "https://api.example.org:65535/",
    "https://api.example.org?q=1",
    "https://xn--bcher-kva.example/",  # an IDN is written as its A-label
    "https://coach/hook",  # a single-label (Docker service) name
]
REFUSED = [
    "http://api.example.org/",  # not https
    "HTTPS://api.example.org/",
    "//api.example.org/",
    "api.example.org",
    "https://API.example.org/",  # no case folding: the lock lists lowercase names
    "https://api.example.org./",  # trailing dot
    "https://api.example.org.evil.test/",
    "https://evil.test/api.example.org",
    "https://evil.test?api.example.org",
    "https://sub.api.example.org/",  # no implicit subdomains
    "https://*.example.org/",  # no wildcard
    "https://example.org/",
    "https://bücher.example/",  # a U-label is not the listed A-label
    "https://api.example.org@evil.test/",  # user info tricks
    "https://user@api.example.org/",
    "https://user:pw@api.example.org/",
    "https://evil.test@api.example.org/",
    "https://api.example.org:80@evil.test/",
    "https://api.example.org/x#frag",  # the contract has no fragment
    "https://evil.test#@api.example.org/",
    "https://api.example.org#",
    "https://api.example.org\\@evil.test/",
    "https://api.example.org\\.evil.test/",
    "https://evil.test\\@api.example.org/",
    "https://api.example.org:0/",
    "https://api.example.org:65536/",
    "https://api.example.org:08080/",
    "https://api.example.org:/",
    "https://api.example.org:80:80/",
    "https://api.example.org /",
    "https://api.example.org\n/",
    "https://api.example.org\x00.evil.test/",
    "https://\tapi.example.org/",
    "https://127.0.0.1/",  # IP literals
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]:443/",
    "https://2130706433/",
    "https://0x7f.0.0.1/",
    "https://localhost/",
    "https:///api.example.org",
    "https://",
    "",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_an_approved_host_is_allowed(url: str) -> None:
    assert Network(tuple(HOSTS)).allows(url) is True


@pytest.mark.parametrize("url", REFUSED)
def test_anything_else_is_refused(url: str) -> None:
    assert Network(tuple(HOSTS)).allows(url) is False


@pytest.mark.parametrize("url", [None, 3, b"https://api.example.org/", ["https://api.example.org"]])
def test_a_non_string_is_refused(url: Any) -> None:
    assert Network(tuple(HOSTS)).allows(url) is False


def test_an_empty_list_allows_nothing() -> None:
    assert Network(()).allows("https://api.example.org/") is False


def test_a_wildcard_entry_is_a_literal_that_matches_no_host() -> None:
    network = Network.from_wire(["*.example.org", "api.example.org:443", ""])
    assert network.allows("https://a.example.org/") is False
    assert network.allows("https://*.example.org/") is False
    assert network.allows("https://api.example.org/") is False


def test_no_list_means_unknown_not_refused() -> None:
    network = Network.from_wire(None)
    assert network.known is False
    assert network.hosts is None
    assert network.allows("https://api.example.org/") is None
    assert network.allows("not a url") is None  # unknown whatever is asked


def test_a_malformed_list_fails_closed() -> None:
    for value in ("api.example.org", {"api.example.org": 1}, 3, True):
        network = Network.from_wire(value)
        assert network.known
        assert network.hosts == ()
        assert network.allows("https://api.example.org/") is False
    assert Network.from_wire([3, None, "api.example.org"]).hosts == ("api.example.org",)


# --- the same rules as the core's contracts ---------------------------------------------------

_CORE_URL = TypeAdapter(HttpsUrl)
_CORE_HOST = TypeAdapter(Hostname)


def _core_accepts(url: str) -> bool:
    try:
        _CORE_URL.validate_python(url)
    except ValidationError:
        return False
    return True


def _core_hostname(host: str) -> bool:
    try:
        _CORE_HOST.validate_python(host)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("url", ALLOWED + REFUSED)
def test_the_sdk_never_allows_what_the_core_contract_refuses(url: str) -> None:
    """A host the lock could list (a valid `Hostname`) is only read from a URL the core's
    `HttpsUrl` accepts, and it is the host the core reads."""
    host = url_host(url)
    if host is not None and _core_hostname(host):
        assert _core_accepts(url), url
        assert core_url_host(url) == host


@pytest.mark.parametrize("url", ALLOWED)
def test_the_sdk_reads_the_host_as_the_core_does(url: str) -> None:
    assert _core_accepts(url)
    assert url_host(url) == core_url_host(url)


# --- reaching plugin code ----------------------------------------------------------------------


def probe() -> Plugin:
    plugin = Plugin("probe", permissions=Permissions(intents=["notify.push"]))

    @plugin.source("probe.hosts")
    def hosts(call: Call, url: str = "https://api.example.org/") -> dict[str, Any]:
        return {
            "hosts": None if call.allowed_hosts is None else list(call.allowed_hosts),
            "allowed": call.host_allowed(url),
            "input": call.input,
        }

    return plugin


def test_a_call_carries_the_locked_hosts() -> None:
    plugin = probe()
    got = asyncio.run(replay_call(plugin, "probe.hosts", network=["api.example.org"])).data
    assert got == {"hosts": ["api.example.org"], "allowed": True, "input": None}
    other = asyncio.run(
        replay_call(plugin, "probe.hosts", {"url": "https://evil.test/"}, network=["a.example.org"])
    ).data
    assert other["allowed"] is False


def test_the_testing_harness_sends_an_empty_list_by_default_like_the_core() -> None:
    got = asyncio.run(replay_call(probe(), "probe.hosts")).data
    assert got["hosts"] == []
    assert got["allowed"] is False


def test_an_old_core_with_no_meta_gives_unknown() -> None:
    got = asyncio.run(replay_call(probe(), "probe.hosts", network=None)).data
    assert got == {"hosts": None, "allowed": None, "input": None}


def test_a_plugin_cannot_influence_the_list() -> None:
    """Input and parameters travel under other keys than `_meta["horae/network"]`."""
    hostile = {NETWORK_META: ["evil.test"], "horae": {"network": ["evil.test"]}}
    got = asyncio.run(
        replay_call(
            probe(),
            "probe.hosts",
            {"url": "https://evil.test/"},
            input=hostile,
            network=["api.example.org"],
        )
    ).data
    assert got["allowed"] is False
    assert got["hosts"] == ["api.example.org"]
    assert got["input"] == hostile
    # without a list from the core, hostile input does not create one either
    old = asyncio.run(replay_call(probe(), "probe.hosts", input=hostile, network=None)).data
    assert old["hosts"] is None
    assert old["allowed"] is None


class Ntfy(Notifier):
    def __init__(self) -> None:
        super().__init__(["ntfy.push"])
        self.seen: list[ApplyRequest] = []

    def identity(self, paths: Paths) -> str:
        return "ntfy"

    def apply(self, request: ApplyRequest) -> Applied:
        self.seen.append(request)
        url = request.params.get("server", "https://ntfy.example.org/")
        return Applied("x", details={"allowed": request.host_allowed(url)})


def test_a_sink_operation_gets_the_list_too() -> None:
    plugin = Plugin("ntfy", permissions=Permissions(intents=["notify.push"]))
    sink = Ntfy()
    plugin.add(sink)
    args = {"paths": {"read": [], "write": []}, "intent": {"type": "notify.push", "body": "x"}}

    def apply(network: list[str] | None, server: str = "https://ntfy.example.org/") -> Any:
        arguments = {**args, "params": {"server": server}}
        return asyncio.run(operation(plugin, "ntfy.apply", arguments, network=network))

    assert apply(["ntfy.example.org"])["details"] == {"allowed": True}
    assert apply(["ntfy.example.org"], "https://evil.test/")["details"] == {"allowed": False}
    assert apply([])["details"] == {"allowed": False}
    assert apply(None)["details"] == {"allowed": None}
    assert sink.seen[-1].network.known is False


def test_the_egress_proxy_is_read_from_the_environment() -> None:
    from horae_sdk import egress_proxy

    assert egress_proxy({}) is None
    assert egress_proxy({"HORAE_EGRESS_PROXY": "http://192.168.155.2:3128"}) == (
        "http://192.168.155.2:3128"
    )
    for bad in (
        "https://192.168.155.2:3128",
        "http://proxy.example.org:3128",
        "http://1.2.3.4",
        "",
    ):
        assert egress_proxy({"HORAE_EGRESS_PROXY": bad}) is None
    assert egress_proxy({"HTTPS_PROXY": "http://192.168.155.2:3128"}) is None  # only the core's
