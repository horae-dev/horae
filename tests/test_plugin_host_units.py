# SPDX-License-Identifier: Apache-2.0
"""The plugin host's pure parts: environment, redaction, stderr capture, registry, commands."""

import base64
import binascii
import itertools
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.config import load_instance
from horae.contracts import PluginManifest
from horae.engine import UnknownPlugin
from horae.plugins.host import HostRegistry, PluginCallError, RegistryError, host_for_instance
from horae.plugins.host.capture import StderrCapture
from horae.plugins.host.env import BASE_KEYS, BASE_PATH, build_env, unsafe_name
from horae.plugins.host.host import resolve_command
from horae.plugins.host.redact import MIN_PARTIAL_BYTES, Redactor, SecretInKey, token
from horae.plugins.host.transport import exit_status
from horae.secrets import Secret
from plugin_fixtures import PROBE, lock_for, probe_manifest, registry

# --- environment -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "TZ",
        "LC_ALL",
        "HORAE_KEY",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "HTTPS_PROXY",
        "no_proxy",
        "SSL_CERT_DIR",
        "GIT_SSH_COMMAND",
        "IFS",
        "PERL5OPT",
        "RUBYOPT",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GLIBC_TUNABLES",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
        "PERL5LIB",
        "PERLLIB",
        "RUBYLIB",
        "SSLKEYLOGFILE",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "LOCPATH",
        "GCONV_PATH",
        "TZDIR",
        "LANGUAGE",
        "SHELLOPTS",
        "PS4",
        "GODEBUG",
        "MALLOC_CONF",
        "MALLOC_ARENA_MAX",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "npm_config_registry",
        "VIRTUAL_ENV",
        "A=B",
        "",
    ],
)
def test_the_deny_list(name: str) -> None:
    assert unsafe_name(name)


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "PROBE_TOKEN", "API_KEY", "PATHFINDER_KEY"])
def test_ordinary_secret_names_are_allowed(name: str) -> None:
    assert not unsafe_name(name)


def test_build_env_is_the_base_plus_the_secrets(tmp_path: Path) -> None:
    env = build_env(
        "p",
        home=tmp_path / "h",
        tmpdir=tmp_path / "t",
        timezone="Europe/Paris",
        secrets={"TOKEN": Secret("TOKEN", "v")},
    )
    assert env == {
        "PATH": BASE_PATH,
        "HOME": str(tmp_path / "h"),
        "TMPDIR": str(tmp_path / "t"),
        "LANG": "C.UTF-8",
        "TZ": "Europe/Paris",
        "TOKEN": "v",
    }
    assert set(env) - {"TOKEN"} == BASE_KEYS
    with pytest.raises(PluginCallError) as info:
        build_env(
            "p",
            home=tmp_path,
            tmpdir=tmp_path,
            timezone="UTC",
            secrets={"T": Secret("T", "a\x00b")},
        )
    assert info.value.kind == "env_unsafe"
    assert "a\x00b" not in info.value.message


# --- redaction -------------------------------------------------------------------------------

SECRET = "Sup3r-Secret/Value+é"  # noqa: S105 (synthetic)


