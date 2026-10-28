# SPDX-License-Identifier: Apache-2.0
"""Loading an instance: horae.yaml, pipelines/ and horae.lock."""

import os
import shutil
from pathlib import Path

import pytest

from horae.config import (
    InstanceError,
    UnknownModelAlias,
    check_instance,
    load_instance,
)
from horae.contracts import InstanceConfig, Lockfile, Pipeline, PluginDefaults
from horae.secrets import Secret, SecretError


def _errors(root: Path) -> list[str]:
    with pytest.raises(InstanceError) as excinfo:
        load_instance(root)
    return [str(p) for p in excinfo.value.problems]


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_valid_instance_loads_into_typed_models(instance_dir: Path) -> None:
    instance = load_instance(instance_dir)
    assert isinstance(instance.config, InstanceConfig)
    assert isinstance(instance.lock, Lockfile)
    assert set(instance.pipelines) == {"daily-brief", "remind"}
    assert all(isinstance(p, Pipeline) for p in instance.pipelines.values())
    assert instance.pipeline_files["remind"] == instance_dir / "pipelines" / "remind.yaml"
    assert instance.config.timezone == "Europe/Paris"
    assert instance.state_dir == instance_dir / "state"
    assert instance.secrets_dir == instance_dir / "secrets"
    assert instance.config.plugins["github"].call_timeout_s == 20
    assert instance.lock.plugins["github"].permissions.secrets == ["GITHUB_TOKEN"]


def test_absolute_dirs_are_kept(instance_dir: Path, tmp_path: Path) -> None:
    _edit(instance_dir / "horae.yaml", "state_dir: state", f"state_dir: {tmp_path}/st")
    assert load_instance(instance_dir).state_dir == tmp_path / "st"


def test_model_aliases_resolve(with_secrets: Path, secret_values: tuple[str, str]) -> None:
    anthropic_key = secret_values[0]
    instance = load_instance(with_secrets)
    assert instance.model("personal").backend == "claude-cli"
    resolved = instance.resolve_model("default", instance.secret_resolver())
    assert (resolved.backend, resolved.model) == ("anthropic", "claude-sonnet-4-5")
    assert resolved.secret is not None
    assert resolved.secret.reveal() == anthropic_key
    assert anthropic_key not in repr(resolved)
    assert instance.resolve_model("personal", instance.secret_resolver()).secret is None
    with pytest.raises(UnknownModelAlias, match="unknown model alias 'nope'"):
        instance.model("nope")


def test_secret_resolver_reads_the_environment_only_through_env_refs(instance_dir: Path) -> None:
    _edit(
        instance_dir / "horae.yaml",
        "    ANTHROPIC_API_KEY: { file: anthropic_api_key }",
        "    ANTHROPIC_API_KEY: { file: anthropic_api_key }\n    GITHUB_TOKEN: { env: GH_TOKEN }",
    )
    instance = load_instance(instance_dir)
    environ = {"GH_TOKEN": "from-env", "ANTHROPIC_API_KEY": "never-read"}
    resolver = instance.secret_resolver(environ)
    assert resolver.resolve("GITHUB_TOKEN") == Secret("GITHUB_TOKEN", "from-env")
    with pytest.raises(SecretError, match="anthropic_api_key does not exist"):
        resolver.resolve("ANTHROPIC_API_KEY")


def test_plugin_timeouts(instance_dir: Path) -> None:
    instance = load_instance(instance_dir)
    assert instance.plugin_timeouts("github") == PluginDefaults(
        startup_timeout_s=10, call_timeout_s=20
    )
    assert instance.plugin_timeouts("other") == PluginDefaults()
    _edit(
        instance_dir / "horae.yaml",
        "plugins:",
        "plugin_defaults: { startup_timeout_s: 5, call_timeout_s: 60 }\nplugins:",
    )
    instance = load_instance(instance_dir)
    assert instance.plugin_timeouts("github") == PluginDefaults(
        startup_timeout_s=5, call_timeout_s=20
    )
    assert instance.plugin_timeouts("other").call_timeout_s == 60


def test_lockfile_and_pipelines_are_optional(instance_dir: Path) -> None:
    (instance_dir / "horae.lock").unlink()
    for path in (instance_dir / "pipelines").iterdir():
        path.unlink()
    _edit(instance_dir / "horae.yaml", "plugins:\n  github: { call_timeout_s: 20 }\n", "")
    instance = load_instance(instance_dir)
    assert instance.lock.plugins == {}
    assert instance.pipelines == {}
    (instance_dir / "pipelines").rmdir()
    assert load_instance(instance_dir).pipelines == {}


