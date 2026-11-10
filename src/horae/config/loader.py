# SPDX-License-Identifier: Apache-2.0
"""Load an instance: `horae.yaml`, `pipelines/*.yaml` and `horae.lock` (PLAN.md §3.1)."""

import functools
import hashlib
import logging
import os
import stat
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from horae.config.cron import cron_problem
from horae.config.documents import (
    Problem,
    ProblemsError,
    YamlDocument,
    parse_yaml,
    read_yaml,
    validate,
)
from horae.config.schedule import ModeError, scheduler_mode
from horae.contracts.instance import (
    InstanceConfig,
    LlmBackend,
    ModelAlias,
    PluginDefaults,
    is_push_url,
)
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import LlmStep, Pipeline
from horae.engine.canonical import sha256_hex
from horae.secrets import (
    CORE_SECRETS,
    Secret,
    SecretError,
    SecretMissing,
    SecretResolver,
    redact,
)

_log = logging.getLogger("horae.config")

CONFIG_FILE = "horae.yaml"
LOCK_FILE = "horae.lock"
PIPELINES_DIR = "pipelines"
STORE_FILE = "horae.db"
"""The SQLite store's file name, inside the state directory (ADR 0006)."""
PIPELINE_SUFFIXES = (".yaml", ".yml")
MAX_PACK_PIPELINE_BYTES = 1024 * 1024


class InstanceError(ProblemsError):
    """The instance is invalid; `problems` lists every problem found, with file and line."""


class UnknownModelAlias(LookupError):
    """A model alias that `horae.yaml` does not define."""


@dataclass(frozen=True)
class ResolvedModel:
    """A model alias resolved to a backend, a model and (if any) its secret."""

    alias: str
    backend: LlmBackend
    model: str
    base_url: str | None
    secret: Secret | None


def redaction_names(config: InstanceConfig, lock: Lockfile | None) -> tuple[str, ...]:
    """Every secret an instance names, sorted: `secrets.refs`, the secrets `horae.lock`
    grants its plugins and the model aliases' `secret` (for redaction, #53)."""
    names = set(config.secrets.refs)
    for plugin in () if lock is None else lock.plugins.values():
        names.update(plugin.permissions.all_secrets)
    names.update(spec.secret for spec in config.models.values() if spec.secret)
    names.update(monitor.secret for monitor in config.scheduler.monitors.values())
    return tuple(sorted(names))


