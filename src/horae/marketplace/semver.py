# SPDX-License-Identifier: Apache-2.0
"""Semantic Versioning 2.0.0 precedence for the versions of an index (`SemVer` contract)."""

from collections.abc import Iterable
from functools import cmp_to_key


def _parse(text: str) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    """`((major, minor, patch), pre-release identifiers)` of a string that already matches
    the `SemVer` contract; build metadata is dropped."""
    core, _, _build = text.partition("+")
    core, _, pre = core.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return (major, minor, patch), tuple(pre.split(".")) if pre else ()


def is_prerelease(text: str) -> bool:
    return bool(_parse(text)[1])


def _cmp_pre(mine: tuple[str, ...], theirs: tuple[str, ...]) -> int:
    if mine == theirs:
        return 0
    if not mine or not theirs:  # a release is higher than any of its pre-releases
        return 1 if not mine else -1
    for left, right in zip(mine, theirs, strict=False):
        if left == right:
            continue
        if left.isdigit() and right.isdigit():
            return -1 if int(left) < int(right) else 1
        if left.isdigit() != right.isdigit():  # numeric identifiers are lower
            return -1 if left.isdigit() else 1
        return -1 if left < right else 1
    return -1 if len(mine) < len(theirs) else 1  # more fields are higher


def compare(left: str, right: str) -> int:
    """-1, 0 or 1 by semver.org §11 precedence (build metadata is ignored, so `1.0.0+a` and
    `1.0.0+b` compare equal)."""
    (left_core, left_pre), (right_core, right_pre) = _parse(left), _parse(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    return _cmp_pre(left_pre, right_pre)


def _total(left: str, right: str) -> int:
    return compare(left, right) or (left > right) - (left < right)


def highest(versions: Iterable[str], *, allow_prerelease: bool = False) -> str | None:
    """The highest version (ties on build metadata broken by the text, so the choice is
    deterministic), skipping pre-releases unless `allow_prerelease`; `None` if none is left."""
    candidates = [v for v in versions if allow_prerelease or not is_prerelease(v)]
    return max(candidates, key=cmp_to_key(_total)) if candidates else None


def sorted_desc(versions: Iterable[str]) -> list[str]:
    """The versions, highest first (the same total order as `highest`)."""
    return sorted(versions, key=cmp_to_key(_total), reverse=True)
