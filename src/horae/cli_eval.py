# SPDX-License-Identifier: Apache-2.0
"""`horae eval <skill>` (#36, schemas decision 48): a skill's golden cases.

**Where the skill comes from.** By default, the instance's installed plugins
(`<instance>/plugins`, checked against `horae.lock` as `horae run` does); with `--plugin-dir`,
a plugin folder (it holds `plugin.yaml`) or a folder of plugin folders, for a plugin's own
repository and CI: no instance is read then, unless `--alias` or `--live` needs one.

**Offline** (the default): no network, no secret, no cost. Each case is rendered by the real
provider backend of the recorded backend (or of `--alias`'s), and its recorded response goes
through the same validator, intent check and action guard as a run's `llm` step
(`horae.evals`). A recording made for another skill digest, prompt, input or schema is
reported `stale` (a warning, not a failure).

**Live** (`--live`): each case is sent once (no repair retry) through the alias' backend and
its API key (the alias' declared secret). Spending money is explicit: every case is rendered
first and the worst case of the whole eval (`ModelBackends.worst_case`, what the usage ledger
would reserve) is shown; it must fit `--max-usd` and what the instance's daily budget
(`budgets.daily`, from the usage ledger when a store exists) has left, and `--max-usd` is
required when no daily budget bounds the eval. Then `--yes`, or a confirmation on a terminal,
is required. The eval is not a run: it records nothing in the store or its ledger (a
follow-up). A report is written to `<state_dir>/evals/<namespace>/<skill>/<timestamp>.json`,
with a text summary beside it (`.txt`). `--record` replaces the recorded responses with the
live answers, redacted (schemas decision 43).

Exit codes (`horae.cli_output`, decision 37): 0 every case passed; 1 a configuration, plugin,
skill or eval file problem, or a case that could not be evaluated (no recording, a backend or
validator error); 2 usage error; 3 unknown skill, case or alias, or a live eval refused (its
worst case is over budget, or it was not confirmed); 4 some case failed its expectations;
70 internal error.
"""

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
    SCHEMA_VERSION,
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
from horae.contracts.instance import ModelAlias
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import Usage
from horae.contracts.types import SkillRef
from horae.engine import UnknownPlugin
from horae.evals import (
    CaseResult,
    EvalError,
    LiveEval,
    LoadedCase,
    SkillUnderEval,
    evals_dir,
    load_cases,
    plan_live,
    run_offline,
    send_live,
    summary,
    total,
    write_recorded,
)
from horae.evals._harness import DEFAULT_TIMEOUT_S
from horae.plugins.host import HostRegistry, RegistryError
from horae.runner._budget import local_day
from horae.skills import SkillError, load_skill
from horae.store import Store, StoreError

EVAL_SCHEMA_VERSION = SCHEMA_VERSION
"""Version of the `horae eval --json` report (and of the written report)."""
MANIFEST = "plugin.yaml"
REPORTS_DIR = "evals"
"""Under the instance's `state_dir`: `evals/<namespace>/<skill>/<timestamp>.json`."""
MAX_TIMEOUT_S = 600.0

_SKILL_REF: TypeAdapter[str] = TypeAdapter(SkillRef)

__all__ = ["EVAL_SCHEMA_VERSION", "REPORTS_DIR", "eval_command"]


@dataclass(frozen=True)
class Located:
    """The skill under evaluation, its cases folder and the plugin's name."""

    sue: SkillUnderEval
    folder: Path
    plugin: str


def _manifests(directory: Path) -> list[tuple[Path, PluginManifest]]:
    """The valid plugin manifests of `directory` (itself a plugin, or a folder of plugins);
    a folder without a valid `plugin.yaml` is skipped."""
    roots = [directory] if (directory / MANIFEST).is_file() else sorted(directory.iterdir())
    found = []
    for root in roots:
        if not (root / MANIFEST).is_file():
            continue
        try:
            found.append((root, validate(read_yaml(root / MANIFEST), PluginManifest)))
        except ProblemsError:
            continue
    return found