def read_lock(root: Path) -> Lockfile | None:
    """`<root>/horae.lock` if it exists and is valid, else `None` (best effort: for
    redaction by commands that read `horae.yaml` alone; the loader reports its problems)."""
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    try:
        return validate(read_yaml(path), Lockfile)
    except (ProblemsError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Instance:
    """A loaded, valid instance."""

    root: Path
    config: InstanceConfig
    pipelines: Mapping[str, Pipeline]
    """Pipelines by name (the file name without its suffix)."""
    lock: Lockfile
    pipeline_files: Mapping[str, Path] = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        return self.root / self.config.state_dir

    @property
    def store_path(self) -> Path:
        """The SQLite store (`<state_dir>/horae.db`)."""
        return self.state_dir / STORE_FILE

    def lock_digest(self) -> str:
        """`RunRecord.lock_digest`: SHA-256 of the canonical JSON of `horae.lock` as validated
        (plugins and permission lists sorted; an absent lockfile is an empty one)."""
        return sha256_hex(self.lock.model_dump(mode="json"))

    def config_digest(self) -> str:
        """`RunRecord.config_digest`: SHA-256 of the canonical JSON of the `models` and `plugins`
        sections of `horae.yaml` as validated (defaults filled in), what a run's LLM cache key
        and plugins depend on. Other settings (time zone, state directory, timeouts) are not
        part of it. An unset `models.<alias>.local` (#34) or `plugins.<name>.limits` (#51) is
        left out, so that the digest of an instance that does not use it is the one recorded
        before it existed."""
        data = self.config.model_dump(mode="json", include={"models", "plugins"})
        for alias in data["models"].values():
            if alias.get("local") is None:
                alias.pop("local", None)
        for plugin in data["plugins"].values():
            if plugin.get("limits") is None:
                plugin.pop("limits", None)
        return sha256_hex(data)

    @property
    def secrets_dir(self) -> Path:
        return self.root / self.config.secrets.dir

    def secret_resolver(self, environ: Mapping[str, str] | None = None) -> SecretResolver:
        """A resolver for this instance's secrets. `environ` (default: `os.environ`) is read
        only for the secrets that have an explicit `{env: VAR}` reference."""
        return SecretResolver(self.config.secrets.refs, self.secrets_dir, environ)

    def redaction_names(self) -> tuple[str, ...]:
        """Every secret the instance names (`redaction_names(config, lock)`)."""
        return redaction_names(self.config, self.lock)

    def prime_redaction(self) -> None:
        """Resolve every secret the instance names (`redaction_names`), for redaction only, so
        that the process registry (`horae.secrets.redact`) knows them before anything is
        run, stored, logged or printed (#53, schemas decision 43 A). A secret that cannot
        be resolved is skipped: what needs it fails later with its own error. The values
        are never sent anywhere; only redaction uses them. The core's own secrets
        (`CORE_SECRETS`: the `/act` key) are resolved too, when set."""
        resolver = self.secret_resolver()
        redact.prime(resolver, self.redaction_names())
        for name in sorted(CORE_SECRETS):  # the core's own (`HORAE_ACT_KEY`, #38)
            try:
                resolver.resolve_core(name)
            except SecretError:
                continue  # unset or unusable: what needs it reports it

    def plugin_timeouts(self, plugin: str) -> PluginDefaults:
        """The effective startup and call timeouts of `plugin` (for the plugin host)."""
        return self.config.plugin_timeouts(plugin)

    def model(self, alias: str) -> ModelAlias:
        """What `alias` resolves to, or `UnknownModelAlias`."""
        try:
            return self.config.models[alias]
        except KeyError:
            raise UnknownModelAlias(f"unknown model alias {alias!r}") from None

    def resolve_model(self, alias: str, secrets: SecretResolver) -> ResolvedModel:
        """Resolve `alias`, including its secret (`SecretError` if it cannot be read)."""
        spec = self.model(alias)
        secret = secrets.resolve(spec.secret) if spec.secret is not None else None
        return ResolvedModel(alias, spec.backend, spec.model, spec.base_url, secret)


@dataclass
class _Loaded:
    root: Path
    problems: list[Problem] = field(default_factory=list)
    config: InstanceConfig | None = None
    config_doc: YamlDocument | None = None
    lock: Lockfile | None = None
    lock_doc: YamlDocument | None = None
    lock_invalid: bool = False
    pipelines: dict[str, tuple[Pipeline, YamlDocument]] = field(default_factory=dict)

    def load[M: BaseModel](
        self, path: Path, model: type[M], raw: bytes | None = None
    ) -> tuple[M, YamlDocument] | None:
        """Load `path`, or parse `raw` (bytes already read and verified) as if it were."""
        try:
            doc = read_yaml(path) if raw is None else _parse_bytes(raw, path)
            return validate(doc, model), doc
        except ProblemsError as exc:
            self.problems.extend(exc.problems)
            return None

    def instance(self) -> Instance:
        assert self.config is not None  # noqa: S101
        return Instance(
            root=self.root,
            config=self.config,
            pipelines={name: p for name, (p, _) in self.pipelines.items()},
            lock=self.lock or Lockfile(schema_version=1),
            pipeline_files={name: doc.path for name, (_, doc) in self.pipelines.items()},
        )


@functools.cache
def _timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _timezone_exists(name: str) -> bool:
    """Whether `name` is a time zone key (not a directory such as `America`)."""
    return name in _timezones()


def _pipeline_files(state: "_Loaded", directory: Path, *, nested: bool = True) -> list[Path]:
    """The pipeline files in `directory`, and (`nested`) in each pack directory
    `directory/<pack>/` one level down (decision 72); unreadable or non-regular entries are
    problems."""
    try:
        info = os.stat(directory)
    except FileNotFoundError:
        return []
    except OSError as exc:
        state.problems.append(
            Problem(str(directory), None, None, f"cannot be read ({exc.strerror})")
        )
        return []
    if not stat.S_ISDIR(info.st_mode):
        state.problems.append(Problem(str(directory), None, None, "not a directory"))
        return []
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        state.problems.append(
            Problem(str(directory), None, None, f"cannot be read ({exc.strerror})")
        )
        return []
    files = []
    for path in entries:
        if path.name.startswith("."):
            continue
        if path.suffix not in PIPELINE_SUFFIXES:
            # A pack directory: a real directory (never a symlink to one) named like a plugin.
            if nested and path.suffix == "" and _is_real_directory(path):
                files.extend(_pipeline_files(state, path, nested=False))
            continue
        try:
            regular = stat.S_ISREG(path.stat().st_mode)
        except OSError as exc:
            state.problems.append(
                Problem(str(path), None, None, f"cannot be read ({exc.strerror})")
            )
            continue
        if not regular:
            state.problems.append(Problem(str(path), None, None, "not a regular file"))
            continue
        files.append(path)
    return files


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_bytes(raw: bytes, path: Path) -> YamlDocument:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProblemsError([Problem(str(path), None, None, "not valid UTF-8")]) from None
    return parse_yaml(text, path)


def _pack_pipeline_bytes(state: "_Loaded", path: Path) -> bytes | None:
    """The bytes of the pipeline file `pipelines/<pack>/<name>.yaml` may run (decision 72): its pack
    is listed under `plugins:` in horae.yaml and has an entry in horae.lock, and the file is
    the one the lock approved (same SHA-256). A pack that is not enabled is skipped without a
    word (removing it from `plugins:` deactivates its pipelines); a file that is not the
    approved one is a problem. The bytes returned are the ones hashed: the caller parses them,
    never the file again, so a swap after the check is not loaded."""
    pack = path.parent.name
    if state.config is None or state.lock is None or pack not in state.config.plugins:
        return None
    entry = state.lock.plugins.get(pack)
    if entry is None:
        return None
    if not entry.pipelines:
        # Locked before decision 72 (or not a pack): ignored, as such a directory always was.
        _log.warning(
            "ignoring %s: the %s entry of %s approves no pipeline (reinstall the plugin)",
            path,
            pack,
            LOCK_FILE,
        )
        return None
    approved = entry.pipelines.get(path.stem)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PACK_PIPELINE_BYTES + 1)
    except OSError as exc:
        state.problems.append(Problem(str(path), None, None, f"cannot be read ({exc.strerror})"))
        return None
    if approved is None or len(raw) > MAX_PACK_PIPELINE_BYTES or sha256_bytes(raw) != approved:
        state.problems.append(
            Problem(
                str(path),
                None,
                None,
                f"not the pipeline of {pack} that {LOCK_FILE} approves: reinstall the plugin "
                "or remove the file",
            )
        )
        return None
    return raw


