# SPDX-License-Identifier: Apache-2.0
"""A plugin that is not first-party can never poison what a first-party build imports (#191,
schemas decision 79 I), after the review of PR #192.

The attack: a third-party `uv.lock` names a build backend (`flit-core` here) under the real
index, but its wheel URL on another host with that host's hash. uv would fetch it and cache it
under the index; a later first-party build resolves its backend from the index, unhashed, finds
the same file name in the cache and imports the attacker's code on the host. Two local HTTP
servers (the index and the "attacker"), synthetic names, no network."""

import base64
import functools
import hashlib
import http.server
import re
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from horae.marketplace import InstallRefused, PluginEnvironment
from horae.marketplace import environment as environment_module
from horae.marketplace.environment import check_lock
from venv_fixtures import UV, pyproject

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")
WHEEL = "flit_core-3.9.0-py3-none-any.whl"

GOOD_BACKEND = """\
import base64, hashlib, os, tomllib, zipfile


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with open("pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    dist, version = project["name"].replace("-", "_"), project["version"]
    name = f"{dist}-{version}-py3-none-any.whl"
    info = f"{dist}-{version}.dist-info"
    files = {}
    for root, _dirs, names in os.walk("src"):
        for entry in names:
            path = os.path.join(root, entry)
            with open(path, "rb") as handle:
                files[os.path.relpath(path, "src")] = handle.read()
    meta = f"Metadata-Version: 2.1\\nName: {dist}\\nVersion: {version}\\n"
    files[f"{info}/METADATA"] = meta.encode()
    tag = "Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    files[f"{info}/WHEEL"] = tag.encode()
    record = []
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{path},sha256={digest.decode()},{len(data)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\\n".join(record) + "\\n")
    return name
"""