def _from_plugin_dir(skill: str, directory: Path) -> tuple[Path, PluginManifest]:
    if not directory.is_dir():
        raise CommandFailed(EXIT_USAGE, "usage", f"--plugin-dir {directory}: not a directory")
    for root, manifest in _manifests(directory):
        if skill in manifest.provides.skills:
            return root, manifest
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


def _cases(located: Located, case: str | None) -> list[LoadedCase]:
    try:
        cases = load_cases(located.folder, case)
    except EvalError as error:
        raise CommandFailed(EXIT_PROBLEMS, "eval_invalid", error.message) from None
    if not cases:
        if case is not None:
            message = f"skill {located.sue.skill.name} has no case {case}"
            raise CommandFailed(EXIT_REFUSED, "unknown_case", message)
        message = f"skill {located.sue.skill.name} has no eval case ({located.folder})"
        raise CommandFailed(EXIT_PROBLEMS, "no_cases", message)
    return cases


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
    if document["report"]:
        lines.append(safe(f"report: {document['report']}"))
    return lines


def _document(
    located: Located,
    mode: str,
    alias: str | None,
    results: list[CaseResult],
    estimate: Usage | None = None,
) -> dict[str, Any]:
    skill = located.sue.skill
    ordered = sorted(results, key=lambda result: (result.case, result.backend or ""))
    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "mode": mode,
        "skill": skill.name,
        "skill_version": skill.version,
        "skill_digest": skill.digest,
        "plugin": located.plugin,
        "alias": alias,
        "cases": [result.to_dict() for result in ordered],
        "summary": summary(ordered),
        "estimate": None if estimate is None else estimate.model_dump(mode="json"),
        "report": None,
        "error": _error(ordered),
    }


def _emit(document: dict[str, Any], json_output: bool) -> None:
    if json_output:
        echo_json(document)
    else:
        for line in text_report(document):
            echo(line)


# --- live -------------------------------------------------------------------------------------


def _allowance(instance: Instance, now: datetime) -> tuple[float | None, int | None] | None:
    """What the instance's daily budget leaves today (`None` without one): the usage ledger's
    spend when the store exists, else the whole ceiling."""
    daily = instance.config.budgets.daily
    if daily is None:
        return None
    spent_usd, spent_tokens = 0.0, 0
    path = instance.store_path
    if path.exists():  # never create the store just to read it
        try:
            with Store.open(path, read_only=True) as store:
                spent = store.day_spend(local_day(now, instance.config.timezone))
        except StoreError as exc:
            raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
        except OSError as exc:
            raise io_failed(exc, path, store=path) from None
        spent_usd, spent_tokens = spent.usd, spent.tokens_out
    usd = None if daily.usd is None else max(0.0, daily.usd - spent_usd)
    tokens = None if daily.tokens_out is None else max(0, daily.tokens_out - spent_tokens)
    return usd, tokens