def _load(root: Path) -> _Loaded:
    state = _Loaded(root)
    if loaded := state.load(root / CONFIG_FILE, InstanceConfig):
        state.config, state.config_doc = loaded

    lock_path = root / LOCK_FILE
    if os.path.lexists(lock_path):
        if lock := state.load(lock_path, Lockfile):
            state.lock, state.lock_doc = lock
        else:
            state.lock_invalid = True

    for path in _pipeline_files(state, root / PIPELINES_DIR):
        raw: bytes | None = None
        # A pack's file is any file below a directory other than `pipelines/` itself: decided by
        # structure, never by a plugin's name.
        if (
            path.parent != root / PIPELINES_DIR
            and (raw := _pack_pipeline_bytes(state, path)) is None
        ):
            continue
        if (loaded_pipeline := state.load(path, Pipeline, raw)) is None:
            continue
        pipeline, doc = loaded_pipeline
        if pipeline.name != path.stem:
            state.problems.append(
                doc.problem(
                    ("name",),
                    f"pipeline name {pipeline.name!r} does not match the file name {path.stem!r}",
                )
            )
        elif pipeline.name in state.pipelines:
            state.problems.append(
                doc.problem(("name",), f"pipeline {pipeline.name!r} is defined twice")
            )
        else:
            state.pipelines[pipeline.name] = (pipeline, doc)

    _cross_check(state)
    return state


def _cross_check(state: _Loaded) -> None:
    config, config_doc = state.config, state.config_doc
    if config is None or config_doc is None:
        return
    if not _timezone_exists(config.timezone):
        state.problems.append(
            config_doc.problem(("timezone",), f"unknown time zone {config.timezone!r}")
        )
    # An invalid horae.lock is already a problem: do not report every plugin as missing too.
    locked = state.lock.plugins if state.lock is not None else {}
    for name in config.plugins if not state.lock_invalid else ():
        if name not in locked:
            state.problems.append(
                config_doc.problem(
                    ("plugins", name),
                    f"plugin {name!r} is not installed (not in {LOCK_FILE})",
                    value=False,
                )
            )
    names = {pipeline.name for pipeline, _ in state.pipelines.values()}
    for name in config.budgets.pipelines:
        if name not in names:
            state.problems.append(
                config_doc.problem(
                    ("budgets", "pipelines", name),
                    f"budget for unknown pipeline {name!r}",
                    value=False,
                )
            )
    for name in config.scheduler.monitors:
        if name not in names:
            state.problems.append(
                config_doc.problem(
                    ("scheduler", "monitors", name),
                    f"push monitor for unknown pipeline {name!r}",
                    value=False,
                )
            )
    for pipeline, doc in state.pipelines.values():
        zone = pipeline.trigger.timezone if pipeline.trigger else None
        if zone is not None and not _timezone_exists(zone):
            state.problems.append(
                doc.problem(("trigger", "timezone"), f"unknown time zone {zone!r}")
            )
        if pipeline.trigger is not None and (reason := cron_problem(pipeline.trigger.cron)):
            state.problems.append(doc.problem(("trigger", "cron"), reason))
        for index, step in enumerate(pipeline.steps):
            if isinstance(step, LlmStep) and step.llm.model not in config.models:
                known = ", ".join(sorted(config.models)) or "none"
                state.problems.append(
                    doc.problem(
                        ("steps", index, "llm", "model"),
                        f"unknown model alias {step.llm.model!r} ({CONFIG_FILE} defines: {known})",
                    )
                )


