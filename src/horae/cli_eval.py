# SPDX-License-Identifier: Apache-2.0
"""`horae eval <skill>` (#36, schemas decision 48): a skill's golden cases.

**Where the skill comes from.** By default, the instance's installed plugins
(`<instance>/plugins`, checked against `horae.lock` as `horae run` does); with `--plugin-dir`,
a plugin folder (it holds `plugin.yaml`) or a folder of plugins, for a plugin's own
repository and CI: no instance is read then, unless `--alias` or `--live` needs one.

**Offline** (the default): no network, no secret, no cost. Each case is rendered by the real
provider backend of the recorded backend (or of `--alias`'s), and its recorded response goes
through the same validator, intent check and action guard as a run's `llm` step
(`horae.evals`). A recording made for another skill digest, prompt, input or schema is
reported `stale` (a warning, not a failure).

**Live** (`--live`): each case is sent once (no repair retry) through the alias' backend and
its API key (the alias' declared secret). Spending money is explicit and recorded (decision
48 J): every case is rendered first and the worst case of the whole eval
(`ModelBackends.worst_case`, what the usage ledger reserves) is shown; it must fit `--max-usd`
and what the instance's daily budget (`budgets.daily`, from the usage ledger) has left, and
`--max-usd` is required unless a daily `usd` ceiling bounds the eval. An alias without a
`price` on a cloud backend is refused unless `--allow-unpriced`. Then `--yes`, or a
confirmation on a terminal, is required, and the allowance is read again. The eval then runs
as a run of its own in the store, pipeline `eval:<skill>` (under its own lock, hidden by
`horae runs` unless `--all`): each case goes through `horae.runner.LedgerLlm`, so it is
reserved against the daily ceilings atomically before it is sent and settled after, like a
run's `llm` step. A report is written to `<state_dir>/evals/<namespace>/<skill>/<timestamp>
.json`, with a text summary beside it (`.txt`), even when the eval is interrupted.
`--record` (with `--plugin-dir` only, never into the installed plugins) replaces the recorded
response of each case whose live answer passes it, redacted (schemas decision 43).

Exit codes (`horae.cli_output`, decision 37): 0 every case passed; 1 a configuration, plugin,
skill or eval file problem, a case that could not be evaluated (no recording, a backend or
validator error), or a report that could not be written; 2 usage error; 3 unknown skill, case
or alias, or a live eval refused (its worst case is over budget, an unpriced alias, not
confirmed, another live eval of the skill running); 4 some case failed its expectations; 70
internal error; 130 interrupted.
"""

import asyncio
import contextlib
import hashlib
import math
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import TypeAdapter, ValidationError

from horae.cli_host import load, plugins_dir
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    CommandFailed,
    echo,
    echo_json,
    io_failed,
    plural,
    run_command,
    safe,
    to_json,
)
from horae.config import Instance, ProblemsError, read_yaml, validate
from horae.contracts.evals import CaseName
from horae.contracts.instance import ModelAlias
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import Usage
from horae.contracts.types import SkillRef
from horae.engine import UnknownPlugin
from horae.engine.ports import LlmResponse
from horae.evals import (
    CaseResult,
    EvalError,
    LiveEval,
    LoadedCase,
    Planned,
    SkillUnderEval,
    evals_dir,
    plan_live,
    run_offline,
    scan_cases,
    send_live,
    summary,
    total,
    write_recorded,
)
from horae.evals._harness import DEFAULT_TIMEOUT_S
from horae.llm.deid import Deidentifier, load_deidentifier
from horae.plugins.host import HostRegistry, RegistryError
from horae.runner import PipelineLock, lock_path, recover_pipeline
from horae.runner._budget import LedgerLlm, local_day
from horae.skills import SkillError, load_skill
from horae.store import EVAL_PIPELINE_PREFIX, PipelineBusy, Store, StoreError

EVAL_REPORT_SCHEMA_VERSION = 1
"""Version of the `horae eval --json` report (and of the written report); its own, not the
shared CLI error envelope's `SCHEMA_VERSION`."""
EVAL_SCHEMA_VERSION = EVAL_REPORT_SCHEMA_VERSION
"""Kept for callers of the first draft."""
MANIFEST = "plugin.yaml"
REPORTS_DIR = "evals"
"""Under the instance's `state_dir`: `evals/<namespace>/<skill>/<timestamp>.json`."""
MAX_TIMEOUT_S = 600.0
MAX_PIPELINE = 64
"""The store's longest pipeline name (`runs.pipeline`)."""

