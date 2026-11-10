# SPDX-License-Identifier: Apache-2.0
"""The egress proxy from the host's side, without an engine (#50, schemas decision 76).

Its `docker run` (the same hardening as a plugin container, `nobody`, the two networks, no
mount, no secret), the egress network check, the plugin environment and the blocked-attempt
reports. The host's use of a (fake) proxy is in `tests/test_plugin_container.py`, the real
one in `tests/test_plugin_egress_live.py`.
"""

import itertools
import json
from pathlib import Path
from typing import Any, cast

import anyio.abc
import pytest

from horae.plugins.host import egress
from horae.plugins.host.container import EGRESS_LABEL, IMAGE_PYTHON, container_belongs
from horae.plugins.host.egress_proxy import MAX_EVENTS
from horae.plugins.host.env import PROXY_KEYS, build_env, unsafe_name
from horae.plugins.host.errors import PluginCallError
from horae.secrets import Secret

IMAGE = "python:3.12-slim-bookworm@sha256:" + "a" * 64


def _argv(**changes: Any) -> list[str]:
    settings: dict[str, Any] = {
        "plugin": "jail",
        "name": "horae-egress-jail-0123456789ab",
        "image": IMAGE,
        "plugin_network": "horae-plugin-jail",
        "policy": egress.proxy_policy(
            ["api.example.org"], ["printer.lan:8080"], "192.168.155.0/24"
        ),
        "python": IMAGE_PYTHON,
        "labels": {EGRESS_LABEL: "jail"},
    } | changes
    return egress.proxy_argv("/usr/bin/docker", **settings)


def test_the_proxy_container_is_hardened_like_a_plugin() -> None:
    argv = _argv()
    image = argv.index(IMAGE)
    flags = argv[:image]
    for flag in ("--interactive", "--rm", "--init", "--read-only"):
        assert flag in flags
    pairs = list(itertools.pairwise(flags))
    for pair in (
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pull", "never"),
        ("--log-driver", "none"),
        ("--pids-limit", "64"),
        ("--memory", "128m"),
        ("--label", "dev.horae.egress-proxy=jail"),
        ("--entrypoint", IMAGE_PYTHON),
    ):
        assert pair in pairs
    networks = [value for flag, value in pairs if flag == "--network"]
    assert networks == ["horae-plugin-jail", "horae-egress"]
    for widening in (
        "--privileged",
        "--cap-add",
        "--device",
        "--mount",
        "--volume",
        "-v",
        "--env",
        "-e",
        "--env-file",
        "--publish",
        "-p",
        "--pid",
        "--ipc",
        "--uts",
        "--userns",
    ):
        assert widening not in flags
    assert not any("seccomp" in arg or arg == "host" for arg in flags)
    assert argv[image + 1 : image + 4] == ["-I", "-S", "-c"]
    assert argv[image + 4] == egress.PROXY_SOURCE
    assert json.loads(argv[-1]) == {
        "network": ["api.example.org"],
        "network_local": ["printer.lan:8080"],
        "clients": ["192.168.155.0/24"],
    }


def test_the_policy_is_sorted_and_secret_free() -> None:
    policy = egress.proxy_policy(["b.example.org", "a.example.org"], [], "10.0.0.0/24")
    assert policy == (
        '{"network":["a.example.org","b.example.org"],"network_local":[],"clients":["10.0.0.0/24"]}'
    )


def test_the_plugin_network_subnet_is_read_from_its_ipam() -> None:
    subnet = egress.network_subnet
    assert subnet({"IPAM": {"Config": [{"Subnet": "192.168.155.0/24"}]}}) == "192.168.155.0/24"
    assert subnet({"IPAM": {"Config": [{"Subnet": "fd00::/64"}, {"Subnet": "10.1.0.0/16"}]}}) == (
        "10.1.0.0/16"
    )
    assert subnet({"IPAM": {"Config": []}}) is None
    assert subnet({"IPAM": None}) is None
    assert subnet({}) is None


def _egress(**changes: Any) -> dict[str, Any]:
    return {
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "ConfigOnly": False,
        "ConfigFrom": {"Network": ""},
        "Labels": {"dev.horae.network": "egress"},
        "Options": {"com.docker.network.bridge.enable_icc": "false"},
    } | changes


def test_only_a_plain_labelled_bridge_is_the_egress_network() -> None:
    problem = egress.egress_network_problem
    assert problem(_egress()) is None
    assert problem(_egress(Internal=True)) is not None
    assert problem(_egress(Driver="host")) == "is not a bridge network"
    assert problem(_egress(EnableIPv6=True)) == "has IPv6 enabled"
    assert problem(_egress(ConfigOnly=True)) is not None
    assert problem(_egress(ConfigFrom={"Network": "x"})) is not None
    assert problem(_egress(Labels={"dev.horae.network": "plugin"})) is not None
    assert problem(_egress(Labels=None)) is not None
    assert problem(_egress(Options={})) is not None
    assert problem(_egress(Options=None)) == "has no driver options"


