# SPDX-License-Identifier: Apache-2.0
"""Declared mounts and resource limits of plugin containers, without an engine (#51, schemas
decision 78): the mount plan (what is refused and why, nesting), the `docker run` flags, the
`horae.yaml` settings and the sweep of writable paths once a container is gone.
`tests/test_plugin_container.py` runs them through a `PluginHost` with a fake Docker CLI, and
`tests/test_plugin_container_live.py` against a real engine. Synthetic paths only.
"""

import os
import shutil
import socket
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.config.loader import Instance
from horae.contracts import InstanceConfig, Lockfile
from horae.contracts.instance import ContainerConfig, ContainerLimits
from horae.engine.canonical import sha256_hex
from horae.plugins.host import PluginCallError, mounts
from horae.plugins.host.container import limit_options, run_argv
from horae.plugins.host.mounts import Mount, MountPolicy, check_path, plan_mounts, sweep


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """A synthetic instance (real path): its control files, a vault, and a home directory."""
    root = (tmp_path / "instance").resolve()
    for directory in ("vault/inbox/deep", "vault/archive", "state", "secrets", "plugins/x"):
        (root / directory).mkdir(parents=True)
    for name in ("horae.yaml", "horae.lock", "vault/note.md"):
        (root / name).write_text("x\n", encoding="utf-8")
    (tmp_path / "home" / ".ssh").mkdir(parents=True)
    return root


def _policy(root: Path, *roots: str) -> MountPolicy:
    return MountPolicy.for_instance(
        root,
        state_dir=root / "state",
        secrets_dir=root / "secrets",
        mount_roots=roots,
        home=root.parent / "home",
    )


def _refused(path: str, policy: MountPolicy) -> str:
    with pytest.raises(PluginCallError) as info:
        check_path("jail", path, policy)
    assert info.value.kind == "runtime_unavailable"
    return info.value.message


# --- the mount plan -----------------------------------------------------------------------------


def test_declared_paths_are_mounted_at_the_same_path_read_only_unless_writable(
    instance: Path,
) -> None:
    vault, inbox = str(instance / "vault"), str(instance / "vault" / "inbox")
    plan = plan_mounts("jail", [vault, inbox], [inbox], _policy(instance))
    assert [(m.path, m.writable) for m in plan] == [(vault, False), (inbox, True)]
    assert [m.option() for m in plan] == [
        f"type=bind,source={vault},target={vault},bind-recursive=disabled,readonly",
        f"type=bind,source={inbox},target={inbox},bind-recursive=disabled",
    ]
    assert plan_mounts("jail", [], [], None) == ()


def test_nested_declarations_are_ordered_parent_first_and_keep_their_access(
    instance: Path,
) -> None:
    vault = instance / "vault"
    read = [str(vault / "inbox" / "deep"), str(vault)]
    write = [str(vault / "inbox")]
    plan = plan_mounts("jail", read, write, _policy(instance))
    # Writable child of a read-only parent, read-only child of that writable child.
    assert [(m.path, m.writable) for m in plan] == [
        (str(vault), False),
        (str(vault / "inbox"), True),
        (str(vault / "inbox" / "deep"), False),
    ]
    reverse = plan_mounts("jail", [str(vault / "inbox")], [str(vault)], _policy(instance))
    assert [(m.path, m.writable) for m in reverse] == [
        (str(vault), True),
        (str(vault / "inbox"), False),
    ]


def test_a_path_both_read_and_written_is_one_writable_mount(instance: Path) -> None:
    vault = str(instance / "vault")
    plan = plan_mounts("jail", [vault], [vault], _policy(instance))
    assert [(m.path, m.writable) for m in plan] == [(vault, True)]


def test_a_file_can_be_declared(instance: Path) -> None:
    note = str(instance / "vault" / "note.md")
    assert [m.path for m in plan_mounts("jail", [note], [], _policy(instance))] == [note]