def load_instance(root: Path) -> Instance:
    """Load and check the instance in `root`, or raise `InstanceError` listing every problem.

    Secrets are not resolved here: see `Instance.secret_resolver`."""
    state = _load(root)
    if state.problems:
        raise InstanceError(sorted(state.problems, key=Problem.sort_key))
    return state.instance()


@dataclass(frozen=True)
class CheckResult:
    """What `horae doctor` found."""

    problems: list[Problem]
    instance: Instance | None
    """The loaded instance, when it has no structural problem (secrets may still be missing)."""

    @property
    def ok(self) -> bool:
        return not any(problem.severity == "error" for problem in self.problems)


def _unresolved_optional(state: _Loaded, resolver: SecretResolver) -> list[Problem]:
    """An `info` for each optional secret a locked plugin may be given that does not resolve:
    the plugin starts without it and says so itself if a call needs it (schemas decision 56)."""
    found: list[Problem] = []
    if state.lock is None or state.lock_doc is None:
        return found
    for plugin, entry in state.lock.plugins.items():
        for index, name in enumerate(entry.permissions.optional_secrets):
            try:
                resolver.resolve(name)
            except SecretError as exc:
                loc = ("plugins", plugin, "permissions", "optional_secrets", index)
                # not set: expected (`info`); set but unusable (too short, bad bytes,
                # unreadable): the operator meant to give it, so `warning`. Never the value.
                unset = isinstance(exc, SecretMissing)
                message = f"optional {exc}; plugin {plugin} runs without it"
                found.append(
                    state.lock_doc.problem(loc, message, severity="info" if unset else "warning")
                )
    return found


def check_instance(root: Path, environ: Mapping[str, str] | None = None) -> CheckResult:
    """Every problem of the instance in `root`, including secrets that cannot be resolved, a
    push monitor's secret that is not a usable push URL, and an invalid `HORAE_SCHEDULER`
    (read from `environ`, else the process environment).

    Secret values are read to prove they resolve, then dropped; they never reach a problem."""
    state = _load(root)
    instance = None if state.problems else state.instance()
    problems = list(state.problems)
    if state.config is not None and state.config_doc is not None:
        resolver = SecretResolver(
            state.config.secrets.refs, root / state.config.secrets.dir, environ
        )
        wanted: list[tuple[str, YamlDocument, tuple[str | int, ...]]] = [
            (spec.secret, state.config_doc, ("models", alias, "secret"))
            for alias, spec in state.config.models.items()
            if spec.secret is not None
        ]
        wanted += [
            (monitor.secret, state.config_doc, ("scheduler", "monitors", name, "secret"))
            for name, monitor in state.config.scheduler.monitors.items()
        ]
        if state.lock is not None and state.lock_doc is not None:
            wanted += [
                (name, state.lock_doc, ("plugins", plugin, "permissions", "secrets", index))
                for plugin, entry in state.lock.plugins.items()
                for index, name in enumerate(entry.permissions.secrets)
            ]
        problems += _unresolved_optional(state, resolver)
        for name, doc, loc in wanted:
            try:
                secret = resolver.resolve(name)
            except SecretError as exc:
                problems.append(doc.problem(loc, str(exc)))
                continue
            # The ping's own check of a push URL; the value is never shown (it has a token).
            if loc[:2] == ("scheduler", "monitors") and not is_push_url(secret.reveal()):
                problems.append(
                    doc.problem(
                        loc, f"secret {name} is not an https:// URL or an http:// URL on the LAN"
                    )
                )
            del secret
        try:
            scheduler_mode(state.config.scheduler.mode, environ)
        except ModeError as exc:
            problems.append(Problem(str(state.config_doc.path), None, None, str(exc)))
    return CheckResult(sorted(set(problems), key=Problem.sort_key), instance)