_SKILL_REF: TypeAdapter[str] = TypeAdapter(SkillRef)
_CASE_NAME: TypeAdapter[str] = TypeAdapter(CaseName)

__all__ = [
    "EVAL_REPORT_SCHEMA_VERSION",
    "EVAL_SCHEMA_VERSION",
    "REPORTS_DIR",
    "eval_command",
    "eval_pipeline",
]


@dataclass(frozen=True)
class Located:
    """The skill under evaluation, its cases folder and the plugin's name."""

    sue: SkillUnderEval
    folder: Path
    plugin: str


def _manifests(directory: Path) -> tuple[list[tuple[Path, PluginManifest]], list[str]]:
    """The valid plugin manifests of `directory` (itself a plugin, or a folder of plugins),
    and the problem of each invalid one; a folder without `plugin.yaml` is skipped."""
    roots = [directory] if (directory / MANIFEST).is_file() else sorted(directory.iterdir())
    found = []
    invalid = []
    for root in roots:
        if root.name.startswith(".") or not (root / MANIFEST).is_file():
            continue
        try:
            found.append((root, validate(read_yaml(root / MANIFEST), PluginManifest)))
        except ProblemsError as error:
            first = str(error.problems[0]) if error.problems else "invalid"
            invalid.append(f"{root / MANIFEST}: {first}")
    return found, invalid


def _from_plugin_dir(skill: str, directory: Path) -> tuple[Path, PluginManifest]:
    if not directory.is_dir():
        raise CommandFailed(EXIT_USAGE, "usage", f"--plugin-dir {directory}: not a directory")
    found, invalid = _manifests(directory)
    if invalid and (directory / MANIFEST).is_file():
        raise CommandFailed(EXIT_PROBLEMS, "plugins_invalid", invalid[0])
    providers = [(root, manifest) for root, manifest in found if skill in manifest.provides.skills]
    if len(providers) > 1:
        names = ", ".join(str(root) for root, _ in providers)
        raise CommandFailed(
            EXIT_PROBLEMS, "plugins_invalid", f"several plugins provide skill {skill}: {names}"
        )
    if providers:
        return providers[0]
    if invalid:
        # The skill may be the one an invalid manifest declares: say so, not "unknown".
        detail = "; ".join(invalid[:5])
        raise CommandFailed(
            EXIT_PROBLEMS,
            "plugins_invalid",
            f"no valid plugin in {directory} provides skill {skill}; invalid manifests: {detail}",
        )
    raise CommandFailed(
        EXIT_REFUSED, "unknown_skill", f"no plugin in {directory} provides skill {skill}"
    )


def _from_instance(skill: str, instance: Instance) -> tuple[Path, PluginManifest]:
    try:
        registry = HostRegistry.from_directory(instance.lock, plugins_dir(instance.root))
    except (ProblemsError, RegistryError) as exc:
        problems = exc.problems if isinstance(exc, ProblemsError) else ()
        detail = "; ".join(map(str, problems)) if problems else str(exc)
        raise CommandFailed(EXIT_PROBLEMS, "plugins_invalid", detail) from None
    try:
        name = registry.plugin_for_skill(skill).manifest.name
    except UnknownPlugin:
        message = f"no installed plugin provides skill {skill}"
        raise CommandFailed(EXIT_REFUSED, "unknown_skill", message) from None
    plugin = registry.installed(name)
    if plugin.root is None:  # pragma: no cover - from_directory sets every root
        raise CommandFailed(EXIT_PROBLEMS, "plugins_invalid", f"plugin {name} has no folder")
    return plugin.root, plugin.manifest


