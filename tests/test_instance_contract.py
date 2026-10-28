# SPDX-License-Identifier: Apache-2.0
"""`horae.yaml` contract rules: backend endpoints, backend requirements, plugin timeouts."""

import pytest
from pydantic import TypeAdapter, ValidationError

from horae.contracts import BackendUrl, InstanceConfig, ModelAlias, PluginDefaults

URL: TypeAdapter[str] = TypeAdapter(BackendUrl)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.org/v1",
        "https://localhost:8443",
        "https://10.0.0.1",
        "https://[2001:db8::1]:443/v1",
        "http://ollama:11434",
        "http://localhost:11434/v1",
        "http://127.0.0.1",
        "http://127.255.0.9:80",
        "http://10.1.2.3:8080/v1",
        "http://172.16.0.5",
        "http://172.31.255.255",
        "http://192.168.1.10/?x=1",
        "http://[::1]:11434",
        "http://[fd12:3456::1]",
        "http://[fc00::1]",
    ],
)
def test_backend_url_accepts(url: str) -> None:
    assert URL.validate_python(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://ollama.example.org",  # public name over http
        "http://8.8.8.8",
        "http://172.32.0.1",
        "http://172.15.0.1",
        "http://192.169.0.1",
        "http://[2001:db8::1]",
        "http://[fc::1]",  # 00fc::1, not in fc00::/7
        "http://user:pw@ollama",
        "https://user@api.example.org",
        "https://api.example.org/#frag",
        "https://api.example.org/a b",
        "https://[zz::1]",
        "https://[fd12:::::1]",  # matches the pattern, but not an IPv6 address
        "ftp://ollama",
        "http://Ollama",
        "https://xn--ls8h.example.org",  # not a valid IDNA A-label
        "ollama:11434",
    ],
)
def test_backend_url_rejects(url: str) -> None:
    with pytest.raises(ValidationError):
        URL.validate_python(url)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"backend": "ollama", "model": "m"}, "backend 'ollama' requires `base_url`"),
        ({"backend": "openai-compatible", "model": "m"}, "requires `base_url`"),
        ({"backend": "anthropic", "model": "m"}, "backend 'anthropic' requires `secret`"),
    ],
)
def test_backend_requirements(spec: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelAlias.model_validate(spec)


def test_backends_without_requirements() -> None:
    assert ModelAlias(backend="claude-cli", model="sonnet").base_url is None
    alias = ModelAlias(backend="ollama", model="m", base_url="http://ollama:11434")
    assert alias.secret is None


def test_plugin_timeouts_are_bounded() -> None:
    assert PluginDefaults() == PluginDefaults(startup_timeout_s=10, call_timeout_s=30)
    assert PluginDefaults(startup_timeout_s=1, call_timeout_s=600).call_timeout_s == 600
    for bad in (0, 0.5, 600.5, True):
        with pytest.raises(ValidationError):
            PluginDefaults.model_validate({"call_timeout_s": bad})
        with pytest.raises(ValidationError):
            InstanceConfig.model_validate({"plugins": {"x": {"startup_timeout_s": bad}}})


def test_schema_version() -> None:
    assert InstanceConfig().schema_version == 1
    for bad in (2, True, 1.0, "1"):
        with pytest.raises(ValidationError):
            InstanceConfig.model_validate({"schema_version": bad})