def _wheel(path: Path, backend: str) -> str:
    """A `flit-core` wheel whose `flit_core.buildapi` is `backend`; returns its sha256."""
    info = "flit_core-3.9.0.dist-info"
    files = {
        "flit_core/__init__.py": "",
        "flit_core/buildapi.py": backend,
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: flit-core\nVersion: 3.9.0\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    record = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            data = text.encode()
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{name},sha256={digest.decode()},{len(data)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """`index`: a PEP 503 index serving the good wheel; `evil`: another host serving a wheel
    of the same file name whose backend writes `marker` when imported."""
    marker = tmp_path / "PWNED"
    good_root, evil_root = tmp_path / "index-root", tmp_path / "evil-root"
    good = _wheel(good_root / "files" / WHEEL, GOOD_BACKEND)
    evil = _wheel(evil_root / WHEEL, f"open({str(marker)!r}, 'w').close()\n" + GOOD_BACKEND)
    running = []
    for root in (good_root, evil_root):
        handler = functools.partial(_Quiet, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        running.append(server)
    index = f"http://127.0.0.1:{running[0].server_address[1]}/simple"
    project = good_root / "simple" / "flit-core"
    project.mkdir(parents=True)
    link = f"http://127.0.0.1:{running[0].server_address[1]}/files/{WHEEL}#sha256={good}"
    (project / "index.html").write_text(f'<html><body><a href="{link}">{WHEEL}</a></body></html>')
    (good_root / "simple" / "index.html").write_text(
        '<html><body><a href="flit-core/">flit-core</a></body></html>'
    )
    evil_url = f"http://127.0.0.1:{running[1].server_address[1]}/{WHEEL}"
    yield {"index": index, "evil_url": evil_url, "evil_hash": evil, "marker": marker}
    for server in running:
        server.shutdown()
        server.server_close()


def _tree(root: Path, name: str, text: str, index: str, cache: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(text, encoding="utf-8")
    (root / "src" / name).mkdir(parents=True)
    (root / "src" / name / "__init__.py").write_text("", encoding="utf-8")
    assert UV is not None
    done = subprocess.run(  # noqa: S603
        [UV, "lock", "--python", sys.executable, "--default-index", index],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root.parent),
            "UV_NO_CONFIG": "1",
            "UV_CACHE_DIR": str(cache),
            "UV_PYTHON_DOWNLOADS": "never",
        },
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    return root


def _evil_lock(tree: Path, url: str, digest: str) -> None:
    """Point the locked `flit-core` wheel at the attacker's host, with its hash."""
    lock = tree / "uv.lock"
    text = re.sub(r'url = "[^"]*flit_core[^"]*"', f'url = "{url}"', lock.read_text())
    text = re.sub(
        r'(url = "' + re.escape(url) + r'", hash = "sha256:)[0-9a-f]{64}', rf"\g<1>{digest}", text
    )
    lock.write_text(text)
    assert url in text
    assert digest in text


FLIT = '[build-system]\nrequires = ["flit_core"]\nbuild-backend = "flit_core.buildapi"\n'


def _scenario(tmp_path: Path, servers: dict[str, Any]) -> tuple[PluginEnvironment, Path, Path]:
    shared = tmp_path / "state" / "uv-cache"
    lock_cache = tmp_path / "lock-cache"
    third = _tree(
        tmp_path / "third",
        "beta",
        pyproject("beta", "1.0.0", ["flit-core==3.9.0"], build=FLIT),
        servers["index"],
        lock_cache,
    )
    _evil_lock(third, servers["evil_url"], servers["evil_hash"])
    first = _tree(
        tmp_path / "first",
        "alpha",
        pyproject("alpha", "1.0.0", [], build=FLIT),
        servers["index"],
        lock_cache,
    )
    return PluginEnvironment(shared, index_url=servers["index"]), third, first


@needs_uv
def test_a_lock_naming_another_host_for_an_index_package_is_refused(
    tmp_path: Path, servers: dict[str, Any]
) -> None:
    environment, third, first = _scenario(tmp_path, servers)
    for trusted in (False, True):  # first-party locks are held to the same rule
        with pytest.raises(InstallRefused, match="not its registry's"):
            check_lock(third, "beta", "beta", trusted=trusted, index_url=servers["index"])
    with pytest.raises(InstallRefused, match="not its registry's"):
        PluginEnvironment(
            environment.cache_dir, index_url=servers["index"], install_project=False
        ).prepare(third, "beta")
    environment.prepare(first, "alpha")  # the first-party build uses the good backend
    assert not servers["marker"].exists()


@needs_uv
def test_a_third_party_install_never_shares_the_cache_with_a_first_party_build(
    tmp_path: Path, servers: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's proof of concept with the host check taken out: the evil wheel is
    fetched, but into the third-party install's own cache, deleted after; the first-party
    build on the same instance gets the index's wheel, and the marker is never written."""
    environment, third, first = _scenario(tmp_path, servers)
    monkeypatch.setattr(environment_module, "_artifact_problem", lambda *a: "")
    isolated = PluginEnvironment(
        environment.cache_dir, index_url=servers["index"], install_project=False
    )
    isolated.prepare(third, "beta")  # the evil flit-core is installed in beta's own venv
    assert not servers["marker"].exists()
    environment.prepare(first, "alpha")
    assert not servers["marker"].exists()


@pytest.mark.parametrize(
    ("registry", "file", "ok"),
    [
        ("https://pypi.org/simple", "https://files.pythonhosted.org/packages/a/b/x.whl", True),
        ("https://pypi.org/simple", "https://pypi.org/packages/x.whl", True),
        ("https://pypi.org/simple", "https://files.pythonhosted.org/other/x.whl", False),
        ("https://pypi.org/simple", "http://files.pythonhosted.org/packages/x.whl", False),
        ("https://pypi.org/simple", "https://evil.example.test/packages/x.whl", False),
        ("https://pypi.org/simple", "https://u:p@pypi.org/packages/x.whl", False),
        ("https://pypi.org/simple", "https://169.254.169.254/latest", False),
        ("https://pypi.example.test/simple", "https://pypi.example.test:8443/x.whl", False),
        ("http://pypi.example.test/simple", "http://pypi.example.test/x.whl", False),
    ],
)
def test_an_index_file_must_come_from_its_index(registry: str, file: str, ok: bool) -> None:
    problem = environment_module._artifact_problem(registry, {"url": file}, None)
    assert (problem == "") is ok, problem


def test_a_third_party_lock_must_use_the_default_index(tmp_path: Path) -> None:
    head = 'version = 1\n[[package]]\nname = "beta"\nsource = { editable = "." }\n'
    entry = (
        '[[package]]\nname = "x"\nsource = {{ registry = "{index}" }}\n'
        'wheels = [{{ url = "{index}/x.whl", hash = "sha256:' + "b" * 64 + '" }}]\n'
    )
    for index, ok in (("https://pypi.org/simple", True), ("https://mirror.example.test", False)):
        (tmp_path / "uv.lock").write_text(head + entry.format(index=index))
        assert check_lock(tmp_path, "beta", "beta") == ["x"]  # first-party: any https index
        if ok:
            assert check_lock(tmp_path, "beta", "beta", trusted=False) == ["x"]
        else:
            with pytest.raises(InstallRefused, match="another index"):
                check_lock(tmp_path, "beta", "beta", trusted=False)
    url = (
        '[[package]]\nname = "x"\nsource = { url = "https://example.test/x.whl" }\n'
        'wheels = [{ hash = "sha256:' + "b" * 64 + '" }]\n'
    )
    (tmp_path / "uv.lock").write_text(head + url)
    with pytest.raises(InstallRefused, match="direct URL"):
        check_lock(tmp_path, "beta", "beta", trusted=False)