def locate(skill: str, instance: Instance | None, plugin_dir: Path | None) -> Located:
    """The skill (loaded and checked as the engine loads it) and its eval folder."""
    try:
        _SKILL_REF.validate_python(skill)
    except ValidationError:
        raise CommandFailed(
            EXIT_USAGE, "usage", f"not a skill name (`<namespace>/<skill>`): {skill[:64]!r}"
        ) from None
    if plugin_dir is not None:
        root, manifest = _from_plugin_dir(skill, plugin_dir)
    else:
        if instance is None:  # pragma: no cover - the caller loads it
            raise CommandFailed(EXIT_PROBLEMS, "config_invalid", "no instance")
        root, manifest = _from_instance(skill, instance)
    try:
        loaded = load_skill(root, skill, allowed_intents=manifest.permissions.intents)
    except SkillError as error:
        raise CommandFailed(EXIT_PROBLEMS, "skill_invalid", str(error)) from None
    return Located(SkillUnderEval(loaded, manifest), evals_dir(root, skill), manifest.name)


def _cases(located: Located, case: str | None) -> tuple[list[LoadedCase], list[CaseResult]]:
    """The cases that load, and an error result for each case file that does not (a name
    that is not a case name, an invalid case or recording: `eval_invalid`)."""
    try:
        scan = scan_cases(located.folder, case)
    except EvalError as error:
        raise CommandFailed(EXIT_PROBLEMS, "eval_invalid", error.message) from None
    broken = [CaseResult(name).fail("eval_invalid", message) for name, message in scan.problems]
    if not scan.cases and not broken:
        if case is not None:
            message = f"skill {located.sue.skill.name} has no case {case}"
            raise CommandFailed(EXIT_REFUSED, "unknown_case", message)
        message = f"skill {located.sue.skill.name} has no eval case ({located.folder})"
        raise CommandFailed(EXIT_PROBLEMS, "no_cases", message)
    return scan.cases, broken


def _alias(instance: Instance, alias: str | None) -> tuple[str, ModelAlias]:
    """`--alias` in the instance's models; without it, the instance's only alias."""
    models = instance.config.models
    if alias is None:
        if len(models) != 1:
            names = ", ".join(sorted(models)) or "none"
            raise CommandFailed(
                EXIT_USAGE, "usage", f"--live needs --alias (the instance's model aliases: {names})"
            )
        alias = next(iter(models))
    spec = models.get(alias)
    if spec is None:
        raise CommandFailed(EXIT_REFUSED, "unknown_alias", f"unknown model alias {alias!r}")
    return alias, spec


# --- output -----------------------------------------------------------------------------------


def _status_code(results: list[CaseResult]) -> int:
    if any(result.status == "error" for result in results):
        return EXIT_PROBLEMS
    if any(result.status == "fail" for result in results):
        return EXIT_DIFFERENT
    return EXIT_OK


def _error(results: list[CaseResult]) -> dict[str, str] | None:
    errors = sum(1 for result in results if result.status == "error")
    if errors:
        message = f"{plural(errors, 'case')} could not be evaluated"
        return {"kind": "eval_errors", "message": message}
    failed = sum(1 for result in results if result.status == "fail")
    if failed:
        return {"kind": "eval_failed", "message": f"{plural(failed, 'case')} failed"}
    return None


def _usage_text(usage: Usage) -> str:
    cost = f"${usage.usd:.4f}" if usage.priced is not False else "unpriced"
    return f"{usage.tokens_in} tokens in, {usage.tokens_out} out, {cost}"