def test_missing_config(tmp_path: Path) -> None:
    assert _errors(tmp_path) == [f"{tmp_path / 'horae.yaml'}: file not found"]


def test_every_problem_is_reported_with_file_and_line(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    _edit(config, "backend: claude-cli", "backend: claude-web")
    lock = instance_dir / "horae.lock"
    _edit(lock, "commit: 3f2c9e1a7b4d5c6e8f9012345678abcdef012345", "commit: abc")
    brief = instance_dir / "pipelines" / "daily-brief.yaml"
    _edit(brief, "      budget: { usd: 0.05, tokens_out: 2000 }", "      budget: { usd: 0.05 }")
    assert _errors(instance_dir) == [
        f"{lock}:7:13: plugins.github.commit: "
        "must be a full git commit SHA (40 or 64 lowercase hex digits)",
        f"{config}:6:24: models.personal.backend: Input should be 'claude-cli', 'anthropic', "
        "'openai-compatible' or 'ollama'",
        f"{brief}:14:7: steps[2].llm.budget.tokens_out: missing required key 'tokens_out'",
    ]


def test_unknown_model_alias_in_pipeline(instance_dir: Path) -> None:
    brief = instance_dir / "pipelines" / "daily-brief.yaml"
    _edit(brief, "      model: default", "      model: fancy")
    assert _errors(instance_dir) == [
        f"{brief}:13:14: unknown model alias 'fancy' (horae.yaml defines: default, personal)"
    ]


def test_unknown_model_alias_without_any_alias(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    text = config.read_text(encoding="utf-8")
    start, end = text.index("models:"), text.index("plugins:")
    config.write_text(text[:start] + text[end:], encoding="utf-8")
    [error] = _errors(instance_dir)
    assert error.endswith("unknown model alias 'default' (horae.yaml defines: none)")


def test_pipeline_name_must_match_its_file(instance_dir: Path) -> None:
    remind = instance_dir / "pipelines" / "remind.yaml"
    _edit(remind, "name: remind", "name: reminders")
    assert _errors(instance_dir) == [
        f"{remind}:2:7: pipeline name 'reminders' does not match the file name 'remind'"
    ]


def test_pipeline_defined_twice(instance_dir: Path) -> None:
    pipelines = instance_dir / "pipelines"
    (pipelines / "remind.yml").write_bytes((pipelines / "remind.yaml").read_bytes())
    (pipelines / ".hidden.yaml").write_text("not: a pipeline\n", encoding="utf-8")
    (pipelines / "notes.txt").write_text("ignored\n", encoding="utf-8")
    assert _errors(instance_dir) == [
        f"{pipelines / 'remind.yml'}:2:7: pipeline 'remind' is defined twice"
    ]


def test_unknown_time_zones(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    _edit(config, "timezone: Europe/Paris", "timezone: Mars/Olympus")
    remind = instance_dir / "pipelines" / "remind.yaml"
    _edit(
        remind,
        'trigger: { cron: "*/5 * * * *" }',
        'trigger: { cron: "*/5 * * * *", timezone: Moon/Base }',
    )
    assert _errors(instance_dir) == [
        f"{config}:2:11: unknown time zone 'Mars/Olympus'",
        f"{remind}:3:43: unknown time zone 'Moon/Base'",
    ]


def test_configured_plugin_must_be_installed(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    _edit(config, "  github: { call_timeout_s: 20 }", "  github: {}\n  gitlab: {}")
    assert _errors(instance_dir) == [
        f"{config}:9:3: plugin 'gitlab' is not installed (not in horae.lock)"
    ]


def test_invalid_config_skips_cross_checks(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    config.write_text("models: [\n", encoding="utf-8")
    [error] = _errors(instance_dir)
    assert error.startswith(f"{config}:2:1: ")


def test_check_instance_reports_missing_secrets(instance_dir: Path) -> None:
    result = check_instance(instance_dir, environ={})
    assert not result.ok
    assert result.instance is not None
    config, lock = instance_dir / "horae.yaml", instance_dir / "horae.lock"
    secrets = instance_dir / "secrets"
    assert [str(p) for p in result.problems] == [
        f"{lock}:10:17: secret GITHUB_TOKEN: file {secrets / 'GITHUB_TOKEN'} does not exist",
        f"{config}:5:68: secret ANTHROPIC_API_KEY: file {secrets / 'anthropic_api_key'} "
        "does not exist",
    ]


def test_check_instance_ok(with_secrets: Path) -> None:
    result = check_instance(with_secrets)
    assert result.ok
    assert result.problems == []
    assert result.instance is not None


def test_check_instance_with_structural_problems(instance_dir: Path) -> None:
    (instance_dir / "horae.lock").write_text("schema_version: 2\n", encoding="utf-8")
    result = check_instance(instance_dir, environ={})
    assert result.instance is None
    assert any("horae.lock:1:1" in str(p) for p in result.problems)
    assert any("ANTHROPIC_API_KEY" in str(p) for p in result.problems)


@pytest.mark.parametrize("zone", ["America", "Etc", "Europe/", "posixrules/x"])
def test_time_zone_directories_are_not_zones(instance_dir: Path, zone: str) -> None:
    config = instance_dir / "horae.yaml"
    _edit(config, "timezone: Europe/Paris", f"timezone: {zone}")
    [error] = _errors(instance_dir)
    assert error.startswith(f"{config}:2:11: ")


@pytest.mark.parametrize("zone", ["UTC", "Etc/UTC", "America/Argentina/Buenos_Aires"])
def test_real_time_zones(instance_dir: Path, zone: str) -> None:
    _edit(instance_dir / "horae.yaml", "timezone: Europe/Paris", f"timezone: {zone}")
    assert load_instance(instance_dir).config.timezone == zone


def test_config_that_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "horae.yaml").mkdir()
    assert _errors(tmp_path) == [f"{tmp_path / 'horae.yaml'}: not a regular file"]


def test_non_regular_pipeline_entries_are_problems(instance_dir: Path) -> None:
    pipelines = instance_dir / "pipelines"
    (pipelines / "folder.yaml").mkdir()
    (pipelines / "other").mkdir()
    assert _errors(instance_dir) == [f"{pipelines / 'folder.yaml'}: not a regular file"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_fifo_pipeline_does_not_block(instance_dir: Path) -> None:
    fifo = instance_dir / "pipelines" / "fifo.yaml"
    os.mkfifo(fifo)
    assert _errors(instance_dir) == [f"{fifo}: not a regular file"]


def test_pipelines_that_is_a_file(instance_dir: Path) -> None:
    pipelines = instance_dir / "pipelines"
    shutil.rmtree(pipelines)
    pipelines.write_text("x", encoding="utf-8")
    assert _errors(instance_dir) == [f"{pipelines}: not a directory"]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_unreadable_pipelines_directory(instance_dir: Path) -> None:
    pipelines = instance_dir / "pipelines"
    pipelines.chmod(0)
    try:
        assert _errors(instance_dir) == [f"{pipelines}: cannot be read (Permission denied)"]
    finally:
        pipelines.chmod(0o700)


def test_unstattable_pipeline_entries(instance_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_stat = Path.stat

    def stat(self: Path, **kwargs: bool) -> os.stat_result:
        if self.name == "remind.yaml":
            raise PermissionError(13, "Permission denied")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    remind = instance_dir / "pipelines" / "remind.yaml"
    assert _errors(instance_dir) == [f"{remind}: cannot be read (Permission denied)"]


def test_unstattable_pipelines_dir(instance_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: object) -> os.stat_result:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "stat", refuse)
    pipelines = instance_dir / "pipelines"
    assert _errors(instance_dir) == [f"{pipelines}: cannot be read (Permission denied)"]


def test_invalid_lockfile_skips_the_plugin_cross_check(instance_dir: Path) -> None:
    lock = instance_dir / "horae.lock"
    lock.write_text("schema_version: 2\nplugins: {}\n", encoding="utf-8")
    # One problem, the lockfile's: the configured plugin is not also reported as missing.
    assert _errors(instance_dir) == [f"{lock}:1:17: schema_version: Input should be 1"]


def test_schema_version_is_optional_and_only_1(instance_dir: Path) -> None:
    config = instance_dir / "horae.yaml"
    assert load_instance(instance_dir).config.schema_version == 1
    _edit(config, "timezone: Europe/Paris", "schema_version: 1\ntimezone: Europe/Paris")
    assert load_instance(instance_dir).config.schema_version == 1
    _edit(config, "schema_version: 1", "schema_version: 2")
    assert _errors(instance_dir) == [f"{config}:2:17: schema_version: Input should be 1"]