def check_budget(
    worst: Usage, max_usd: float | None, allowance: tuple[float | None, int | None] | None
) -> None:
    """Refuse (exit 3, `budget_exceeded`) a live eval whose worst case does not fit; a usage
    error when nothing bounds it."""
    if max_usd is None and allowance is None:
        raise CommandFailed(
            EXIT_USAGE,
            "usage",
            "--live needs --max-usd: no daily budget (horae.yaml budgets.daily) bounds the eval",
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


def write_report(state_dir: Path, document: dict[str, Any], now: datetime) -> Path:
    """`<state_dir>/evals/<namespace>/<skill>/<timestamp>.json` and its `.txt` (0700
    folders; files opened `x`, never overwritten)."""
    namespace, name = str(document["skill"]).split("/", 1)
    folder = state_dir / REPORTS_DIR / namespace / name
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"{stamp}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        document = {**document, "report": str(path)}
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(to_json(document) + "\n")
        with open(path.with_suffix(".txt"), "x", encoding="utf-8") as handle:
            handle.write("\n".join(text_report(document)) + "\n")
    except OSError as exc:
        raise io_failed(exc, path) from None
    return path


def _recorder(located: Located) -> Callable[[str, Any], str | None]:
    def record(case: str, recorded: Any) -> str | None:
        try:
            return str(write_recorded(located.folder, case, recorded))
        except EvalError as error:
            echo(f"horae eval: {safe(error.message)}", err=True)
            return None

    return record


@dataclass
class _Gate:
    """The cost gate of a live eval: budget, then confirmation."""

    max_usd: float | None
    allowance: tuple[float | None, int | None] | None
    yes: bool
    record: Callable[[str, Any], str | None] | None

    def __call__(self, worst: Usage, count: int) -> None:
        check_budget(worst, self.max_usd, self.allowance)
        _confirm(count, worst, self.yes)


async def _live(
    live: LiveEval, located: Located, cases: list[LoadedCase], timeout_s: float, gate: _Gate
) -> tuple[list[CaseResult], Usage]:
    """Plan every case, pass the gate, then send them; the backends are closed after."""
    try:
        planned, errors = plan_live(live, located.sue, cases, timeout_s)
        worst = total([item.worst_case for item in planned])
        if planned:
            gate(worst, len(planned))
        results = await send_live(live, located.sue, planned, on_record=gate.record)
        return [*errors, *results], worst
    finally:
        await live.backends.aclose()


# --- the command ------------------------------------------------------------------------------


def _eval(
    skill: str,
    instance_dir: Path,
    plugin_dir: Path | None,
    case: str | None,
    alias: str | None,
    live: bool,
    record: bool,
    yes: bool,
    max_usd: float | None,
    timeout_s: float,
    json_output: bool,
) -> int:
    if not live and (record or yes or max_usd is not None):
        raise CommandFailed(EXIT_USAGE, "usage", "--record, --yes and --max-usd need --live")
    if max_usd is not None and not max_usd > 0:
        raise CommandFailed(EXIT_USAGE, "usage", "--max-usd must be more than 0")
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise CommandFailed(EXIT_USAGE, "usage", f"--timeout must be in (0, {MAX_TIMEOUT_S:g}]")
    instance = load(instance_dir) if (plugin_dir is None or alias or live) else None
    located = locate(skill, instance, plugin_dir)
    cases = _cases(located, case)
    if not live:
        chosen = None if alias is None or instance is None else _alias(instance, alias)
        results = asyncio.run(run_offline(located.sue, cases, alias=chosen, timeout_s=timeout_s))
        _emit(_document(located, "offline", alias, results), json_output)
        return _status_code(results)
    if instance is None:  # pragma: no cover - loaded above for --live
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", "no instance")
    name, _ = _alias(instance, alias)
    now = datetime.now(UTC)
    gate = _Gate(max_usd, _allowance(instance, now), yes, _recorder(located) if record else None)
    backends = LiveEval.build(name, instance.config.models, instance.secret_resolver())
    results, worst = asyncio.run(_live(backends, located, cases, timeout_s, gate))
    document = _document(located, "live", name, results, worst)
    document["report"] = str(write_report(instance.state_dir, document, now))
    _emit(document, json_output)
    return _status_code(results)


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
        bool, typer.Option("--record", help="With --live: replace the recorded responses.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="With --live: do not ask before sending.")
    ] = False,
    max_usd: Annotated[
        float | None,
        typer.Option("--max-usd", help="With --live: refuse when the worst case costs more."),
    ] = None,
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
    must fit --max-usd and the instance's daily budget (--max-usd is required without one),
    then --yes or a terminal confirmation is needed. A report is written to
    <state_dir>/evals/<namespace>/<skill>/. --record replaces the recorded responses (redacted).

    Exit codes: 0 every case passed, 1 problems (configuration, plugin, skill or eval files;
    a case that could not be evaluated), 2 usage error, 3 unknown skill, case or alias, or a
    live eval refused (over budget, not confirmed), 4 some case failed, 70 internal error.
    With --json: {schema_version, mode, skill, skill_version, skill_digest, plugin, alias,
    cases, summary, estimate, report, error}; an error is {schema_version, error}."""
    run_command(
        "eval",
        json_output,
        lambda: _eval(
            skill,
            instance,
            plugin_dir,
            case,
            alias,
            live,
            record,
            yes,
            max_usd,
            timeout,
            json_output,
        ),
    )