def text_report(document: dict[str, Any]) -> list[str]:
    """The text form of an eval report (untrusted text escaped with `safe`)."""
    lines = [
        f"skill {document['skill']} {document['skill_version']} ({document['plugin']}), "
        f"{document['mode']}" + (f", alias {document['alias']}" if document["alias"] else "")
    ]
    if document.get("run"):
        lines.append(safe(f"run {document['run']}"))
    for case in document["cases"]:
        where = f" [{case['backend']}]" if case["backend"] else ""
        head = f"{case['status'].upper():5} {case['case']}{where}"
        if case["error"] is not None:
            head += f": {case['error']['kind']}: {case['error']['message']}"
        lines.append(safe(head))
        for check in case["checks"]:
            if check["ok"]:
                continue
            lines.append(safe(f"  - {check['name']}: {check['message'] or 'failed'}"))
            for diff_line in (check["diff"] or "").splitlines():
                lines.append("    " + safe(diff_line))
        if case["stale"]:
            stale = ", ".join(case["stale"])
            lines.append(f"  stale recording ({stale} changed): re-record with --live --record")
        if case["usage"] is not None and document["mode"] == "live":
            lines.append("  usage: " + _usage_text(Usage.model_validate(case["usage"])))
        if case["recorded_diff"]:
            lines.append("  differs from the recorded response:")
            for diff_line in case["recorded_diff"].splitlines():
                lines.append("    " + safe(diff_line))
        if case["recorded_path"]:
            lines.append(safe(f"  recorded: {case['recorded_path']}"))
        if case["recorded_skipped"]:
            lines.append(safe(f"  {case['recorded_skipped']}"))
    total_ = document["summary"]
    tail = (
        f"{plural(total_['cases'], 'case')}: {total_['passed']} passed, {total_['failed']} "
        f"failed, {plural(total_['errors'], 'error')}"
    )
    if total_["stale"]:
        tail += f" ({total_['stale']} stale)"
    if total_["usage"] is not None and document["mode"] == "live":
        tail += "; spent " + _usage_text(Usage.model_validate(total_["usage"]))
    lines.append(tail)
    if document["error"] is not None and document["error"]["kind"] == "interrupted":
        lines.append(safe(f"interrupted: {document['error']['message']}"))
    if document["report"]:
        lines.append(safe(f"report: {document['report']}"))
    if document["report_error"]:
        lines.append(safe(f"report not written: {document['report_error']}"))
    return lines


def _document(
    located: Located,
    mode: str,
    alias: str | None,
    results: list[CaseResult],
    estimate: Usage | None = None,
    run: str | None = None,
) -> dict[str, Any]:
    skill = located.sue.skill
    ordered = sorted(results, key=lambda result: (result.case, result.backend or ""))
    return {
        "schema_version": EVAL_REPORT_SCHEMA_VERSION,
        "mode": mode,
        "skill": skill.name,
        "skill_version": skill.version,
        "skill_digest": skill.digest,
        "plugin": located.plugin,
        "alias": alias,
        "run": run,
        "cases": [result.to_dict() for result in ordered],
        "summary": summary(ordered),
        "estimate": None if estimate is None else estimate.model_dump(mode="json"),
        "report": None,
        "report_error": None,
        "error": _error(ordered),
    }


def _emit(document: dict[str, Any], json_output: bool) -> None:
    if json_output:
        echo_json(document)
    else:
        for line in text_report(document):
            echo(line)


# --- reports ----------------------------------------------------------------------------------

_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _private_dir(path: Path) -> None:
    """`path`, a real directory (not a link), created 0700 if missing."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise NotADirectoryError(
                20, "not a directory (or a symbolic link)", str(path)
            ) from None


def _write_new(path: Path, text: str) -> None:
    """A new file (never an existing one, never through a link), 0600, written and synced."""
    fd = os.open(path, _NEW_FILE, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_report(state_dir: Path, document: dict[str, Any], now: datetime) -> Path:
    """`<state_dir>/evals/<namespace>/<skill>/<timestamp>.json` and its `.txt`: every folder
    level created 0700 (not a link), each file written 0600 to a new temporary name
    (`O_EXCL | O_NOFOLLOW`), then both renamed into place (the `.txt` first, the `.json`
    last). Raises `CommandFailed` (`io_error`)."""
    namespace, name = str(document["skill"]).split("/", 1)
    folder = state_dir / REPORTS_DIR / namespace / name
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"{stamp}.json"
    text_path = path.with_suffix(".txt")
    token = secrets.token_hex(4)
    temporary = [folder / f".{stamp}.{token}.json.tmp", folder / f".{stamp}.{token}.txt.tmp"]
    try:
        for level in (state_dir, state_dir / REPORTS_DIR, folder.parent, folder):
            _private_dir(level)
        document = {**document, "report": str(path)}
        _write_new(temporary[0], to_json(document) + "\n")
        _write_new(temporary[1], "\n".join(text_report(document)) + "\n")
        for target in (path, text_path):
            if target.exists() or target.is_symlink():
                raise FileExistsError(17, "the report exists", str(target))
        os.rename(temporary[1], text_path)
        os.rename(temporary[0], path)
    except OSError as exc:
        for leftover in temporary:
            with contextlib.suppress(OSError):
                os.unlink(leftover)
        raise io_failed(exc, path) from None
    return path


# --- live -------------------------------------------------------------------------------------


def eval_pipeline(skill: str) -> str:
    """The pipeline of a live eval's run row: `eval:<skill>`; a skill name too long for the
    store's 64 characters keeps its start and ends with a digest of the whole name."""
    pipeline = f"{EVAL_PIPELINE_PREFIX}{skill}"
    if len(pipeline) <= MAX_PIPELINE:
        return pipeline
    digest = hashlib.sha256(skill.encode("utf-8")).hexdigest()[:16]
    keep = MAX_PIPELINE - len(EVAL_PIPELINE_PREFIX) - len(digest) - 1
    return f"{EVAL_PIPELINE_PREFIX}{skill[:keep]}~{digest}"