def test_a_proxy_may_join_its_own_plugin_network_only() -> None:
    assert container_belongs({EGRESS_LABEL: "jail"}, "jail")
    assert not container_belongs({EGRESS_LABEL: "other"}, "jail")
    assert not container_belongs({EGRESS_LABEL: ""}, "jail")


def test_the_proxy_address_is_its_ipv4_on_the_plugin_network() -> None:
    networks = {
        "horae-plugin-jail": {"IPAddress": "192.168.155.2"},
        "horae-egress": {"IPAddress": "192.168.156.2"},
    }
    assert egress.plugin_address(networks, "horae-plugin-jail") == "192.168.155.2"
    assert egress.plugin_address(networks, "horae-plugin-other") is None
    assert (
        egress.plugin_address({"horae-plugin-jail": {"IPAddress": ""}}, "horae-plugin-jail") is None
    )
    assert (
        egress.plugin_address({"horae-plugin-jail": {"IPAddress": "0.0.0.0"}}, "horae-plugin-jail")  # noqa: S104
        is None
    )
    assert (
        egress.plugin_address({"horae-plugin-jail": {"IPAddress": "x"}}, "horae-plugin-jail")
        is None
    )
    assert egress.plugin_address(None, "horae-plugin-jail") is None


def test_a_plugin_container_gets_the_proxy_and_nothing_bypasses_it() -> None:
    env = build_env(
        "jail",
        home=Path("/tmp/home"),  # noqa: S108 (a container path)
        tmpdir=Path("/tmp/tmp"),  # noqa: S108
        timezone="UTC",
        secrets={},
        proxy="http://192.168.155.2:3128",
    )
    for key in PROXY_KEYS:
        assert env[key] == "http://192.168.155.2:3128"
    assert not any(key.upper() in ("NO_PROXY", "ALL_PROXY") for key in env)
    for key in (*PROXY_KEYS, "NO_PROXY", "no_proxy", "ALL_PROXY"):
        assert unsafe_name(key)  # a secret can never override or bypass it
    with pytest.raises(PluginCallError) as info:
        build_env(
            "jail",
            home=Path("/h"),
            tmpdir=Path("/t"),
            timezone="UTC",
            secrets={"NO_PROXY": Secret("NO_PROXY", "*")},
        )
    assert info.value.kind == "env_unsafe"


@pytest.mark.parametrize(
    ("event", "detail"),
    [
        (
            {
                "event": "blocked",
                "host": "evil.example.net",
                "port": 443,
                "reason": "host_not_allowed",
            },
            "jail: egress blocked to evil.example.net:443 (host_not_allowed)",
        ),
        (
            {"event": "blocked", "host": "fd00::10", "port": 8123, "reason": "ip_literal"},
            "jail: egress blocked to [fd00::10]:8123 (ip_literal)",
        ),
        (
            {"event": "blocked", "host": "evil\nINJECTED", "port": 99999, "reason": "ip_literal"},
            "jail: egress blocked to ?:0 (ip_literal)",
        ),
        ({"event": "blocked", "host": "x.test", "port": 1, "reason": "Not A Code"}, None),
        ({"event": "ready", "port": 3128}, None),
        ({"event": "suppressed"}, "jail: more blocked egress attempts (not listed)"),
        ("blocked", None),
    ],
)
def test_reports_are_checked_again_by_the_host(event: object, detail: str | None) -> None:
    assert egress.blocked_detail("jail", event) == detail


def test_reports_are_kept_until_taken_and_capped() -> None:
    handle = egress.ProxyHandle("jail", "n", cast(anyio.abc.Process, None), _nothing)
    line = json.dumps(
        {"event": "blocked", "host": "evil.example.net", "port": 443, "reason": "host_not_allowed"}
    ).encode()
    handle.feed(line)
    handle.feed(b"not json")
    assert handle.take() == ["jail: egress blocked to evil.example.net:443 (host_not_allowed)"]
    assert handle.take() == []
    for _ in range(MAX_EVENTS + 3):
        handle.feed(line)
    taken = handle.take()
    assert len(taken) == MAX_EVENTS + 1
    assert taken[-1] == "jail: 3 more blocked egress attempts"
    violations = egress.violations(taken[:1])
    assert [violation.kind for violation in violations] == ["egress_blocked"]


async def _nothing() -> None:
    return None
