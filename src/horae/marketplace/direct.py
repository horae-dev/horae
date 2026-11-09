# SPDX-License-Identifier: Apache-2.0
"""Direct install references (#48): `https://<host>/<path>@<full commit SHA>`.

`horae plugin add` takes either a marketplace name (`name[@version]`) or a git URL pinned to a
commit. A URL is recognised by a `:` or a `/`, which a plugin name never holds, and must be:

- **https** only: `http://`, `file://`, `ssh://`, `git://`, `ext::` and the scp form
  (`git@host:owner/repo`) are refused;
- **without credentials**: user info (`https://user[:token]@host/...`) is refused. No error
  repeats the URL (a token may sit in its user info, query or fragment);
- a `GitRepoUrl` (the contract `horae.lock` stores): a DNS host name (no port, no IP literal,
  no `localhost`), path segments of `A-Z a-z 0-9 . _ -`, none `.` or `..`, no query or
  fragment;
- pinned to a **full commit SHA** after the last `@` (40 lowercase hex digits, or 64 for a
  SHA-256 repository): no tag, branch or abbreviated SHA, so what is approved is one commit.

Errors are `InvalidReference` (a usage error, exit 2).
"""

from pydantic import TypeAdapter, ValidationError

from horae.contracts.lockfile import GitRepoUrl
from horae.contracts.types import CommitSha
from horae.marketplace.errors import InvalidReference

_repo: TypeAdapter[str] = TypeAdapter(GitRepoUrl)
_sha: TypeAdapter[str] = TypeAdapter(CommitSha)
_HTTPS = "https://"
_SHAPE = "expected https://<host>/<owner>/<repo>@<full commit SHA>"


def is_direct(ref: str) -> bool:
    """Whether `ref` is a git URL rather than a marketplace `name[@version]`."""
    return ":" in ref or "/" in ref


def parse_direct(ref: str) -> tuple[str, str]:
    """`https://host/path@sha` -> `(repo, sha)`, or `InvalidReference` saying what is wrong."""
    if not ref.startswith(_HTTPS):
        raise InvalidReference(
            f"a direct install takes an https:// git URL only (no http, file, ssh, git or "
            f"scp-style address): {_SHAPE}"
        )
    authority = ref[len(_HTTPS) :].split("/", 1)[0]
    if "@" in authority:
        raise InvalidReference(
            "credentials in a git URL are refused (the URL is not repeated here): "
            "a plugin repository must be readable without them"
        )
    repo, at, sha = ref.rpartition("@")
    if not at:
        raise InvalidReference(f"a direct install must be pinned to a commit: {_SHAPE}")
    try:
        _sha.validate_python(sha)
    except ValidationError:
        raise InvalidReference(
            f"{sha!r} is not a full commit SHA: a direct install is pinned to the full "
            "40-character (lowercase hex) commit id, never a tag, a branch or a short SHA"
        ) from None
    try:
        _repo.validate_python(repo)
    except ValidationError:
        raise InvalidReference(
            "not an allowed git URL (the URL is not repeated here): https, a DNS host name (no "
            "port, IP address or localhost), path segments of letters, digits, '.', '_' and "
            "'-', no query or fragment"
        ) from None
    return repo, sha


__all__ = ["is_direct", "parse_direct"]