@pytest.mark.parametrize("bad", [",readonly=false", ",source=/", '"', "\n", "\x00x", "\x7f"])
def test_mount_metacharacters_are_refused(instance: Path, bad: str) -> None:
    path = str(instance / "vault") + bad
    message = _refused(path, _policy(instance))
    assert "a quote or a control character" in message


def test_a_symbolic_link_is_refused_wherever_it_is(instance: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = _policy(instance)
    (instance / "vault" / "escape").symlink_to(outside)
    assert "symbolic link" in _refused(str(instance / "vault" / "escape"), policy)
    (instance / "vault" / "inner").symlink_to(instance / "vault" / "archive")
    assert "symbolic link" in _refused(str(instance / "vault" / "inner"), policy)
    linked = instance / "linked-vault"
    linked.symlink_to(instance / "vault")
    assert "symbolic link" in _refused(str(linked / "inbox"), policy)  # through a parent


def test_a_path_outside_the_instance_and_mount_roots_is_refused(
    instance: Path, tmp_path: Path
) -> None:
    elsewhere = (tmp_path / "elsewhere").resolve()
    (elsewhere / "vault").mkdir(parents=True)
    message = _refused(str(elsewhere / "vault"), _policy(instance))
    assert "outside the instance directory and `container.mount_roots`" in message
    assert check_path("jail", str(elsewhere / "vault"), _policy(instance, str(elsewhere)))


@pytest.mark.parametrize(
    "name",
    ["state", "secrets", "horae.yaml", "horae.lock", "plugins", "plugins/x", "."],
)
def test_the_instance_controls_are_never_mounted(instance: Path, name: str) -> None:
    path = os.path.normpath(instance / name)
    assert "which no plugin may see" in _refused(path, _policy(instance))


def test_a_path_containing_a_sensitive_one_is_refused(instance: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    policy = _policy(instance, str(home.resolve()))
    assert "which no plugin may see" in _refused(str(home.resolve()), policy)  # holds ~/.ssh
    assert ".ssh" in _refused(str((home / ".ssh").resolve()), policy)


def test_secrets_and_state_outside_the_instance_stay_sensitive(tmp_path: Path) -> None:
    root = (tmp_path / "instance").resolve()
    shared = (tmp_path / "shared").resolve()
    for directory in (root, shared / "secrets", shared / "state", shared / "vault"):
        directory.mkdir(parents=True)
    policy = MountPolicy.for_instance(
        root,
        state_dir=shared / "state",
        secrets_dir=shared / "secrets",
        mount_roots=[str(shared)],
        home=tmp_path / "home",
    )
    assert check_path("jail", str(shared / "vault"), policy)
    for path in (shared, shared / "secrets", shared / "state"):
        assert "which no plugin may see" in _refused(str(path), policy)


def test_the_docker_socket_and_its_directory_are_refused(tmp_path: Path) -> None:
    short = Path(tempfile.mkdtemp(prefix="h51-", dir="/tmp")).resolve()
    try:
        policy = MountPolicy.for_instance(
            short,
            state_dir=short / "state",
            secrets_dir=short / "secrets",
            home=tmp_path / "home",
        )
        run = short / "run"
        run.mkdir()
        sock_path = run / "d.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(sock_path))
            assert "not a directory or a regular file" in _refused(str(sock_path), policy)
        engine = policy.with_sensitive(str(sock_path))
        assert "which no plugin may see" in _refused(str(run), engine)  # holds the socket
        assert check_path("jail", str(run), policy)  # without it, an ordinary directory
    finally:
        shutil.rmtree(short)


@pytest.mark.parametrize("system", ["/etc", "/proc", "/var/run", "/usr", "/run", "/dev"])
def test_system_paths_are_sensitive(system: str) -> None:
    real = os.path.realpath(system)
    if not os.path.isdir(real):
        pytest.skip(f"no {system} here")
    policy = MountPolicy.for_instance(
        Path("/nonexistent-instance"),
        state_dir=Path("/nonexistent-instance/state"),
        secrets_dir=Path("/nonexistent-instance/secrets"),
    )
    roots_all = MountPolicy(roots=("/",), sensitive=policy.sensitive)
    assert "which no plugin may see" in _refused(real, roots_all)


@pytest.mark.skipif(os.path.realpath("/tmp") != "/tmp", reason="/tmp is a link here")  # noqa: S108
def test_the_container_tmp_may_hold_a_declared_path_but_never_be_one() -> None:
    policy = MountPolicy(roots=("/",), sensitive=())
    assert "container's own /tmp" in _refused("/tmp", policy)  # noqa: S108


def test_a_missing_path_or_a_special_file_is_refused(instance: Path) -> None:
    policy = _policy(instance)
    assert "does not exist" in _refused(str(instance / "vault" / "absent"), policy)
    fifo = instance / "vault" / "fifo"
    os.mkfifo(fifo)
    assert "not a directory or a regular file" in _refused(str(fifo), policy)


def test_a_path_that_is_not_normalised_is_refused(instance: Path) -> None:
    policy = _policy(instance)
    assert "normalised" in _refused(str(instance / "vault") + "/../vault", policy)
    assert "normalised" in _refused("vault", policy)


def test_one_refused_path_refuses_the_plugin(instance: Path) -> None:
    good, bad = str(instance / "vault"), str(instance / "state")
    with pytest.raises(PluginCallError) as info:
        plan_mounts("jail", [good], [bad], _policy(instance))
    assert bad in info.value.message
    with pytest.raises(PluginCallError) as info:
        plan_mounts("jail", [good], [], None)
    assert "no instance" in info.value.message


def test_the_instance_policy_comes_from_horae_yaml(instance: Path, tmp_path: Path) -> None:
    from horae.plugins.host import host_for_instance
    from horae.plugins.host.host import HostRegistry

    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    config = InstanceConfig.model_validate(
        {"container": {"mount_roots": [str(elsewhere)]}, "secrets": {"dir": "secrets"}}
    )
    loaded = Instance(instance, config, {}, Lockfile.model_validate({"schema_version": 1}))
    registry = HostRegistry(loaded.lock, {}, {})
    h = host_for_instance(loaded, registry)
    policy = h._containers.mounts
    assert policy is not None
    assert str(elsewhere) in policy.roots
    assert str(instance) in policy.roots
    assert str(instance / "secrets") in policy.sensitive
    assert str(instance / "state") in policy.sensitive


# --- limits ---------------------------------------------------------------------------------


def test_the_default_limits_and_their_flags() -> None:
    limits = ContainerLimits()
    assert (limits.cpus, limits.memory_mb, limits.pids, limits.tmp_mb, limits.nofile) == (
        1.0,
        512,
        128,
        64,
        1024,
    )
    assert limit_options(limits) == [
        "--cpus=1",
        "--memory=512m",
        "--memory-swap=512m",  # equal: no swap
        "--pids-limit=128",
        "--ulimit=nofile=1024:1024",
    ]


def test_run_argv_carries_the_limits_and_the_declared_mounts() -> None:
    declared = [Mount("/srv/vault", False, (1, 2)), Mount("/srv/vault/inbox", True, (1, 3))]
    argv = run_argv(
        "/usr/bin/docker",
        plugin="jail",
        name="horae-jail-0123456789ab",
        image="image@sha256:" + "0" * 64,
        user="10001:10001",
        source="/srv/instance/plugins/jail",
        command=["/plugin/.venv/bin/python"],
        mounts=declared,
        limits=ContainerLimits(cpus=2.5, memory_mb=1024, pids=256, tmp_mb=128, nofile=4096),
    )
    options = argv[: argv.index("image@sha256:" + "0" * 64)]
    assert "--rm" not in options
    for flag in (
        "--cpus=2.5",
        "--memory=1024m",
        "--memory-swap=1024m",
        "--pids-limit=256",
        "--ulimit=nofile=4096:4096",
    ):
        assert flag in options
    assert options[options.index("--tmpfs") + 1].endswith(",size=128m,mode=1777")
    mounted = [options[i + 1] for i, flag in enumerate(options) if flag == "--mount"]
    assert mounted == [
        "type=bind,source=/srv/instance/plugins/jail,target=/plugin,readonly",
        "type=bind,source=/srv/vault,target=/srv/vault,bind-recursive=disabled,readonly",
        "type=bind,source=/srv/vault/inbox,target=/srv/vault/inbox,bind-recursive=disabled",
    ]


def test_limits_are_set_per_plugin_over_the_defaults() -> None:
    config = InstanceConfig.model_validate(
        {
            "plugin_defaults": {"runtime": "container", "limits": {"memory_mb": 1024}},
            "plugins": {"big": {"limits": {"cpus": 2.0, "memory_mb": 2048}}, "small": {}},
        }
    )
    assert config.plugin_timeouts("big").limits == ContainerLimits(cpus=2.0, memory_mb=2048)
    assert config.plugin_timeouts("small").limits == ContainerLimits(memory_mb=1024)
    assert config.plugin_timeouts("other").limits == ContainerLimits(memory_mb=1024)
    assert InstanceConfig().plugin_timeouts("x").limits == ContainerLimits()


@pytest.mark.parametrize(
    "limits",
    [
        {"cpus": 0.0},
        {"cpus": 65.0},
        {"cpus": "1"},
        {"memory_mb": 32},
        {"memory_mb": 1.5},
        {"pids": 8},
        {"pids": 5000},
        {"tmp_mb": 0},
        {"tmp_mb": 2048},
        {"nofile": 16},
        {"nofile": True},
        {"swap": 1},
        {"memory_mb": 128, "tmp_mb": 128},
    ],
)
def test_limits_are_bounded(limits: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        InstanceConfig.model_validate({"plugin_defaults": {"limits": limits}})
    with pytest.raises(ValidationError):
        InstanceConfig.model_validate({"plugins": {"jail": {"limits": limits}}})


def test_a_plugin_override_is_checked_merged_with_the_defaults() -> None:
    with pytest.raises(ValidationError, match=r"plugins\.jail\.limits: tmp_mb must be less"):
        InstanceConfig.model_validate({"plugins": {"jail": {"limits": {"memory_mb": 64}}}})


def test_mount_roots_are_absolute_normalised_paths() -> None:
    ContainerConfig.model_validate({"mount_roots": ["/srv/vaults"]})
    for bad in (["relative"], ["/srv/../etc"], ["/"], ["/srv/vaults/"]):
        with pytest.raises(ValidationError):
            ContainerConfig.model_validate({"mount_roots": bad})


def test_unset_limits_leave_the_config_digest_as_it_was(instance: Path) -> None:
    lock = Lockfile.model_validate({"schema_version": 1})
    plain = InstanceConfig.model_validate({"plugins": {"jail": {"runtime": "container"}}})
    limited = InstanceConfig.model_validate(
        {"plugins": {"jail": {"runtime": "container", "limits": {"pids": 64}}}}
    )
    digest = Instance(instance, plain, {}, lock).config_digest()
    assert digest != Instance(instance, limited, {}, lock).config_digest()
    jail = {"startup_timeout_s": None, "call_timeout_s": None, "runtime": "container"}
    before: dict[str, Any] = {"models": {}, "plugins": {"jail": jail}}
    assert digest == sha256_hex(before)  # as recorded before #51 added `limits`


# --- the sweep of writable paths --------------------------------------------------------------


def _mount(path: Path, writable: bool = True) -> Mount:
    status = os.lstat(path)
    return Mount(str(path), writable, (status.st_dev, status.st_ino))


def test_the_sweep_undoes_what_the_plugin_left_in_its_writable_paths(
    instance: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    vault, inbox = instance / "vault", instance / "vault" / "inbox"
    (inbox / "old-link").symlink_to("/etc/hostname")  # the user's own, older: kept
    old_suid = inbox / "old-tool"
    old_suid.write_text("#!/bin/sh\n", encoding="utf-8")
    old_suid.chmod(0o4755)
    time.sleep(0.2)
    since = mounts.now()
    (inbox / "escape").symlink_to("/etc/passwd")
    (inbox / "climb").symlink_to("../note.md")  # into the read-only parent
    (inbox / "inner").symlink_to("deep")
    (inbox / "absolute-inner").symlink_to(inbox / "deep")
    os.mkfifo(inbox / "fifo")
    setuid = inbox / "deep" / "tool"
    setuid.write_text("#!/bin/sh\n", encoding="utf-8")
    setuid.chmod(0o6755)
    plan = [_mount(vault, writable=False), _mount(inbox)]
    with caplog.at_level("WARNING", logger="horae.plugins.host.mounts"):
        swept = sweep("jail", plan, since)
    findings = swept.findings
    assert swept.failures == []
    assert not os.path.lexists(inbox / "escape")
    assert not os.path.lexists(inbox / "climb")
    assert not os.path.lexists(inbox / "fifo")
    assert os.path.islink(inbox / "inner")
    assert os.path.islink(inbox / "absolute-inner")
    assert stat.S_IMODE(os.stat(setuid).st_mode) == 0o755
    assert os.path.islink(inbox / "old-link")  # older than the run: never touched
    assert os.stat(old_suid).st_mode & stat.S_ISUID
    assert len(findings) == 4, findings
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 4


def test_the_sweep_leaves_read_only_paths_and_reports_a_replaced_path(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    archive, inbox = instance / "vault" / "archive", instance / "vault" / "inbox"
    plan = [_mount(archive, writable=False), _mount(inbox)]
    since = mounts.now()
    (archive / "planted").symlink_to("/etc/passwd")  # not the plugin's: read-only for it
    os.rename(inbox, instance / "vault" / "moved")
    (instance / "vault" / "inbox").mkdir()
    swept = sweep("jail", plan, since)
    assert os.path.islink(archive / "planted")
    replaced = [f"declared path {str(inbox)!r} was replaced or removed"]
    assert swept.findings == swept.failures == replaced  # fail closed: quarantined


def test_the_sweep_fails_closed_at_its_bounds(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    inbox = instance / "vault" / "inbox"
    for index in range(5):
        (inbox / f"old-{index}.md").write_text("x", encoding="utf-8")
    time.sleep(0.2)
    since = mounts.now()
    for index in range(5):
        (inbox / f"new-{index}.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mounts, "NEW_LIMIT", 5)  # only new entries count: 5 fit
    assert sweep("jail", [_mount(inbox)], since).failures == []
    monkeypatch.setattr(mounts, "NEW_LIMIT", 3)
    assert sweep("jail", [_mount(inbox)], since).failures == [
        "more than 3 new entries: not every one was checked"
    ]
    monkeypatch.setattr(mounts, "WALK_LIMIT", 8)
    assert sweep("jail", [_mount(inbox)], mounts.now() + 60).failures == [
        "more than 8 entries: not every one was checked"
    ]


def test_the_sweep_logs_a_flood_as_a_count(
    instance: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    inbox = instance / "vault" / "inbox"
    since = mounts.now()
    for index in range(mounts.REPORTED + 5):
        os.mkfifo(inbox / f"fifo-{index}")
    with caplog.at_level("WARNING", logger="horae.plugins.host.mounts"):
        findings = sweep("jail", [_mount(inbox)], since).findings
    assert len(findings) == mounts.REPORTED + 5
    assert len(caplog.records) == mounts.REPORTED + 1
    assert "5 more such findings" in caplog.records[-1].getMessage()


def test_a_link_into_a_read_only_child_of_a_writable_path_is_removed(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    vault, archive = instance / "vault", instance / "vault" / "archive"
    (archive / "kept.md").write_text("x", encoding="utf-8")
    plan = plan_mounts("jail", [str(archive)], [str(vault)], _policy(instance))
    since = mounts.now()
    (vault / "into-archive").symlink_to(archive / "kept.md")  # a host writer would edit it
    (vault / "inbox" / "sibling").symlink_to("../note.md")  # still in its writable path
    (archive / "users-own").symlink_to("/etc/hostname")  # not the plugin's: read-only for it
    findings = sweep("jail", plan, since).findings
    assert not os.path.lexists(vault / "into-archive")
    assert os.path.islink(vault / "inbox" / "sibling")
    assert os.path.islink(archive / "users-own")
    assert len(findings) == 1, findings
    assert mounts.writable_at(str(vault / "inbox"), plan)
    assert not mounts.writable_at(str(archive / "kept.md"), plan)
    assert not mounts.writable_at(str(instance / "state"), plan)


def test_the_sweep_checks_a_writable_mount_itself(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mounts, "CTIME_SLACK_S", 0.05)  # coarse kernel timestamps
    note = instance / "vault" / "note.md"
    plan = plan_mounts("jail", [], [str(note)], _policy(instance))
    since = mounts.now()
    note.chmod(0o4755)  # what a malicious plugin would do to its writable file
    swept = sweep("jail", plan, since)
    assert not note.stat().st_mode & stat.S_ISUID
    assert swept.failures == []
    assert len(swept.findings) == 1


class _Entry:
    """A directory entry whose `stat` reports another device."""

    def __init__(self, path: str, device: int) -> None:
        self.path = path
        self._device = device

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        values = list(os.lstat(self.path))
        values[stat.ST_DEV] = self._device
        return os.stat_result(values)


def test_an_entry_on_another_device_fails_the_sweep_unless_it_is_a_mount_point(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = instance / "vault" / "inbox"
    other = str(inbox / "deep")
    real_scandir = os.scandir

    def scandir(path: Any) -> Any:
        if str(path) == str(inbox):
            return iter([_Entry(other, os.lstat(inbox).st_dev + 1)])
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    swept = sweep("jail", [_mount(inbox)], mounts.now(), points=frozenset())
    assert swept.failures == [f"{other!r} is on another device (not a mount)"]
    assert sweep("jail", [_mount(inbox)], mounts.now(), points=frozenset({other})).failures == []


def test_a_directory_the_sweep_cannot_list_fails_it(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = instance / "vault" / "inbox"

    def scandir(path: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", scandir)
    swept = sweep("jail", [_mount(inbox)], mounts.now())
    assert swept.failures == [f"could not list {str(inbox)!r} (Permission denied)"]


def test_mount_points_are_read_from_mountinfo() -> None:
    points = mounts.mount_points()
    if os.path.exists("/proc/self/mountinfo"):
        assert "/" in points
    else:
        assert points == frozenset()


def test_quarantine_refuses_the_next_start_until_its_file_is_removed(instance: Path) -> None:
    policy = _policy(instance)
    mounts.check_quarantine("jail", policy)
    assert mounts.quarantine("jail", policy, ["could not list 'x'"])
    with pytest.raises(PluginCallError, match="quarantined"):
        mounts.check_quarantine("jail", policy)
    mounts.check_quarantine("other", policy)
    (instance / "state" / "quarantine" / "jail").unlink()
    mounts.check_quarantine("jail", policy)
    assert not mounts.quarantine("jail", MountPolicy(roots=(), sensitive=()), ["x"])


def test_a_recheck_refuses_a_path_replaced_since_it_was_checked(instance: Path) -> None:
    policy = _policy(instance)
    inbox = instance / "vault" / "inbox"
    plan = plan_mounts("jail", [str(inbox)], [], policy)
    mounts.recheck("jail", plan, policy)
    os.rename(inbox, instance / "vault" / "moved")
    inbox.symlink_to(instance / "state")  # swapped for a link
    with pytest.raises(PluginCallError, match="symbolic link"):
        mounts.recheck("jail", plan, policy)
    inbox.unlink()
    inbox.mkdir()  # swapped for another directory
    with pytest.raises(PluginCallError, match="replaced since it was checked"):
        mounts.recheck("jail", plan, policy)


def test_a_path_inside_another_plugins_writable_path_is_refused(instance: Path) -> None:
    vault, inbox = str(instance / "vault"), str(instance / "vault" / "inbox")
    policy = _policy(instance)
    with pytest.raises(PluginCallError, match="which plugin writer may write"):
        plan_mounts("jail", [inbox], [], policy, [("writer", vault)])
    # The same path, or one holding the other's (sharing data): it cannot swap this one.
    assert plan_mounts("jail", [vault], [], policy, [("writer", vault)])
    assert plan_mounts("jail", [vault], [], policy, [("writer", inbox)])
    assert plan_mounts("jail", [inbox], [], policy, [("jail", vault)])  # its own


def test_the_started_container_must_have_exactly_the_planned_mounts() -> None:
    plan = [Mount("/srv/vault", False, (1, 2)), Mount("/srv/vault/inbox", True, (1, 3))]

    def bind(source: str, target: str, rw: bool) -> dict[str, Any]:
        return {"Type": "bind", "Source": source, "Destination": target, "RW": rw}

    good = [
        bind("/srv/i/plugins/jail", "/plugin", False),
        bind("/srv/vault", "/srv/vault", False),
        bind("/srv/vault/inbox", "/srv/vault/inbox", True),
    ]
    assert mounts.mounts_problem(plan, good, "/plugin") is None
    assert mounts.mounts_problem(plan, None, "/plugin") == "its mounts cannot be read"
    assert mounts.mounts_problem(plan, good[:2], "/plugin") is not None
    other = [*good[:2], bind("/home/u/.ssh", "/srv/vault/inbox", True)]
    assert "not mounted from itself" in str(mounts.mounts_problem(plan, other, "/plugin"))
    writable = [good[0], bind("/srv/vault", "/srv/vault", True), good[2]]
    assert "with its declared access" in str(mounts.mounts_problem(plan, writable, "/plugin"))
    volume = [*good, {"Type": "volume", "Destination": "/data"}]
    assert "not a bind mount" in str(mounts.mounts_problem(plan, volume, "/plugin"))


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_an_alias_of_another_plugins_writable_path_is_recognised(instance: Path) -> None:
    vault, inbox = instance / "vault", str(instance / "vault" / "inbox")
    alias = instance / "alias"
    alias.symlink_to(vault)
    with pytest.raises(PluginCallError, match="which plugin writer may write"):
        plan_mounts("jail", [inbox], [], _policy(instance), [("writer", str(alias))])


def test_a_case_variant_of_another_plugins_writable_path_is_recognised(instance: Path) -> None:
    if not _case_insensitive(instance):
        pytest.skip("the temporary filesystem is case-sensitive")
    variant = str(instance / "VAULT")
    inbox = str(instance / "vault" / "inbox")
    with pytest.raises(PluginCallError, match="which plugin writer may write"):
        plan_mounts("jail", [inbox], [], _policy(instance), [("writer", variant)])


def test_another_plugins_writable_path_that_cannot_be_read_refuses(instance: Path) -> None:
    inbox = str(instance / "vault" / "inbox")
    missing = str(instance / "gone")
    with pytest.raises(PluginCallError, match="cannot be read"):
        plan_mounts("jail", [inbox], [], _policy(instance), [("writer", missing)])


def test_a_case_variant_of_a_sensitive_path_is_refused(instance: Path) -> None:
    if not _case_insensitive(instance):
        pytest.skip("the temporary filesystem is case-sensitive")
    policy = _policy(instance)
    assert "which no plugin may see" in _refused(str(instance / "STATE"), policy)
    assert "which no plugin may see" in _refused(str(instance / "Plugins" / "x"), policy)
    assert check_path("jail", str(instance / "VAULT"), policy)  # not sensitive in any case