def _run_id(skill: str, at: datetime) -> str:
    """A run id that sorts by start time: `<UTC stamp>-eval-<namespace>.<skill>-<hex>`."""
    stamp = at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-eval-{skill.replace('/', '.')[:80]}-{secrets.token_hex(3)}"


Allowance = tuple[float | None, int | None] | None
"""What the daily budget leaves today: `(usd, tokens_out)`, each `None` when not bounded;
`None` without a daily budget."""


def _allowance(instance: Instance, store: Store, now: datetime) -> Allowance:
    """What the instance's daily budget leaves today, from the usage ledger."""
    daily = instance.config.budgets.daily
    if daily is None:
        return None
    try:
        spent = store.day_spend(local_day(now, instance.config.timezone))
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{instance.store_path}: {exc}") from None
    usd = None if daily.usd is None else max(0.0, daily.usd - spent.usd)
    tokens = None if daily.tokens_out is None else max(0, daily.tokens_out - spent.tokens_out)
    return usd, tokens


def check_budget(worst: Usage, max_usd: float | None, allowance: Allowance) -> None:
    """Refuse (exit 3, `budget_exceeded`) a live eval whose worst case does not fit; a usage
    error when no `usd` bound (`--max-usd`, or a daily `usd` ceiling) bounds it."""
    if max_usd is None and (allowance is None or allowance[0] is None):
        raise CommandFailed(
            EXIT_USAGE,
            "usage",
            "--live needs --max-usd: no daily usd budget (horae.yaml budgets.daily.usd) "
            "bounds the eval",
        )
    if max_usd is not None and worst.usd > max_usd:
        raise CommandFailed(
            EXIT_REFUSED,
            "budget_exceeded",
            f"the eval may cost up to ${worst.usd:.4f}, over --max-usd ${max_usd:.4f}",
        )
    if allowance is not None:
        usd, tokens = allowance
        if usd is not None and worst.usd > usd:
            raise CommandFailed(
                EXIT_REFUSED,
                "budget_exceeded",
                f"the eval may cost up to ${worst.usd:.4f}; the daily budget has ${usd:.4f} left",
            )
        if tokens is not None and worst.tokens_out > tokens:
            raise CommandFailed(
                EXIT_REFUSED,
                "budget_exceeded",
                f"the eval may use up to {worst.tokens_out} output tokens; the daily budget "
                f"has {tokens} left",
            )


def interactive() -> bool:
    """Whether a confirmation can be asked (stdin is a terminal)."""
    return sys.stdin.isatty()


def _confirm(count: int, worst: Usage, yes: bool) -> None:
    message = f"send {plural(count, 'request')} to the model; worst case {_usage_text(worst)}"
    echo(message, err=True)
    if yes:
        return
    if not interactive():
        raise CommandFailed(EXIT_USAGE, "usage", "--live needs --yes when not on a terminal")
    if not typer.confirm("Send them?", default=False, err=True):
        raise CommandFailed(EXIT_REFUSED, "not_confirmed", "the live eval was not confirmed")


