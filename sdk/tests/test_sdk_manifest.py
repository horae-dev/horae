# SPDX-License-Identifier: Apache-2.0
"""Manifest helpers write manifests the core's loader accepts (#42)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.config.documents import read_yaml, validate
from horae.contracts import PluginManifest
from horae_sdk import Permissions, Plugin, dump_manifest, manifest
from horae_sdk.manifest import ManifestError, write_manifest
from sdk_helpers import HELLO, TASKS_FILE, load_example, schema_errors


def accepted(data: dict[str, Any]) -> PluginManifest:
    """`data` passes the generated JSON Schema and the core's model (from YAML, as loaded)."""
    assert schema_errors("plugin-manifest/v1.json", data) == []
    return PluginManifest.model_validate(yaml.safe_load(dump_manifest(data)))


def test_the_examples_manifests_are_valid_and_match_the_shipped_file(tmp_path: Path) -> None:
    hello = load_example(HELLO).plugin
    data = hello.manifest(command=["python3", "hello_plugin.py"], license="Apache-2.0")
    model = accepted(data)
    assert model.provides.sources == ["hello.greet"]
    assert model.provides.transforms == ["hello.shout"]
    assert model.permissions.intents == ["task.create"]
    shipped = HELLO.parent / "plugin.yaml"
    assert yaml.safe_load(shipped.read_text(encoding="utf-8")) == data
    validate(read_yaml(shipped), PluginManifest)  # the core's own loader
    sink = load_example(TASKS_FILE).plugin.manifest(command=["python3", "tasks_file_sink.py"])
    assert accepted(sink).provides.sinks == ["tasks-file.append"]
    write_manifest(tmp_path / "plugin.yaml", sink)
    validate(read_yaml(tmp_path / "plugin.yaml"), PluginManifest)


def test_a_full_manifest_is_valid() -> None:
    permissions = Permissions(
        intents=["note.create", "notify.push", "http.post"],
        network=["api.example.org", "coach"],
        secrets=["EXAMPLE_TOKEN"],
        read=["/srv/data"],
        write=["/srv/vault"],
    )
    data = manifest(
        name="full",
        version="1.2.3-rc.1+build.5",
        horae=">=0.1,<0.2",
        provides={
            "sources": ["full.read"],
            "transforms": ["full.clean"],
            "sinks": ["vault.write_note"],
            "notifiers": ["push.send"],
            "skills": ["full/summarise"],
            "pipelines": ["full-brief"],
        },
        permissions=permissions,
        command=["/usr/bin/python3", "-m", "full"],
        description="Everything at once.",
        license="MIT OR Apache-2.0",
        homepage="https://example.org/full",
    )
    model = accepted(data)
    assert model.permissions.paths.write == ["/srv/vault"]
    pack = manifest(name="pack", version="0.1.0", provides={"pipelines": ["daily"]})
    assert "runtime" not in pack
    assert pack["permissions"] == {}
    accepted(pack)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "Bad"}, "name"),
        ({"version": "1.0"}, "version"),
        ({"provides": {"tools": ["x.y"]}}, "unknown kind"),
        ({"provides": {"sources": ["nodot"]}}, "not a valid ref"),
        ({"provides": {"sources": ["a.b", "a.b"]}}, "twice"),
        ({"provides": {"skills": ["a.b"]}}, "not a valid ref"),
        ({"provides": {"sinks": ["v.write"], "sources": ["v.read"]}}, "sink operation"),
        ({"permissions": Permissions(intents=["note.delete"])}, "intents"),
        ({"permissions": Permissions(network=["Example.org"])}, "network"),
        ({"permissions": Permissions(secrets=["lower"])}, "secrets"),
        ({"permissions": Permissions(secrets=["HORAE_KEY"])}, "reserved"),
        ({"permissions": Permissions(read=["relative/path"])}, "paths.read"),
        ({"permissions": Permissions(write=["/a/../b"])}, "paths.write"),
        ({"permissions": Permissions(intents="note.create")}, "not a string"),
        ({"permissions": Permissions(intents=["note.create", "note.create"])}, "twice"),
        ({"command": []}, "runtime.command"),
        ({"homepage": "http://example.org"}, "homepage"),
    ],
)
def test_mistakes_are_refused(changes: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {"name": "ok", "version": "0.1.0", "provides": {}, **changes}
    with pytest.raises(ManifestError, match=message):
        manifest(**arguments)


def test_plugin_registration_rules() -> None:
    plugin = Plugin("dup")
    plugin.source("dup.a")(lambda call: {})
    with pytest.raises(ValueError, match="twice"):
        plugin.source("dup.a")(lambda call: {})
