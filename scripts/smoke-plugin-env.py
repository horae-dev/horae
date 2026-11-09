# SPDX-License-Identifier: Apache-2.0
"""Smoke test of the install-time uv inside the image (#169), run by smoke-image.sh.

Runs in the container (read-only root, no network, user 10001) with `tests/` mounted read-only at
/smoke/tests. Builds a plugin tree and a local `file://` package index on the instance volume
from the unit tests' fixtures, locks the plugin offline with the image's uv, then builds its venv
with `PluginEnvironment` exactly as `horae plugin add` does (uv's cache under `<state>/uv-cache`),
and checks the venv: linked to the image's own Python, runnable offline without uv, and still
working once the tree is renamed.
Usage: smoke-plugin-env.py WORKDIR   (a writable directory)
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/smoke/tests")

from horae.contracts import PluginManifest
from horae.marketplace import PluginEnvironment
from horae.plugins.host.env import OFFLINE_ENV, VENV_DIR
from venv_fixtures import UV, local_index, lock, venv_manifest, venv_plugin_files


def check(condition: bool, message: object) -> None:
    if not condition:
        sys.exit(f"FAIL: {message}")


work = Path(sys.argv[1])
check(UV == "/usr/local/bin/uv", f"uv is {UV}")
index = local_index(work / "index")
tree = work / "scratch" / "demo"
for path, text in venv_plugin_files("demo", "1.0.0", "1.0.0").items():
    (tree / path).parent.mkdir(parents=True, exist_ok=True)
    (tree / path).write_text(text, encoding="utf-8")
lock(tree, index, work / "lock-cache")

cache = work / "state" / "uv-cache"
manifest = PluginManifest.model_validate(venv_manifest("demo", "1.0.0"))
PluginEnvironment(cache, index_url=index)(tree, manifest)

venv = tree / VENV_DIR
cfg = (venv / "pyvenv.cfg").read_text()
check("home = /usr/local/bin" in cfg, cfg)
check(
    (venv / "bin" / "python").resolve() == Path("/usr/local/bin/python3.12"),
    "the venv is not linked to the image's python",
)
check(any(cache.iterdir()), "uv wrote nothing to the instance cache")

moved = work / "plugins" / "demo"
moved.parent.mkdir(parents=True)
tree.rename(moved)
env = {"PATH": f"{moved / VENV_DIR / 'bin'}:/usr/local/bin:/usr/bin:/bin", **OFFLINE_ENV}
python = moved / VENV_DIR / "bin" / "python"
out = subprocess.run(  # noqa: S603
    [
        str(moved / VENV_DIR / "bin" / "python"),
        "-c",
        "import demo, tinydep; print(tinydep.VERSION)",
    ],
    env=env,
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
check(out == "1.0.0", out)
print("ok")