def _recorder(located: Located) -> Callable[[str, Any], str | None]:
    def record(case: str, recorded: Any) -> str | None:
        try:
            return str(write_recorded(located.folder, case, recorded))
        except EvalError as error:
            echo(f"horae eval: {safe(error.message)}", err=True)
            return None

    return record


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Session:
    """A live eval's run in the store: the cost gate, its run row and its ledger."""

    instance: Instance
    store: Store
    skill: str
    alias: str
    spec: ModelAlias
    max_usd: float | None
    yes: bool
    allow_unpriced: bool
    record: Callable[[str, Any], str | None] | None
    run_id: str | None = None
    ledger: LedgerLlm | None = None
    worst: Usage | None = None
    results: list[CaseResult] = field(default_factory=list)

    def gate(self, live: LiveEval, worst: Usage, count: int) -> None:
        """Unpriced, budget, confirmation, budget again (it may have changed while the
        confirmation waited), then the run row: nothing is sent before it all passes."""
        self.worst = worst
        unpriced = not self.spec.is_local and self.spec.price is None
        if unpriced and not self.allow_unpriced:
            raise CommandFailed(
                EXIT_REFUSED,
                "unpriced_alias",
                f"model alias {self.alias!r} has no price: its cost cannot be bounded "
                "(pass --allow-unpriced to send anyway)",
            )
        check_budget(worst, self.max_usd, _allowance(self.instance, self.store, _now()))
        _confirm(count, worst, self.yes)
        check_budget(worst, self.max_usd, _allowance(self.instance, self.store, _now()))
        started = _now()
        run_id = _run_id(self.skill, started)
        try:
            self.store.begin_run(run_id, eval_pipeline(self.skill), started)
        except PipelineBusy as busy:  # pragma: no cover - the eval lock excludes it
            raise CommandFailed(EXIT_REFUSED, "eval_busy", str(busy)) from None
        except StoreError as exc:
            raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{exc}") from None
        self.run_id = run_id
        self.ledger = LedgerLlm(
            inner=live.backends,
            store=self.store,
            run_id=run_id,
            pipeline=eval_pipeline(self.skill),
            step=0,
            budgets=self.instance.config.budgets,
            timezone=self.instance.config.timezone,
            clock=_now,
        )

    async def complete(self, item: Planned, index: int) -> LlmResponse:
        """One case through the usage ledger: reserved atomically against the daily ceilings
        (`BEGIN IMMEDIATE`), sent, then settled (or released); each case is its own `step`."""
        if self.ledger is None:  # pragma: no cover - the gate sets it
            raise RuntimeError("the live eval has no run")
        self.ledger.step = index
        return await self.ledger.complete(replace(item.request, attempt=1))

    def finish(self, failure: BaseException | None) -> None:
        """The run row: `ok` once every case was sent, `failed` (interrupted) otherwise,
        with what the cases cost (the ledger holds the details)."""
        if self.run_id is None:
            return
        used = [result.usage for result in self.results if result.usage is not None]
        cost = total(used) if used else Usage(tokens_in=0, tokens_out=0, usd=0.0)
        error = None if failure is None else f"interrupted ({type(failure).__name__})"
        try:
            self.store.finish_bare_run(
                self.run_id,
                status="ok" if failure is None else "failed",
                at=_now(),
                cost=cost,
                error=error,
            )
        except StoreError as exc:  # the next eval of the skill abandons the row
            echo(f"horae eval: the eval's run row cannot be finished ({exc})", err=True)


def _policy(instance: Instance) -> Deidentifier:
    """The instance's de-identification policy, as a run loads it (decision 47): a cloud
    alias is sent the pseudonymized viewed input of a classified skill."""
    config = instance.config
    return load_deidentifier(
        config.models, instance.secret_resolver(), config.deidentification.vocabulary
    )


async def _live(
    live: LiveEval,
    located: Located,
    cases: list[LoadedCase],
    timeout_s: float,
    session: _Session,
) -> None:
    """Plan every case, pass the gate, then send them (each result into `session.results`
    as soon as it is known); the backends are closed after."""
    try:
        policy = _policy(session.instance)
        planned, errors = plan_live(live, located.sue, cases, timeout_s, policy)
        session.results.extend(errors)
        worst = total([item.worst_case for item in planned])
        session.worst = worst
        if planned:
            session.gate(live, worst, len(planned))
            await send_live(
                live,
                located.sue,
                planned,
                complete=session.complete,
                on_record=session.record,
                results=session.results,
            )
    finally:
        await live.backends.aclose()


def _open_store(instance: Instance) -> Store:
    """The instance's store, opened for writing (created when missing, as a run does)."""
    path = instance.store_path
    try:
        return Store.open(path)
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None


