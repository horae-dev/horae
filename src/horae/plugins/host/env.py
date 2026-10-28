# SPDX-License-Identifier: Apache-2.0
"""The environment of a plugin process, built from scratch (#24, PR #85 answer 3).

A child gets exactly the base below plus its declared `permissions.secrets`; nothing is
copied from `os.environ`, and Horae never puts a resolved secret into its own environment
(the child's mapping is built here and handed to the spawn call only).

| Variable | Value |
|---|---|
| `PATH` | `BASE_PATH` (`/usr/local/bin:/usr/bin:/bin`), never the host's |
| `HOME` | an empty per-run directory (never the user's home and its dotfile credentials) |
| `TMPDIR` | an empty per-run directory |
| `LANG` | `C.UTF-8` |
| `TZ` | the instance time zone (`horae.yaml` `timezone`) |

**Deny list** (schemas decisions 8 and 28): a declared secret may not be one of the base keys or
a variable that changes how a runtime behaves: `HORAE_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `LC_*`,
`GIT_*`, `SSL_CERT_*`, `*_PROXY`, `NODE_OPTIONS`, `NODE_PATH`, `BASH_ENV`, `ENV`, `PERL5OPT`,
`RUBYOPT`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `IFS` (compared case-insensitively). Such a
plugin is refused at spawn with `env_unsafe`; a value with a NUL character too.
"""

from collections.abc import Mapping
from pathlib import Path

from horae.plugins.host.errors import PluginCallError
from horae.secrets import Secret

BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
"""The child's `PATH` (M4: the plugin venv's `bin` goes first)."""
BASE_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "TZ"})

_DENIED = BASE_KEYS | {
    "NODE_OPTIONS",
    "NODE_PATH",
    "BASH_ENV",
    "ENV",
    "PERL5OPT",
    "RUBYOPT",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "IFS",
}
_DENIED_PREFIXES = ("HORAE_", "LD_", "DYLD_", "PYTHON", "LC_", "GIT_", "SSL_CERT_")
_DENIED_SUFFIXES = ("_PROXY",)


def unsafe_name(name: str) -> bool:
    """Whether `name` may not be passed to a plugin as a secret."""
    upper = name.upper()
    return (
        upper in _DENIED
        or upper.startswith(_DENIED_PREFIXES)
        or upper.endswith(_DENIED_SUFFIXES)
        or "=" in name
        or "\x00" in name
        or not name
    )


def base_env(*, home: Path, tmpdir: Path, timezone: str) -> dict[str, str]:
    """The fixed part of every child environment."""
    return {
        "PATH": BASE_PATH,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "LANG": "C.UTF-8",
        "TZ": timezone,
    }


def check_names(plugin: str, names: list[str]) -> None:
    """`env_unsafe` if one of the declared secret `names` is on the deny list."""
    bad = sorted(name for name in names if unsafe_name(name))
    if bad:
        raise PluginCallError(
            "env_unsafe",
            f"plugin {plugin} declares secrets that may not be passed to a process: "
            + ", ".join(bad),
        )


def build_env(
    plugin: str,
    *,
    home: Path,
    tmpdir: Path,
    timezone: str,
    secrets: Mapping[str, Secret],
) -> dict[str, str]:
    """The child environment: `base_env` plus each declared secret, revealed here only."""
    check_names(plugin, list(secrets))
    env = base_env(home=home, tmpdir=tmpdir, timezone=timezone)
    for name, secret in secrets.items():
        value = secret.reveal()
        if "\x00" in value:
            del value
            raise PluginCallError("env_unsafe", f"secret {name} contains a NUL character")
        env[name] = value
    return env


__all__ = ["BASE_KEYS", "BASE_PATH", "base_env", "build_env", "check_names", "unsafe_name"]
