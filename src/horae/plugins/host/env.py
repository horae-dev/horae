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
| `UV_OFFLINE`, `PIP_NO_INDEX` | `1` (with `UV_PYTHON_DOWNLOADS=never`): no fetch at run (#45) |

A plugin installed with its own venv (`<plugin>/.venv`, #45, decision 69) also gets
`VIRTUAL_ENV=<plugin>/.venv`, and `PATH` starts with `<plugin>/.venv/bin`.

A plugin container (`runtime: container`, #50, schemas decision 76) also gets its egress
proxy's URL in `PROXY_KEYS` (`HTTPS_PROXY`, `HTTP_PROXY`, their lowercase forms and
`HORAE_EGRESS_PROXY`), set by the core only: a secret can never be named so (deny list below).
`NO_PROXY` is never set.

**Deny list** (schemas decisions 8, 28 and 35): a declared secret may not be one of the base
keys or a variable that changes how a runtime, its loader, its TLS stack or its package manager
behaves (compared case-insensitively):

- prefixes `HORAE_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `LC_*`, `GIT_*`, `SSL_CERT_*`, `OPENSSL_*`,
  `MALLOC_*`, `UV_*`, `PIP_*`, `NPM_CONFIG_*`, and the suffix `*_PROXY`;
- `NODE_OPTIONS`, `NODE_PATH`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`,
  `BASH_ENV`, `ENV`, `SHELLOPTS`, `PS4`, `IFS`, `PERL5OPT`, `PERL5LIB`, `PERLLIB`, `RUBYOPT`,
  `RUBYLIB`, `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS`, `CLASSPATH`, `GODEBUG`,
  `GLIBC_TUNABLES`, `LOCPATH`, `GCONV_PATH`, `TZDIR`, `LANGUAGE`, `SSLKEYLOGFILE`,
  `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `VIRTUAL_ENV`.

Such a plugin is refused at spawn with `env_unsafe`; a value with a NUL character too.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from horae.plugins.host.errors import PluginCallError
from horae.secrets import Secret

BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
"""The child's `PATH` (a plugin venv's `bin` goes first, #45)."""
BASE_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "TZ"})
OFFLINE_ENV = {"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "PIP_NO_INDEX": "1"}
"""In every plugin child: a package manager it runs fetches nothing (#45)."""
PLUGIN_KEYS = BASE_KEYS | frozenset(OFFLINE_ENV)
"""The keys of every plugin child's environment, but its secrets and `VIRTUAL_ENV`."""
PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "HORAE_EGRESS_PROXY")
"""The variables a plugin container gets its egress proxy's URL in (decision 76 D)."""
VENV_DIR = ".venv"
"""A plugin's own venv, in its directory (#45, decision 69)."""

_DENIED = BASE_KEYS | {
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "PS4",
    "IFS",
    "PERL5OPT",
    "PERL5LIB",
    "PERLLIB",
    "RUBYOPT",
    "RUBYLIB",
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "CLASSPATH",
    "GODEBUG",
    "GLIBC_TUNABLES",
    "LOCPATH",
    "GCONV_PATH",
    "TZDIR",
    "LANGUAGE",
    "SSLKEYLOGFILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "VIRTUAL_ENV",
}
_DENIED_PREFIXES = (
    "HORAE_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "LC_",
    "GIT_",
    "SSL_CERT_",
    "OPENSSL_",
    "MALLOC_",
    "UV_",
    "PIP_",
    "NPM_CONFIG_",
)
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


def plugin_venv(root: Path | None) -> Path | None:
    """`<root>/.venv` when the plugin has a venv there (its `pyvenv.cfg` and `bin/python`)."""
    if root is None:
        return None
    venv = root / VENV_DIR
    if (venv / "pyvenv.cfg").is_file() and os.path.isfile(venv / "bin" / "python"):
        return venv
    return None


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
    venv: Path | None = None,
    proxy: str | None = None,
) -> dict[str, str]:
    """The child environment: `base_env`, `OFFLINE_ENV`, the plugin's `venv` if any, its
    egress `proxy` URL in `PROXY_KEYS` if any, and each declared secret, revealed here only."""
    check_names(plugin, list(secrets))
    env = base_env(home=home, tmpdir=tmpdir, timezone=timezone) | OFFLINE_ENV
    if venv is not None:
        if ":" in str(venv):
            raise PluginCallError("spawn_failed", f"plugin {plugin}: its venv path holds a ':'")
        env["PATH"] = f"{venv / 'bin'}:{BASE_PATH}"
        env["VIRTUAL_ENV"] = str(venv)
    if proxy is not None:
        env.update(dict.fromkeys(PROXY_KEYS, proxy))
    for name, secret in secrets.items():
        value = secret.reveal()
        if "\x00" in value:
            del value
            raise PluginCallError("env_unsafe", f"secret {name} contains a NUL character")
        env[name] = value
    return env


__all__ = [
    "BASE_KEYS",
    "BASE_PATH",
    "OFFLINE_ENV",
    "PLUGIN_KEYS",
    "PROXY_KEYS",
    "VENV_DIR",
    "base_env",
    "build_env",
    "check_names",
    "plugin_venv",
    "unsafe_name",
]