def _eval_lock(instance: Instance, skill: str) -> PipelineLock:
    pipeline = eval_pipeline(skill)
    try:
        lock = PipelineLock.try_acquire(instance.state_dir, pipeline)
    except OSError as exc:
        raise io_failed(exc, lock_path(instance.state_dir, pipeline)) from None
    if lock is None:
        raise CommandFailed(
            EXIT_REFUSED, "eval_busy", f"another live eval of skill {skill} is running"
        )
    return lock


@dataclass(frozen=True)
class _LiveOptions:
    max_usd: float | None
    yes: bool
    record: bool
    allow_unpriced: bool
    timeout_s: float


def _run_live(
    instance: Instance,
    located: Located,
    cases: list[LoadedCase],
    broken: list[CaseResult],
    alias: str | None,
    options: _LiveOptions,
    json_output: bool,
) -> int:
    name, spec = _alias(instance, alias)
    skill = located.sue.skill.name
    with _eval_lock(instance, skill), _open_store(instance) as store:
        try:
            recover_pipeline(store, eval_pipeline(skill), clock=_now)
        except StoreError as exc:
            raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{exc}") from None
        session = _Session(
            instance,
            store,
            skill,
            name,
            spec,
            options.max_usd,
            options.yes,
            options.allow_unpriced,
            _recorder(located) if options.record else None,
        )
        session.results.extend(broken)
        live = LiveEval.build(name, instance.config.models, instance.secret_resolver())
        try:
            asyncio.run(_live(live, located, cases, options.timeout_s, session))
        except BaseException as exc:  # a Ctrl-C or a bug midway: the report still says
            if session.run_id is None:
                raise  # refused (or stopped) before anything was sent: no run, no report
            session.finish(exc)
            _report_live(instance, located, name, session, exc, json_output)
            raise
        session.finish(None)
        return _report_live(instance, located, name, session, None, json_output)


def _report_live(
    instance: Instance,
    located: Located,
    alias: str,
    session: _Session,
    failure: BaseException | None,
    json_output: bool,
) -> int:
    """Write the report (also of an interrupted eval) and print it; a report that cannot be
    written is said, the document is still printed, and the exit code is at least 1."""
    document = _document(located, "live", alias, session.results, session.worst, session.run_id)
    if failure is not None:
        document["error"] = {
            "kind": "interrupted",
            "message": f"the eval stopped after {plural(len(session.results), 'case')} "
            f"({type(failure).__name__})",
        }
    code = _status_code(session.results)
    try:
        document["report"] = str(write_report(instance.state_dir, document, _now()))
    except CommandFailed as failed:
        document["report_error"] = failed.message
        echo(f"horae eval: the report cannot be written: {safe(failed.message)}", err=True)
        code = EXIT_PROBLEMS if code == EXIT_OK else code
    if failure is None:
        _emit(document, json_output)
    else:
        for line in text_report(document):
            echo(line, err=True)
    return code


# --- the command ------------------------------------------------------------------------------


def _check_usage(
    live: bool,
    record: bool,
    yes: bool,
    max_usd: float | None,
    allow_unpriced: bool,
    plugin_dir: Path | None,
    case: str | None,
    timeout_s: float,
) -> None:
    if not live and (record or yes or max_usd is not None or allow_unpriced):
        raise CommandFailed(
            EXIT_USAGE, "usage", "--record, --yes, --max-usd and --allow-unpriced need --live"
        )
    if record and plugin_dir is None:
        raise CommandFailed(
            EXIT_USAGE,
            "usage",
            "--record needs --plugin-dir (a plugin's own folder): it never writes into the "
            "instance's installed plugins",
        )
    if max_usd is not None and not (math.isfinite(max_usd) and max_usd > 0):
        raise CommandFailed(EXIT_USAGE, "usage", "--max-usd must be a finite amount over 0")
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise CommandFailed(EXIT_USAGE, "usage", f"--timeout must be in (0, {MAX_TIMEOUT_S:g}]")
    if case is not None:
        try:
            _CASE_NAME.validate_python(case)
        except ValidationError:
            raise CommandFailed(
                EXIT_USAGE,
                "usage",
                f"not a case name (lower case, digits, `_`, `-`): {case[:64]!r}",
            ) from None


