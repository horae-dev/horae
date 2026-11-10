# SPDX-License-Identifier: Apache-2.0
"""Hardened git for marketplace indexes (#43), after `horae-dev/marketplace`'s verifier.

An index is any git URL the user configures, and a clone is a network read of untrusted data:
only the named protocol is allowed (`https`; tests may name `file`), the user's and the
system's git configuration are ignored, nothing may prompt, nothing inherits the environment
(no `GIT_SSH_COMMAND`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`, ...), and every call has a timeout.
The one exception is a call that reads a remote (an index, a plugin repository): it also
inherits the proxy and CA variables, validated (`network_env`, #153).
No hook ever runs (`core.hooksPath=/dev/null`) and Git LFS never smudges
(`GIT_LFS_SKIP_SMUDGE=1`): a plugin checkout (#44) runs nothing from the repository.
Clones are shallow (the tip of HEAD only) but not partial (`--filter` made a later `git show`
fetch lazily over a protocol the other calls block).
"""

import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

GIT_TIMEOUT_S = 120
"""Seconds one git call may take."""
HTTPS = "https"
_ENV_KEPT = frozenset({"PATH", "HOME", "LANG", "TMPDIR"})
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
_NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
_CA_FILE_VARS = ("SSL_CERT_FILE",)
_CA_DIR_VARS = ("SSL_CERT_DIR",)
_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_NO_PROXY_OK = re.compile(r"[A-Za-z0-9._:*,/\[\] -]*")
MAX_ERROR_CHARS = 300
WATCH_INTERVAL_S = 0.25
"""How often a watched call has the size of its directory measured."""


class GitError(Exception):
    """A git call failed, timed out, or git is missing."""


class GitTooLarge(GitError):
    """A watched git call wrote more than its byte budget; it was killed."""


def git_command(*args: str, allow: str = HTTPS, options: Sequence[str] = ()) -> list[str]:
    """The argv of one git call: every protocol is off except `allow`, no hook runs.
    `options` are global options placed before the subcommand (`-c key=value`, `--git-dir`)."""
    return [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{allow}.allow=always",
        "-c",
        "core.hooksPath=/dev/null",
        *options,
        *args,
    ]


def git_env() -> dict[str, str]:
    """The environment of a git call: a few variables of ours, no config, no prompt."""
    env = {key: value for key, value in os.environ.items() if key in _ENV_KEPT}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
        LC_ALL="C",
    )
    return env


def _bad(var: str, why: str) -> GitError:
    """An error that names the variable, never its value (a proxy URL may be a secret)."""
    return GitError(f"{var} is not usable for a fetch: {why}")


def _proxy_url_ok(value: str) -> bool:
    if not value.isprintable() or value != value.strip() or " " in value:
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return False
    return (
        parts.scheme in _PROXY_SCHEMES
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
        and parts.path in ("", "/")
    )


def _absolute_existing(var: str, value: str, *, directory: bool) -> None:
    kind = "directory" if directory else "file"
    paths = value.split(os.pathsep) if directory else [value]
    for item in paths:
        if "\0" in item or not item or not os.path.isabs(item):
            raise _bad(var, f"it must be an absolute path to a {kind}")
        path = Path(item)
        if not (path.is_dir() if directory else path.is_file()):
            raise _bad(var, f"it must be an existing {kind}")


def network_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The proxy and CA variables a git call that reads a remote may inherit (#153): exactly
    `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (and their lowercase forms), `SSL_CERT_FILE` and
    `SSL_CERT_DIR`, so a home server behind a proxy or with its own CA can fetch.

    Each proxy must be an `http`, `https`, `socks5` or `socks5h` URL with a host and **no
    credentials**; a CA path must be absolute and exist. Anything else is a `GitError` that
    names the variable (never the value: it is not logged). Empty values are dropped."""
    source = os.environ if environ is None else environ
    kept: dict[str, str] = {}
    for var in (*_PROXY_VARS, *_NO_PROXY_VARS, *_CA_FILE_VARS, *_CA_DIR_VARS):
        value = source.get(var, "")
        if not value:
            continue
        if var in _PROXY_VARS:
            if not _proxy_url_ok(value):
                raise _bad(
                    var, "it must be an http(s) or socks5 proxy URL with a host and no credentials"
                )
        elif var in _NO_PROXY_VARS:
            if not _NO_PROXY_OK.fullmatch(value):
                raise _bad(var, "it must be a comma-separated list of host names")
        else:
            _absolute_existing(var, value, directory=var in _CA_DIR_VARS)
        kept[var] = value
    return kept


def tree_bytes(root: Path) -> int:
    """Bytes in the files under `root` (links are not followed); what vanishes is skipped."""
    total, pending = 0, [str(root)]
    while pending:
        try:
            with os.scandir(pending.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def kill_session(proc: "subprocess.Popen[bytes]") -> None:
    """Kill git and the helpers it started (`git-remote-https`, `index-pack`): one session."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    proc.communicate()


def run_git(
    cwd: Path,
    *args: str,
    allow: str = HTTPS,
    timeout_s: float = GIT_TIMEOUT_S,
    options: Sequence[str] = (),
    watch: Path | None = None,
    max_bytes: int | None = None,
    network: bool = False,
) -> str:
    """Run git in `cwd` and return its stripped stdout; `GitError` on any failure.
    With `network` (a call that reads a remote), `network_env()` is added to the environment;
    nothing else about the call changes.
    With `watch` and `max_bytes`, the call is killed (`GitTooLarge`) as soon as the files
    under `watch` exceed `max_bytes`: a download is bounded in size, not only in time."""
    env = git_env()
    if network:
        env.update(network_env())
    try:
        proc = subprocess.Popen(  # noqa: S603
            git_command(*args, allow=allow, options=options),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise GitError(f"cannot run git: {exc.strerror or type(exc).__name__}") from exc
    deadline = time.monotonic() + timeout_s
    watching = watch is not None and max_bytes is not None
    while True:
        left = deadline - time.monotonic()
        try:
            stdout, stderr = proc.communicate(
                timeout=max(0.0, min(WATCH_INTERVAL_S, left)) if watching else max(0.0, left)
            )
            break
        except subprocess.TimeoutExpired:
            pass
        except BaseException:
            kill_session(proc)
            raise
        if watch is not None and max_bytes is not None and tree_bytes(watch) > max_bytes:
            kill_session(proc)
            raise GitTooLarge(f"git {args[0]} wrote more than {max_bytes} bytes")
        if time.monotonic() >= deadline:
            kill_session(proc)
            raise GitError(f"git {args[0]} timed out after {timeout_s:g}s")
    if proc.returncode != 0:
        reason = " ".join(stderr.decode("utf-8", errors="replace").split())[:MAX_ERROR_CHARS]
        raise GitError(f"git {args[0]} failed: {reason}")
    try:
        return stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        # A hostile index file that is not UTF-8 is a failed fetch (the cache stays), not a crash.
        raise GitError(f"git {args[0]} printed output that is not UTF-8") from None
