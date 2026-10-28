# SPDX-License-Identifier: Apache-2.0
"""The eval files of a skill: `evals/<case>.yaml` and `evals/recorded/<backend>/<case>.json`.

Every file is opened with `O_NOFOLLOW` (a symbolic link is refused), must be a regular file
and is read with a size bound. The `evals` and `recorded` folders and each backend folder
must be real directories too (not links). A recording is written atomically: a new temporary
file in the same folder (opened `x`, mode 0644), then a `rename` over the old one.
"""

import contextlib
import errno
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from horae.config.documents import ProblemsError, parse_yaml, validate
from horae.contracts.evals import EvalCase, RecordedResponse
from horae.contracts.instance import LlmBackend
from horae.skills import skill_dir

EVALS_DIR = "evals"
"""The folder of a skill's golden cases: `skills/<namespace>/<skill>/evals/`."""
RECORDED_DIR = "recorded"
"""Under `evals/`: `recorded/<backend>/<case>.json`."""
CASE_SUFFIX = ".yaml"
MAX_CASE_BYTES = 256 * 1024
MAX_RECORDED_BYTES = 512 * 1024
MAX_CASES = 256
"""Most cases per skill."""

BACKENDS: tuple[str, ...] = get_args(LlmBackend)
_CASE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class EvalError(Exception):
    """An eval file that cannot be used (unreadable, invalid); the message names it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class LoadedCase:
    """A case and its recorded responses, by backend."""

    name: str
    case: EvalCase
    recorded: dict[str, RecordedResponse] = field(default_factory=dict)


def evals_dir(plugin_root: Path, skill: str) -> Path:
    """`<plugin>/skills/<namespace>/<skill>/evals`."""
    return skill_dir(plugin_root, skill) / EVALS_DIR


def recorded_path(folder: Path, backend: str, case: str) -> Path:
    """Where the recorded response of `case` for `backend` lives under `folder` (`evals/`)."""
    return folder / RECORDED_DIR / backend / f"{case}.json"


def _is_dir(path: Path) -> bool:
    """A real directory (a symbolic link to one is not)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def read_bounded(path: Path, limit: int) -> bytes | None:
    """The content of the regular file `path` (not a link), `None` when it does not exist;
    `EvalError` when it cannot be read or is larger than `limit` bytes."""
    try:
        fd = os.open(path, _FILE_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise EvalError(f"{path}: a symbolic link (refused)") from None
        raise EvalError(f"{path}: cannot be read ({error.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EvalError(f"{path}: not a regular file")
        data = os.read(fd, limit + 1)
        while data and len(data) <= limit:
            more = os.read(fd, limit + 1 - len(data))
            if not more:
                break
            data += more
    except OSError as error:
        raise EvalError(f"{path}: cannot be read ({error.strerror})") from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise EvalError(f"{path}: larger than {limit} bytes")
    return data


def _text(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EvalError(f"{path}: not valid UTF-8") from None


def _first(error: ProblemsError) -> str:
    first = error.problems[0]
    more = f" (and {len(error.problems) - 1} more)" if len(error.problems) > 1 else ""
    return f"{first}{more}"


def _json_value(value: Any, path: Path, what: str) -> None:
    """`value` must be plain JSON (YAML dates and times are not: quote them)."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise EvalError(
            f"{path}: `{what}` is not plain JSON (quote dates and times in YAML)"
        ) from None


def load_case(path: Path) -> EvalCase:
    """Parse and validate one case file."""
    data = read_bounded(path, MAX_CASE_BYTES)
    if data is None:
        raise EvalError(f"{path}: file not found")
    try:
        case = validate(parse_yaml(_text(data, path), path), EvalCase)
    except ProblemsError as error:
        raise EvalError(_first(error)) from None
    _json_value(case.input, path, "input")
    _json_value(case.expect.model_dump(mode="python"), path, "expect")
    return case


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a number is not finite")
    return value


def load_recorded(path: Path) -> RecordedResponse | None:
    """Parse and validate one recorded response (`None` when there is none)."""
    data = read_bounded(path, MAX_RECORDED_BYTES)
    if data is None:
        return None
    try:
        raw = json.loads(
            _text(data, path),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
            parse_float=_finite,
        )
    except RecursionError:
        raise EvalError(f"{path}: nests too deeply") from None
    except ValueError as error:
        raise EvalError(f"{path}: not valid JSON ({str(error)[:200]})") from None
    try:
        return RecordedResponse.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        where = "/".join(str(part) for part in first["loc"]) or "/"
        raise EvalError(f"{path}: {where}: {first['msg']}") from None


def case_names(folder: Path) -> list[str]:
    """The case names of `folder` (`evals/`), sorted; none when it does not exist."""
    if not _is_dir(folder):
        if folder.exists() or folder.is_symlink():
            raise EvalError(f"{folder}: not a directory (or a symbolic link)")
        return []
    names = []
    for entry in sorted(folder.iterdir()):
        if entry.suffix != CASE_SUFFIX:
            continue
        if not _CASE_NAME.fullmatch(entry.stem):
            raise EvalError(f"{entry}: not a case name (lower case, digits, `_` and `-`)")
        names.append(entry.stem)
    if len(names) > MAX_CASES:
        raise EvalError(f"{folder}: more than {MAX_CASES} cases")
    return names


def load_cases(folder: Path, only: str | None = None) -> list[LoadedCase]:
    """Every case of `folder` (`evals/`), or only `only`, with its recorded responses."""
    names = case_names(folder)
    if only is not None:
        names = [name for name in names if name == only]
    recorded_root = folder / RECORDED_DIR
    backends: list[str] = []
    if _is_dir(recorded_root):
        for entry in sorted(recorded_root.iterdir()):
            if entry.name not in BACKENDS or not _is_dir(entry):
                raise EvalError(f"{entry}: not a backend folder ({', '.join(BACKENDS)})")
            backends.append(entry.name)
    elif recorded_root.exists() or recorded_root.is_symlink():
        raise EvalError(f"{recorded_root}: not a directory (or a symbolic link)")
    loaded = []
    for name in names:
        case = load_case(folder / f"{name}{CASE_SUFFIX}")
        recorded: dict[str, RecordedResponse] = {}
        for backend in backends:
            path = recorded_path(folder, backend, name)
            response = load_recorded(path)
            if response is None:
                continue
            if response.backend != backend:
                raise EvalError(f"{path}: recorded for backend {response.backend!r}")
            recorded[backend] = response
        loaded.append(LoadedCase(name, case, recorded))
    return loaded


def _mkdir(path: Path) -> None:
    if _is_dir(path):
        return
    if path.exists() or path.is_symlink():
        raise EvalError(f"{path}: not a directory (or a symbolic link)")
    path.mkdir(mode=0o755)


def write_recorded(folder: Path, case: str, recorded: RecordedResponse) -> Path:
    """Write `recorded` as `recorded/<backend>/<case>.json` under `folder`, atomically; the
    content is the canonical JSON of the model (indented, ASCII, sorted keys)."""
    if not _CASE_NAME.fullmatch(case):  # pragma: no cover - names come from case_names
        raise EvalError(f"{case!r}: not a case name")
    _mkdir(folder)
    _mkdir(folder / RECORDED_DIR)
    _mkdir(folder / RECORDED_DIR / recorded.backend)
    target = recorded_path(folder, recorded.backend, case)
    data = recorded.model_dump(mode="json")
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    temporary = target.with_name(f".{case}.{os.getpid()}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, target)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise EvalError(f"{target}: cannot be written ({error.strerror})") from None
    return target


__all__ = [
    "BACKENDS",
    "CASE_SUFFIX",
    "EVALS_DIR",
    "MAX_CASES",
    "MAX_CASE_BYTES",
    "MAX_RECORDED_BYTES",
    "RECORDED_DIR",
    "EvalError",
    "LoadedCase",
    "case_names",
    "evals_dir",
    "load_case",
    "load_cases",
    "load_recorded",
    "read_bounded",
    "recorded_path",
    "write_recorded",
]