def _eval(
    skill: str,
    instance_dir: Path,
    plugin_dir: Path | None,
    case: str | None,
    alias: str | None,
    live: bool,
    options: _LiveOptions,
    json_output: bool,
) -> int:
    _check_usage(
        live,
        options.record,
        options.yes,
        options.max_usd,
        options.allow_unpriced,
        plugin_dir,
        case,
        options.timeout_s,
    )
    instance = load(instance_dir) if (plugin_dir is None or alias or live) else None
    located = locate(skill, instance, plugin_dir)
    cases, broken = _cases(located, case)
    if not live:
        chosen = None if alias is None or instance is None else _alias(instance, alias)
        results = asyncio.run(
            run_offline(
                located.sue,
                cases,
                alias=chosen,
                timeout_s=options.timeout_s,
                deid=None if instance is None else _policy(instance),
            )
        )
        results = [*broken, *results]
        _emit(_document(located, "offline", alias, results), json_output)
        return _status_code(results)
    if instance is None:  # pragma: no cover - loaded above for --live
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", "no instance")
    return _run_live(instance, located, cases, broken, alias, options, json_output)


def eval_command(
    skill: Annotated[str, typer.Argument(help="The skill, <namespace>/<skill>.")],
    instance: Annotated[
        Path,
        typer.Option(
            "--instance",
            "-i",
            envvar="HORAE_INSTANCE",
            help="Instance directory (holds horae.yaml).",
            file_okay=False,
        ),
    ] = Path(),
    plugin_dir: Annotated[
        Path | None,
        typer.Option(
            "--plugin-dir",
            help="A plugin folder, or a folder of plugins, instead of the instance's plugins.",
            file_okay=False,
        ),
    ] = None,
    case: Annotated[str | None, typer.Option("--case", help="Only this case.")] = None,
    alias: Annotated[
        str | None, typer.Option("--alias", help="The instance's model alias to render with.")
    ] = None,
    live: Annotated[
        bool, typer.Option("--live", help="Send each case to the model (costs money).")
    ] = False,
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            help="With --live and --plugin-dir: replace the recordings of the passing cases.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="With --live: do not ask before sending.")
    ] = False,
    max_usd: Annotated[
        float | None,
        typer.Option("--max-usd", help="With --live: refuse when the worst case costs more."),
    ] = None,
    allow_unpriced: Annotated[
        bool,
        typer.Option(
            "--allow-unpriced", help="With --live: send through a cloud alias without a price."
        ),
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds per case (validation included).")
    ] = DEFAULT_TIMEOUT_S,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Run a skill's golden cases (skills/<ns>/<skill>/evals/<case>.yaml).

    Offline (default): each recorded response (evals/recorded/<backend>/<case>.json) goes
    through the real backend's request rendering, the output validator, the intent check and
    the action guard, and is compared with the case's expectations (unified diffs). No
    network, no secret. A recording older than the skill, prompt, input or schema is reported
    stale.

    --live sends each case once through --alias (costs money): the worst case is shown and
    must fit --max-usd and the instance's daily budget (--max-usd is required without a
    daily usd ceiling; a cloud alias without a price needs --allow-unpriced), then --yes or a
    terminal confirmation is needed. The spend is recorded in the usage ledger, as a run of
    pipeline eval:<skill> (horae runs --all lists it). A report is written to
    <state_dir>/evals/<namespace>/<skill>/. --record (with --plugin-dir) replaces the
    recordings of the cases whose live answer passes (redacted).

    Exit codes: 0 every case passed, 1 problems (configuration, plugin, skill or eval files;
    a case that could not be evaluated; a report not written), 2 usage error, 3 unknown
    skill, case or alias, or a live eval refused (over budget, unpriced, not confirmed, busy),
    4 some case failed, 70 internal error, 130 interrupted. With --json: {schema_version,
    mode, skill, skill_version, skill_digest, plugin, alias, run, cases, summary, estimate,
    report, report_error, error}; an error is {schema_version, error}."""
    options = _LiveOptions(max_usd, yes, record, allow_unpriced, timeout)
    run_command(
        "eval",
        json_output,
        lambda: _eval(skill, instance, plugin_dir, case, alias, live, options, json_output),
    )
