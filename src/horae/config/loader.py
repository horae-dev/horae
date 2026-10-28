# SPDX-License-Identifier: Apache-2.0
"""Load an instance: `horae.yaml`, `pipelines/*.yaml` and `horae.lock` (PLAN.md §3.1)."""

import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from horae.config.documents import Problem, ProblemsError, YamlDocument, read_yaml, validate
from horae.contracts.instance import InstanceConfig, LlmBackend, ModelAlias
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import LlmStep, Pipeline
from horae.secrets import Secret, SecretError, SecretResolver

CONFIG_FILE = "horae.yaml"
LOCK_FILE = "horae.lock"
PIPELINES_DIR = "pipelines"
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
    def secrets_dir(self) -> Path:
        return self.root / self.config.secrets.dir

    def secret_resolver(self, environ: Mapping[str, str] | None = None) -> SecretResolver:
        """A resolver for this instance's secrets, from `environ` (default: `os.environ`)."""
        return SecretResolver(self.config.secrets.refs, self.secrets_dir, environ)

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


def _timezone_exists(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _pipeline_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.iterdir()
        if p.suffix in PIPELINE_SUFFIXES and not p.name.startswith(".") and p.is_file()
    )


def _load(root: Path) -> _Loaded:
    state = _Loaded(root)
    config_path = root / CONFIG_FILE
    if not config_path.is_file():
        state.problems.append(Problem(str(config_path), None, None, "file not found"))
    elif loaded := state.load(config_path, InstanceConfig):
        state.config, state.config_doc = loaded

    lock_path = root / LOCK_FILE
    if lock_path.exists() and (lock := state.load(lock_path, Lockfile)):
        state.lock, state.lock_doc = lock

    for path in _pipeline_files(root / PIPELINES_DIR):
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
    locked = state.lock.plugins if state.lock is not None else {}
    for name in config.plugins:
        if name not in locked:
            state.problems.append(
                config_doc.problem(
                    ("plugins", name), f"plugin {name!r} is not installed (not in {LOCK_FILE})"
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
