# SPDX-License-Identifier: Apache-2.0
"""Load an instance: `horae.yaml`, `pipelines/*.yaml` and `horae.lock` (PLAN.md §3.1)."""

import functools
import os
import stat
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from horae.config.documents import Problem, ProblemsError, YamlDocument, read_yaml, validate
from horae.contracts.instance import InstanceConfig, LlmBackend, ModelAlias, PluginDefaults
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import LlmStep, Pipeline
from horae.engine.canonical import sha256_hex
from horae.secrets import Secret, SecretError, SecretResolver, redact

CONFIG_FILE = "horae.yaml"
LOCK_FILE = "horae.lock"
PIPELINES_DIR = "pipelines"
STORE_FILE = "horae.db"
"""The SQLite store's file name, inside the state directory (ADR 0006)."""
PIPELINE_SUFFIXES = (".yaml", ".yml")


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
        part of it."""
        data = self.config.model_dump(mode="json", include={"models", "plugins"})
        return sha256_hex(data)

    @property
    def secrets_dir(self) -> Path:
        return self.root / self.config.secrets.dir

    def secret_resolver(self, environ: Mapping[str, str] | None = None) -> SecretResolver:
        """A resolver for this instance's secrets. `environ` (default: `os.environ`) is read
        only for the secrets that have an explicit `{env: VAR}` reference."""
        return SecretResolver(self.config.secrets.refs, self.secrets_dir, environ)

    def secret_names(self) -> tuple[str, ...]:
        """Every secret the instance names, sorted: `secrets.refs`, the secrets `horae.lock`
        grants its plugins and the model aliases' `secret`."""
        names = set(self.config.secrets.refs)
        for plugin in self.lock.plugins.values():
            names.update(plugin.permissions.secrets)
        names.update(spec.secret for spec in self.config.models.values() if spec.secret)
        return tuple(sorted(names))

    def prime_redaction(self) -> None:
        """Resolve every secret the instance names (`secret_names`), for redaction only, so
        that the process registry (`horae.secrets.redact`) knows them before anything is
        run, stored, logged or printed (#53, schemas decision 43 B). A secret that cannot
        be resolved is skipped: what needs it fails later with its own error. The values
        are never sent anywhere; only redaction uses them."""
        redact.prime(self.secret_resolver(), self.secret_names())

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

    def load[M: BaseModel](self, path: Path, model: type[M]) -> tuple[M, YamlDocument] | None:
        try:
            doc = read_yaml(path)
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


def _pipeline_files(state: "_Loaded", directory: Path) -> list[Path]:
    """The pipeline files in `directory`; unreadable or non-regular entries are problems."""
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
        if path.suffix not in PIPELINE_SUFFIXES or path.name.startswith("."):
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
        if (loaded_pipeline := state.load(path, Pipeline)) is None:
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
    for pipeline, doc in state.pipelines.values():
        zone = pipeline.trigger.timezone if pipeline.trigger else None
        if zone is not None and not _timezone_exists(zone):
            state.problems.append(
                doc.problem(("trigger", "timezone"), f"unknown time zone {zone!r}")
            )
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
        return not self.problems


def check_instance(root: Path, environ: Mapping[str, str] | None = None) -> CheckResult:
    """Every problem of the instance in `root`, including secrets that cannot be resolved.

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
        if state.lock is not None and state.lock_doc is not None:
            wanted += [
                (name, state.lock_doc, ("plugins", plugin, "permissions", "secrets", index))
                for plugin, entry in state.lock.plugins.items()
                for index, name in enumerate(entry.permissions.secrets)
            ]
        for name, doc, loc in wanted:
            try:
                resolver.resolve(name)
            except SecretError as exc:
                problems.append(doc.problem(loc, str(exc)))
    return CheckResult(sorted(set(problems), key=Problem.sort_key), instance)