def test_every_encoding_of_a_secret_is_redacted() -> None:
    redactor = Redactor([Secret("TOKEN", SECRET)])
    raw = SECRET.encode()
    forms = [
        SECRET,
        base64.b64encode(raw).decode(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode(),
        "Sup3r-Secret%2FValue%2B%C3%A9",
        "Sup3r-Secret%2fValue%2b%c3%a9",
        "Sup3r-Secret/Value+\\u00e9",
    ]
    for form in forms:
        text, found = redactor.redact_text(f"<{form}>")
        assert text == f"<{token('TOKEN')}>", form
        assert found == {"TOKEN"}
        assert redactor.redact_bytes(f"<{form}>".encode()) == f"<{token('TOKEN')}>".encode()


def test_the_longest_secret_wins_and_data_is_walked() -> None:
    redactor = Redactor([Secret("INNER", "abcdefgh12"), Secret("OUTER", "xxabcdefgh12xx")])
    text = "xxabcdefgh12xx abcdefgh12"
    assert redactor.redact_text(text)[0] == "[REDACTED:OUTER] [REDACTED:INNER]"
    deep: Any = "abcdefgh12"
    for _ in range(500):  # deeper than any recursion limit would like
        deep = [deep]
    data, found = redactor.redact_data({"key": deep, "n": 1, "ok": [True, None, 2.5]})
    assert found == {"INNER"}
    assert data["n"] == 1
    assert data["ok"] == [True, None, 2.5]
    inner = data["key"]
    for _ in range(500):
        inner = inner[0]
    assert inner == "[REDACTED:INNER]"


def test_misaligned_base64_hex_basic_auth_and_json_are_redacted() -> None:
    sample = "hunter2-is-long-enough"
    redactor = Redactor([Secret("S", sample)])
    raw = sample.encode()
    for before, after in itertools.product(range(6), range(4)):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            stream = encode(os.urandom(before) + raw + os.urandom(after)).decode()
            text, found = redactor.redact_text(stream)
            assert found == {"S"}, (before, after, stream)
            # Only characters that also depend on the neighbouring bytes stay (at most 2 a side).
            assert len(text.replace(token("S"), "")) <= len(stream) - len(raw) * 4 // 3 + 4
    basic = "Authorization: Basic " + base64.b64encode(b"user:" + raw).decode()
    assert token("S") in redactor.redact_text(basic)[0]
    for form in (binascii.hexlify(raw).decode(), binascii.hexlify(raw).decode().upper()):
        assert redactor.redact_text(f"hex={form};")[0] == f"hex={token('S')};"
    embedded = json.dumps({"auth": {"token": sample, "note": f"Bearer {sample}"}})
    assert sample not in redactor.redact_text(embedded)[0]
    assert redactor.redact_bytes(embedded.encode()).count(token("S").encode()) == 2


def test_short_secrets_match_their_bounded_raw_value_only() -> None:
    short = "pin42"
    assert len(short) < MIN_PARTIAL_BYTES
    redactor = Redactor([Secret("PIN", short), Secret("NUM", "12345678901")])
    assert redactor.short == ("PIN",)
    pin = token("PIN")
    # Inside text: matched when bounded (punctuation, spaces, the ends), not inside a word.
    assert redactor.redact_text(f"token={short};") == (f"token={pin};", {"PIN"})
    assert redactor.redact_text(f"probe.fail: {short}")[0] == f"probe.fail: {pin}"
    assert redactor.redact_text(f"spin42 xpin42y pin42x {short}") == (
        f"spin42 xpin42y pin42x {pin}",
        {"PIN"},
    )
    assert (
        redactor.redact_bytes(b"auth " + short.encode() + b"\n") == b"auth " + pin.encode() + b"\n"
    )
    assert redactor.redact_bytes(b"spin42s") == b"spin42s"
    # Encodings of a short secret are not looked for (they would match everywhere).
    encoded = base64.b64encode(short.encode()).decode()
    assert redactor.redact_text(encoded) == (encoded, set())
    value = {
        "a": short,
        "b": f"x{short}",
        "c": f'{{"token": "{short}"}}',
        "n": 12345678901,
        "f": 1.5,
        "t": True,
    }
    data, found = redactor.redact_data(value)
    assert data == {
        "a": pin,
        "b": f"x{short}",
        "c": f'{{"token": "{pin}"}}',
        "n": token("NUM"),
        "f": 1.5,
        "t": True,
    }
    assert found == {"PIN", "NUM"}


def test_object_keys_are_never_rewritten() -> None:
    redactor = Redactor([Secret("PIN", "pin42"), Secret("LONG", "a-long-secret-value")])
    for key in ("pin42", "has a-long-secret-value inside"):
        with pytest.raises(SecretInKey) as info:
            redactor.redact_data({"ok": [{key: 1}]})
        assert "pin42" not in str(info.value)
        assert "a-long-secret-value" not in str(info.value)
    assert redactor.redact_data({"xpin42": 1}) == ({"xpin42": 1}, set())


def test_an_empty_redactor_changes_nothing() -> None:
    redactor = Redactor([])
    assert not redactor
    assert redactor.redact_text("x") == ("x", set())
    assert redactor.redact_bytes(b"x") == b"x"
    assert redactor.redact_data({"a": "x"}) == ({"a": "x"}, set())
    assert redactor.spans(b"x") == []
    assert StderrCapture(0).take(redactor) is None


# --- stderr capture --------------------------------------------------------------------------


def _feed(capture: StderrCapture, data: bytes, chunk: int) -> None:
    for start in range(0, len(data), chunk):
        capture.feed(data[start : start + chunk])


def test_a_secret_at_any_offset_around_the_cuts_never_leaks() -> None:
    """Property: for every position of the secret relative to the head and tail cuts, and
    several chunkings, the text holds no character of the secret outside its tokens."""
    secret = "SECRETVALUE"  # noqa: S105 (synthetic)
    redactor = Redactor([Secret("S", secret)])
    head, tail = 32, 64
    margin = redactor.max_len - 1
    for total, position, chunk in itertools.product((200, 400), range(0, 200, 3), (1, 7, 64)):
        if position + len(secret) > total:
            continue
        data = bytearray(b"." * total)
        data[position : position + len(secret)] = secret.encode()
        tail_secret = total - tail - 4  # one more, across the tail cut
        if tail_secret > position + len(secret):
            data[tail_secret : tail_secret + len(secret)] = secret.encode()
        capture = StderrCapture(margin, head=head, tail=tail)
        _feed(capture, bytes(data), chunk)
        text = capture.take(redactor)
        assert text is not None
        rest = text.replace(token("S"), "").replace("bytes elided", "")
        assert not set(rest) & set(secret), (total, position, chunk, text)


def test_a_short_stream_is_redacted_whole_and_windows_restart() -> None:
    redactor = Redactor([Secret("S", "SECRET-VALUE")])
    capture = StderrCapture(redactor.max_len - 1, head=8, tail=8)
    capture.feed(b"abc SECRET-VALUE def")
    assert capture.take(redactor) == "abc [REDACTED:S] def"
    assert capture.take(redactor) is None
    capture.feed(b"\xff\xfe broken utf-8")
    assert capture.take(redactor) == "�� broken utf-8"


def test_an_elided_stream_keeps_head_and_tail() -> None:
    redactor = Redactor([])
    capture = StderrCapture(0, head=4, tail=4)
    capture.feed(b"HEAD" + b"-" * 100 + b"TAIL")
    text = capture.take(redactor)
    assert text == "HEAD\n…[100 bytes elided]…\nTAIL"


def test_exit_status() -> None:
    assert exit_status(None) == "still running"
    assert exit_status(3) == "exited with code 3"
    assert exit_status(-9) == "killed by signal SIGKILL"
    assert exit_status(-999) == "killed by signal 999"


# --- registry and commands -------------------------------------------------------------------


def test_the_registry_checks_the_lock() -> None:
    manifest = probe_manifest()
    model = PluginManifest.model_validate(manifest)
    lock = lock_for(manifest)
    with pytest.raises(RegistryError, match="not pinned"):
        HostRegistry(lock_for(), {"probe": model})
    with pytest.raises(RegistryError, match=r"horae\.lock pins 0\.1\.0"):
        HostRegistry(lock, {"probe": model.model_copy(update={"version": "0.2.0"})})
    greedy = probe_manifest(
        permissions={
            "secrets": ["PROBE_TOKEN", "EXTRA"],
            "network": ["api.example.org"],
            "paths": {"read": ["/data"], "write": ["/out"]},
            "intents": ["notify.push"],
        }
    )
    with pytest.raises(RegistryError) as info:
        HostRegistry(lock, {"probe": PluginManifest.model_validate(greedy)})
    message = str(info.value)
    for part in ("secrets: EXTRA", "network: api.example.org", "paths.read: /data", "paths.write"):
        assert part in message
    twin = dict(manifest, name="twin")
    with pytest.raises(RegistryError, match="provided by both"):
        registry(manifest, twin)


def test_the_locked_permissions_are_in_force() -> None:
    manifest = probe_manifest(permissions={"secrets": [], "intents": []})
    lock = lock_for(probe_manifest())  # the user approved more than the manifest asks
    reg = HostRegistry(lock, {"probe": PluginManifest.model_validate(manifest)})
    assert reg.plugin_for_tool("probe.env").manifest.permissions.secrets == ["PROBE_TOKEN"]
    with pytest.raises(UnknownPlugin):
        reg.plugin_for_tool("probe.nothing")
    with pytest.raises(UnknownPlugin):
        reg.plugin_for_skill("probe/nothing")
    with pytest.raises(UnknownPlugin):
        reg.installed("nobody")
    assert reg.installed("probe").root is None


def test_from_directory_reads_each_locked_plugin(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    shutil.copytree(PROBE, root / "probe")
    reg = HostRegistry.from_directory(lock_for(probe_manifest()), root)
    installed = reg.installed("probe")
    assert installed.root == root / "probe"
    assert installed.manifest.provides.sources[0] == "probe.env"


def test_relative_commands_stay_inside_the_plugin(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    (root / "bin" / "link").symlink_to(outside)
    for command in ("../outside", "bin/../../outside", "bin/link"):
        with pytest.raises(PluginCallError) as info:
            resolve_command([command], root)
        assert info.value.kind == "spawn_failed"
        assert "outside the plugin" in info.value.message
    inside = root / "bin" / "run"
    inside.write_text("#!/bin/sh\n", encoding="utf-8")
    inside.chmod(0o755)
    relative = Path(os.path.relpath(root, Path.cwd()))
    resolved = resolve_command(["bin/../bin/run"], relative)
    assert resolved == [str(inside.resolve())]
    assert os.path.isabs(resolved[0])


def test_resolve_command(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "run"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    assert resolve_command(["bin/run", "x"], tmp_path) == [str(script.resolve()), "x"]
    assert resolve_command([sys.executable], None) == [sys.executable]
    env = shutil.which("env", path=BASE_PATH)
    assert env is not None
    assert resolve_command(["env"], None) == [env]
    for command, root in (
        (["bin/run"], None),
        (["no-such-command-x"], None),
        (["bin/nope"], tmp_path),
    ):
        with pytest.raises(PluginCallError) as info:
            resolve_command(command, root)
        assert info.value.kind == "spawn_failed"
    (tmp_path / "plain").write_text("", encoding="utf-8")
    with pytest.raises(PluginCallError):
        resolve_command([str(tmp_path / "plain")], None)


def test_host_for_instance(with_secrets: Path) -> None:
    instance = load_instance(with_secrets)
    data = yaml.safe_load((PROBE / "plugin.yaml").read_text(encoding="utf-8"))
    model = PluginManifest.model_validate(data)
    reg = HostRegistry(lock_for(data), {"probe": model})
    h = host_for_instance(instance, reg)
    assert h.timezone == instance.config.timezone
    assert h.registry is reg
    assert h._work_root == instance.state_dir / "plugin-runs"
