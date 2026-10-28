# SPDX-License-Identifier: Apache-2.0
"""First-party fixture plugins shipped with the core (PLAN.md M1, #25): examples and tests.

- `fixture-source` (`horae.plugins.builtin.fixture_source`): serves deterministic synthetic
  data from a JSON fixture file, and proposes the intents the file lists;
- `file-sink` (`horae.plugins.builtin.file_sink`): applies `note.*` intents to a local vault,
  the core computing every note's bytes (`horae.plugins.host.sink`).

Each is a stdio MCP server built with the `mcp` SDK, with its `plugin.yaml` and `README.md`
next to it. The packaged manifest runs `python3 -m <module>`; the plugin host looks a bare
command up on the child's fixed `PATH` (`/usr/local/bin:/usr/bin:/bin`), where `python3` does
not have Horae installed. Until M4 installs plugins into their own environments (#44, #46),
`install(directory)` writes each manifest to `<directory>/<name>/plugin.yaml` with the
command pinned to an interpreter that has Horae (default: the running one), the layout
`HostRegistry.from_directory` reads.
"""

import contextlib
import errno
import os
import sys
from pathlib import Path
from typing import Any

import yaml

MANIFEST = "plugin.yaml"
PACKAGE_DIR = Path(__file__).resolve().parent
BUILTIN: dict[str, str] = {
    "fixture-source": "horae.plugins.builtin.fixture_source",
    "file-sink": "horae.plugins.builtin.file_sink",
}
"""Plugin name to module (`python -m <module>` runs its server)."""
VERSION = "0.1.0"
"""The version of both plugins (pinned in `horae.lock`)."""


def plugin_dir(name: str) -> Path:
    """The packaged directory of plugin `name` (its `plugin.yaml`, `README.md`, server)."""
    return PACKAGE_DIR / BUILTIN[name].rsplit(".", 1)[1]


class InstallError(Exception):
    """The built-in plugins cannot be installed there (the message says why)."""


def _interpreter(python: str | None) -> str:
    interpreter = python or sys.executable
    if not interpreter:
        raise InstallError(
            "no interpreter to run the built-in plugins: sys.executable is empty "
            "(pass python=<absolute path of an interpreter that has Horae>)"
        )
    return interpreter


def manifest_data(name: str, *, python: str | None = None) -> dict[str, Any]:
    """Plugin `name`'s packaged manifest, its command run by `python` (default: this
    interpreter, `sys.executable`; `InstallError` when both are empty)."""
    data: dict[str, Any] = yaml.safe_load(
        (plugin_dir(name) / "plugin.yaml").read_text(encoding="utf-8")
    )
    data["runtime"]["command"] = [_interpreter(python), "-m", BUILTIN[name]]
    return data


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_dir(name: str, parent: int | None, what: Path) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise InstallError(f"{what} is a symbolic link or not a directory") from None
        raise InstallError(f"{what} cannot be opened ({error.strerror})") from None


def _write_manifest(fd: int, text: str, what: Path) -> None:
    """`plugin.yaml` in directory `fd`, mode 0644: a temporary file, then `os.replace`."""
    temporary = f".plugin.yaml.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        out = os.open(temporary, flags, 0o644, dir_fd=fd)
    except OSError as error:
        raise InstallError(f"{what} cannot be written ({error.strerror})") from None
    try:
        try:
            os.fchmod(out, 0o644)  # whatever the umask
            data = memoryview(text.encode("utf-8"))
            while data:
                data = data[os.write(out, data) :]
            os.fsync(out)
        finally:
            os.close(out)
        os.replace(temporary, MANIFEST, src_dir_fd=fd, dst_dir_fd=fd)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=fd)
        raise InstallError(f"{what} cannot be written ({error.strerror})") from None


def install(directory: Path, *, python: str | None = None) -> dict[str, Path]:
    """Write `<directory>/<name>/plugin.yaml` (mode 0644) for every built-in plugin (see the
    module docstring); returns each plugin's directory.

    `directory` is created if needed. Neither it nor a plugin directory may be a symbolic
    link, and a manifest is written to a temporary file then renamed over `plugin.yaml`
    (replacing a symbolic link there, never following it), all relative to directory
    descriptors. Raises `InstallError`."""
    interpreter = _interpreter(python)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise InstallError(f"{directory} cannot be created ({error.strerror})") from None
    top = _open_dir(str(directory), None, directory)
    installed: dict[str, Path] = {}
    try:
        for name in sorted(BUILTIN):
            root = directory / name
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=top)
            fd = _open_dir(name, top, root)
            try:
                text = yaml.safe_dump(manifest_data(name, python=interpreter), sort_keys=False)
                _write_manifest(fd, text, root / MANIFEST)
            finally:
                os.close(fd)
            installed[name] = root
    finally:
        os.close(top)
    return installed


__all__ = [
    "BUILTIN",
    "PACKAGE_DIR",
    "VERSION",
    "InstallError",
    "install",
    "manifest_data",
    "plugin_dir",
]
