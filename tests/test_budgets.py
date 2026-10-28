# SPDX-License-Identifier: Apache-2.0
"""Budgets per step and per day, and the usage ledger (#33, schemas decision 41)."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from budget_fixtures import FakeProvider, backends, write_instance
from horae.cli import app
from horae.config import Instance, load_instance
from horae.contracts import RunRecord, Usage
from horae.contracts.instance import BudgetsConfig, DailyBudget
from horae.contracts.pipeline import Budget
from horae.engine import LlmBackend
from horae.engine.ports import LlmError, LlmRequest, LlmResponse
from horae.engine.testing import FakeLlm, llm_response
from horae.runner import Ports, Runner
from horae.runner._budget import LedgerLlm, cap_budget, local_day
from horae.store import DaySpend, RunNotRunning, Store, StoreBusy, UnknownRun
from runner_fixtures import Plugins

FIXTURES = Path(__file__).resolve().parent / "budget_fixtures.py"
T0 = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
DAY = date(2026, 10, 28)
cli = CliRunner()


def _request(usd: float = 0.05, tokens_out: int = 2000, attempt: int = 1) -> LlmRequest:
    return LlmRequest(
        "pack/brief", "default", {"x": 1}, Budget(usd=usd, tokens_out=tokens_out), 30.0, attempt
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "horae.db") as opened:
        opened.begin_run("run-a", "brief-a", T0)
        opened.begin_run("run-b", "brief-b", T0)
        yield opened


# --- the ledger in the store ------------------------------------------------------------


def _reserve(store: Store, run: str = "run-a", **kwargs: Any) -> int:
    values: dict[str, Any] = {
        "step": 1,
        "attempt": 1,
        "alias": "default",
        "at": T0,
        "day": DAY,
        "usd": 0.02,
        "tokens_out": 500,
    }
    values.update(kwargs)
    return store.reserve_usage(run, **values)


def test_a_reservation_counts_until_it_is_settled_or_released(store: Store) -> None:
    first = _reserve(store, tokens_in=700, backend="anthropic", model="m", priced=True)
    second = _reserve(store, "run-b", usd=0.01, tokens_out=100)
    spent = store.day_spend(DAY)
    assert spent.usd == pytest.approx(0.03)
    assert spent.tokens_out == 600
    assert store.day_spend(DAY, pipeline="brief-b") == DaySpend(usd=0.01, tokens_out=100)
    assert store.day_spend(DAY + timedelta(days=1)) == DaySpend()
    usage = Usage(tokens_in=90, tokens_out=40, usd=0.005, cache_read_tokens=3, priced=True)
    store.settle_usage(first, usage, at=T0, model="m-2026")
    store.settle_usage(first, Usage(tokens_in=1, tokens_out=1, usd=9.0), at=T0)  # no-op
    store.release_usage(second)
    store.release_usage(first)  # settled: kept
    assert store.day_spend(DAY) == DaySpend(usd=0.005, tokens_out=40)
    (group,) = store.usage_report()
    assert (group.pipeline, group.backend, group.model, group.calls) == (
        "brief-a",
        "anthropic",
        "m-2026",
        1,
    )
    assert (group.tokens_in, group.cache_read_tokens, group.unpriced, group.estimated) == (
        90,
        3,
        0,
        0,
    )


def test_an_unknown_cost_keeps_the_reservation_as_an_estimate(store: Store) -> None:
    entry = _reserve(store)
    store.settle_usage(entry, None, at=T0, backend="anthropic")
    (group,) = store.usage_report()
    assert (group.usd, group.tokens_out, group.estimated, group.backend) == (
        0.02,
        500,
        1,
        "anthropic",
    )
    _reserve(store)  # in flight
    assert sum(g.estimated for g in store.usage_report()) == 2


def test_a_reservation_needs_a_running_run(store: Store) -> None:
    with pytest.raises(UnknownRun):
        _reserve(store, "nope")
    store.abandon_run("run-b", at=T0, error="gone")
    with pytest.raises(RunNotRunning):
        _reserve(store, "run-b")
    with pytest.raises(ValueError, match="attempt"):
        _reserve(store, attempt=0)
    with pytest.raises(ValueError, match="negative"):
        _reserve(store, usd=-1.0)


def test_ledger_rows_go_with_their_run(store: Store) -> None:
    _reserve(store)
    _reserve(store, "run-b")
    store.abandon_run("run-a", at=T0, error="gone")
    assert store.prune_runs(T0 + timedelta(seconds=1)) == 1
    assert [g.pipeline for g in store.usage_report()] == ["brief-b"]


def test_the_report_groups_and_filters(store: Store) -> None:
    for day, run in [(DAY, "run-a"), (DAY, "run-a"), (DAY - timedelta(days=2), "run-b")]:
        entry = _reserve(store, run, day=day)
        priced = run == "run-a"
        store.settle_usage(entry, Usage(tokens_in=1, tokens_out=2, usd=0.5, priced=priced), at=T0)
    groups = store.usage_report()
    assert [(g.day, g.pipeline, g.calls, g.unpriced) for g in groups] == [
        ("2026-10-26", "brief-b", 1, 1),
        ("2026-10-28", "brief-a", 2, 0),
    ]
    assert [g.day for g in store.usage_report(since=DAY)] == ["2026-10-28"]
    assert [g.day for g in store.usage_report(until=DAY - timedelta(days=1))] == ["2026-10-26"]
    assert store.usage_report(pipeline="nope") == []


def test_the_ledger_checks_its_rows(tmp_path: Path, store: Store) -> None:
    _reserve(store)
    conn = sqlite3.connect(tmp_path / "horae.db")
    try:
        for statement in (
            "UPDATE llm_usage SET day = '28/10/2026'",
            "UPDATE llm_usage SET state = 'settled'",
            "UPDATE llm_usage SET estimated = 1",
            "UPDATE llm_usage SET usd = -1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    finally:
        conn.close()


# --- capping a budget -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "spent", "expected"),
    [
        (
            DailyBudget(usd=1.0),
            DaySpend(usd=0.99),
            (0.01, 2000),
        ),
        (DailyBudget(usd=1.0), DaySpend(), (0.05, 2000)),
        (
            DailyBudget(tokens_out=2500),
            DaySpend(tokens_out=1000),
            (0.05, 1500),
        ),
        (
            DailyBudget(usd=5.0, tokens_out=9999),
            DaySpend(usd=1, tokens_out=1),
            (0.05, 2000),
        ),
    ],
)
def test_the_daily_allowance_caps_the_step_budget(
    ceiling: DailyBudget, spent: DaySpend, expected: tuple[float, int]
) -> None:
    budget = cap_budget(Budget(usd=0.05, tokens_out=2000), [("instance", ceiling, spent)], DAY)
    assert budget.usd == pytest.approx(expected[0])
    assert budget.tokens_out == expected[1]


@pytest.mark.parametrize(
    ("ceiling", "spent", "message"),
    [
        (
            DailyBudget(usd=1.0),
            DaySpend(usd=1.0),
            "daily usd ceiling (1) is reached for 2026-10-28",
        ),
        (DailyBudget(usd=1.0), DaySpend(usd=1.5), "usd ceiling"),
        (DailyBudget(tokens_out=10), DaySpend(tokens_out=10), "daily tokens_out ceiling (10)"),
    ],
)
def test_a_reached_ceiling_refuses_the_call(
    ceiling: DailyBudget, spent: DaySpend, message: str
) -> None:
    with pytest.raises(LlmError) as info:
        cap_budget(Budget(usd=0.05, tokens_out=2000), [("instance", ceiling, spent)], DAY)
    assert info.value.kind == "budget_exceeded"
    assert message in info.value.message


def test_the_day_is_the_instance_time_zone_day() -> None:
    late = datetime(2026, 10, 28, 23, 30, tzinfo=UTC)
    assert local_day(late, "Europe/Paris") == date(2026, 10, 29)
    assert local_day(late, "UTC") == DAY


# --- the ledger around the backend ------------------------------------------------------


def _ledger(store: Store, inner: LlmBackend, run: str = "run-a", **budgets: Any) -> LedgerLlm:
    """`budgets` is `horae.yaml` `budgets`."""
    pipeline = {"run-a": "brief-a", "run-b": "brief-b"}[run]
    config = BudgetsConfig.model_validate(budgets)
    return LedgerLlm(inner, store, run, pipeline, 1, config, "Europe/Paris", lambda: T0)


def test_a_call_is_reserved_at_its_budget_then_settled(store: Store) -> None:
    fake = FakeLlm([llm_response({}, tokens_out=40, usd=0.004)])
    ledger = _ledger(store, fake, daily={"usd": 0.03, "tokens_out": 1000})
    asyncio.run(ledger.complete(_request()))
    assert (fake.requests[0].budget.usd, fake.requests[0].budget.tokens_out) == (0.03, 1000)
    (group,) = store.usage_report()
    assert (group.usd, group.tokens_out, group.model, group.estimated) == (
        0.004,
        40,
        "fixture-model",
        0,
    )


def test_a_reached_ceiling_makes_no_call_and_reserves_nothing(store: Store) -> None:
    store.settle_usage(_reserve(store, "run-b", usd=0.03), None, at=T0)
    fake = FakeLlm([llm_response({})])
    ledger = _ledger(store, fake, daily={"usd": 0.03})
    with pytest.raises(LlmError, match="instance daily usd ceiling") as info:
        asyncio.run(ledger.complete(_request()))
    assert info.value.kind == "budget_exceeded"
    assert fake.requests == []
    assert sum(g.calls for g in store.usage_report()) == 1


def test_a_pipeline_ceiling_counts_only_its_own_calls(store: Store) -> None:
    store.settle_usage(_reserve(store, "run-b", tokens_out=900), None, at=T0)
    budgets: dict[str, Any] = {"pipelines": {"brief-a": {"daily": {"tokens_out": 1000}}}}
    fake = FakeLlm([llm_response({})])
    asyncio.run(_ledger(store, fake, **budgets).complete(_request()))
    assert fake.requests[0].budget.tokens_out == 1000
    fake_b = FakeLlm([llm_response({})])
    budgets_b: dict[str, Any] = {"pipelines": {"brief-b": {"daily": {"tokens_out": 900}}}}
    with pytest.raises(LlmError, match="pipeline 'brief-b' daily tokens_out"):
        asyncio.run(_ledger(store, fake_b, "run-b", **budgets_b).complete(_request()))


def test_yesterdays_spend_does_not_count(store: Store) -> None:
    store.settle_usage(_reserve(store, day=DAY - timedelta(days=1), usd=1.0), None, at=T0)
    fake = FakeLlm([llm_response({})])
    asyncio.run(_ledger(store, fake, daily={"usd": 1.0}).complete(_request()))
    assert fake.requests[0].budget.usd == 0.05


def test_a_cached_response_costs_nothing(store: Store) -> None:
    fake = FakeLlm([llm_response({}, usd=1.0, cached=True)])
    asyncio.run(_ledger(store, fake).complete(_request()))
    assert store.day_spend(DAY) == DaySpend()


@pytest.mark.parametrize(
    ("error", "calls", "usd", "estimated"),
    [
        (
            LlmError("output_invalid", "x", usage=Usage(tokens_in=1, tokens_out=5, usd=0.001)),
            1,
            0.001,
            0,
        ),
        (LlmError("rate_limited", "429"), 0, 0.0, 0),
        (LlmError("budget_exceeded", "pre-flight"), 0, 0.0, 0),
        (LlmError("timeout", "slow"), 1, 0.05, 1),
        (LlmError("host_error", "reset"), 1, 0.05, 1),
    ],
)
def test_a_failed_call_is_charged_what_it_may_have_cost(
    store: Store, error: LlmError, calls: int, usd: float, estimated: int
) -> None:
    with pytest.raises(LlmError):
        asyncio.run(_ledger(store, FakeLlm([error])).complete(_request()))
    groups = store.usage_report()
    assert sum(g.calls for g in groups) == calls
    assert store.day_spend(DAY).usd == pytest.approx(usd)
    assert sum(g.estimated for g in groups) == estimated


class _Hanging:
    async def complete(self, request: LlmRequest) -> LlmResponse:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")  # pragma: no cover


def test_a_cancelled_call_keeps_its_reservation(store: Store) -> None:
    async def go() -> None:
        async with asyncio.timeout(0.05):
            await _ledger(store, _Hanging()).complete(_request())

    with pytest.raises(TimeoutError):
        asyncio.run(go())
    (group,) = store.usage_report()
    assert (group.usd, group.estimated) == (0.05, 1)


def test_a_ledger_that_cannot_reserve_refuses_the_call(store: Store) -> None:
    store.abandon_run("run-a", at=T0, error="gone")
    fake = FakeLlm([llm_response({})])
    with pytest.raises(LlmError, match="cannot reserve the call") as info:
        asyncio.run(_ledger(store, fake).complete(_request()))
    assert info.value.kind == "host_error"
    assert fake.requests == []


def test_a_ledger_that_cannot_settle_keeps_the_answer(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def busy(*args: object, **kwargs: object) -> None:
        raise StoreBusy("busy")

    monkeypatch.setattr(Store, "settle_usage", busy)
    monkeypatch.setattr(Store, "release_usage", busy)
    response = asyncio.run(_ledger(store, FakeLlm([llm_response({"ok": 1})])).complete(_request()))
    assert response.output == {"ok": 1}
    with pytest.raises(LlmError):
        asyncio.run(_ledger(store, FakeLlm([LlmError("rate_limited", "x")])).complete(_request()))
    assert "cannot settle" in caplog.text
    assert "cannot release" in caplog.text
    assert store.usage_report()[0].estimated == 2  # both still reserved


def test_model_backends_reserve_their_worst_case(store: Store, tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance", ("brief-a",))
    models = load_instance(root).config.models
    llm, made = backends(models, tokens_out=30)
    asyncio.run(_ledger(store, llm).complete(_request(usd=0.01)))
    (provider,) = made
    sent = provider.sent[0]
    rendered = llm.render(_request(usd=0.01))
    worst = llm.worst_case(rendered)
    assert sent["max_tokens"] == rendered.max_tokens == worst.tokens_out
    assert worst.usd <= 0.01
    (group,) = store.usage_report()
    assert (group.tokens_out, group.tokens_in, group.backend, group.model) == (
        30,
        100,
        "anthropic",
        "fixture-model",
    )
    assert group.usd == pytest.approx((100 * 1.0 + 30 * 10.0) / 1e6)


def test_an_unpriced_alias_reserves_no_dollars(store: Store, tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance", ("brief-a",))
    models = dict(load_instance(root).config.models)
    models["default"] = models["default"].model_copy(update={"price": None})
    llm, _ = backends(models)
    rendered = llm.render(_request())
    assert llm.worst_case(rendered) == Usage(
        tokens_in=rendered.input_bound, tokens_out=2000, usd=0.0, priced=False
    )
    asyncio.run(_ledger(store, llm).complete(_request()))
    assert store.usage_report()[0].unpriced == 1


# --- end to end through the runner ------------------------------------------------------


def _run(
    tmp_path: Path,
    provider_kwargs: dict[str, Any] | None = None,
    pipeline: str = "brief-a",
    **config: Any,
) -> tuple[RunRecord, list[FakeProvider], Instance]:
    root = tmp_path / "instance"
    if not root.exists():
        write_instance(root, ("brief-a", "brief-b"), **config)
    loaded = load_instance(root)
    llm, made = backends(loaded.config.models, **(provider_kwargs or {}))
    plugins = Plugins()
    with Store.open(loaded.store_path) as opened:
        runner = Runner(loaded, opened, Ports(plugins, plugins, llm=llm))
        record = asyncio.run(runner.run(pipeline))
    return record, made, loaded


def _report(loaded: Instance) -> list[Any]:
    with Store.open(loaded.store_path) as opened:
        return opened.usage_report()


def test_a_run_writes_one_ledger_row_per_call(tmp_path: Path) -> None:
    record, _made, loaded = _run(tmp_path, {"tokens_out": 50})
    assert record.status == "ok"
    (group,) = _report(loaded)
    assert (group.pipeline, group.calls, group.tokens_out, group.usd) == (
        "brief-a",
        1,
        record.cost.tokens_out,
        pytest.approx(record.cost.usd),
    )


def test_the_daily_ceiling_stops_the_step_before_any_call(tmp_path: Path) -> None:
    budgets = {"daily": {"tokens_out": 60}}
    first, _, loaded = _run(tmp_path, {"tokens_out": 60}, budgets=budgets)
    assert first.status == "ok"
    second, made, _ = _run(tmp_path, {"tokens_out": 60}, "brief-b")
    assert second.status == "degraded"
    step = second.steps[1]
    assert (step.status, step.error and step.error.kind) == ("degraded", "budget_exceeded")
    assert step.error is not None
    assert "daily tokens_out ceiling (60)" in step.error.message
    assert made == [] or made[0].sent == []
    assert sum(g.calls for g in _report(loaded)) == 1


def test_the_daily_allowance_lowers_max_tokens(tmp_path: Path) -> None:
    _run(tmp_path, {"tokens_out": 1500}, budgets={"daily": {"tokens_out": 1800}})
    record, made, _ = _run(tmp_path, pipeline="brief-b")
    assert record.status == "ok"
    assert made[0].sent[0]["max_tokens"] == 300


def test_a_step_budget_too_small_for_the_input_sends_nothing(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance", ("brief-a", "brief-b"))
    text = (root / "pipelines" / "brief-a.yaml").read_text(encoding="utf-8")
    (root / "pipelines" / "brief-a.yaml").write_text(
        text.replace("usd: 0.05", "usd: 0.0001"), encoding="utf-8"
    )
    record, made, loaded = _run(tmp_path)
    step = record.steps[1]
    assert (record.status, step.status) == ("degraded", "degraded")
    assert step.error is not None
    assert step.error.kind == "budget_exceeded"
    assert "input" in step.error.message
    assert all(provider.sent == [] for provider in made)
    assert _report(loaded) == []


def test_the_repair_attempt_gets_what_the_first_one_left(tmp_path: Path) -> None:
    kwargs = {"tokens_out": 1500, "script": [["items: not an array"], None]}
    record, made, loaded = _run(tmp_path, kwargs)
    (provider,) = made
    assert [payload["max_tokens"] for payload in provider.sent] == [2000, 500]
    # The second attempt reports 1500 tokens too, over what was left: stopped.
    step = record.steps[1]
    assert step.error is not None
    assert step.error.kind == "budget_exceeded"
    assert record.cost.tokens_out == 3000
    assert [g.calls for g in _report(loaded)] == [2]


def test_a_first_attempt_that_spends_everything_leaves_no_repair(tmp_path: Path) -> None:
    record, made, _ = _run(tmp_path, {"script": [["bad"]]})
    assert len(made[0].sent) == 1
    step = record.steps[1]
    assert step.error is not None
    assert step.error.message == "no budget left for the repair retry"


# --- two processes, one daily ceiling ---------------------------------------------------


def _child(root: Path, pipeline: str, where: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 (our own interpreter and test script)
        [sys.executable, str(FIXTURES), str(root), pipeline, str(where)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_cannot_jointly_exceed_the_daily_ceiling(tmp_path: Path) -> None:
    # Each step may spend 2000 output tokens; the day allows 2000 in all.
    root = write_instance(
        tmp_path / "instance", ("brief-a", "brief-b"), budgets={"daily": {"tokens_out": 2000}}
    )
    where = tmp_path / "signals"
    where.mkdir()
    children = {name: _child(root, name, where) for name in ("brief-a", "brief-b")}
    try:
        deadline = time.monotonic() + 60
        # One reserves and calls (then waits for `go`); the other must be refused meanwhile.
        while time.monotonic() < deadline:
            sent = [name for name in children if (where / f"{name}.sent").exists()]
            finished = [name for name, proc in children.items() if proc.poll() is not None]
            if sent and finished:
                break
            time.sleep(0.02)
        assert len(sent) == 1, (sent, finished)
        assert finished != sent
        (where / "go").write_text("go", encoding="utf-8")
        results = {}
        for name, proc in children.items():
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
            results[name] = json.loads(out)
    finally:
        (where / "go").write_text("go", encoding="utf-8")
        for proc in children.values():
            if proc.poll() is None:  # pragma: no cover - only when the test fails
                proc.kill()
                proc.communicate()
    (called,) = sent
    (refused,) = set(children) - {called}
    assert results[called] == {"status": "ok", "kind": None}
    assert results[refused] == {"status": "degraded", "kind": "budget_exceeded"}
    with Store.open(root / "state" / "horae.db") as opened:
        assert opened.day_spend(local_day(datetime.now(UTC), "Europe/Paris")).tokens_out <= 2000
        assert [g.pipeline for g in opened.usage_report()] == [called]


# --- horae usage ------------------------------------------------------------------------


def _usage(*args: str) -> tuple[int, str]:
    result = cli.invoke(app, ["usage", *args], terminal_width=200)
    return result.exit_code, result.output


@pytest.fixture
def ledger_root(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance", ("brief-a", "brief-b"))
    loaded = load_instance(root)
    loaded.state_dir.mkdir()
    with Store.open(loaded.store_path) as opened:
        opened.begin_run("run-a", "brief-a", T0)
        opened.begin_run("run-b", "brief-b", T0)
        rows = [
            ("run-a", DAY, Usage(tokens_in=100, tokens_out=50, usd=0.25, priced=True), "m1"),
            ("run-a", DAY, Usage(tokens_in=10, tokens_out=5, usd=0.0, priced=False), "m2"),
            ("run-b", DAY - timedelta(days=1), Usage(tokens_in=1, tokens_out=2, usd=0.5), "m1"),
        ]
        for run, day, usage, model in rows:
            entry = _reserve(opened, run, day=day)
            opened.settle_usage(entry, usage, at=T0, backend="anthropic", model=model)
        _reserve(opened, "run-b", day=DAY)  # in flight
    return root


def test_usage_shows_cost_per_pipeline_and_day(ledger_root: Path) -> None:
    code, output = _usage("-i", str(ledger_root))
    assert code == 0
    lines = output.splitlines()
    assert lines[0].split() == [
        "DAY",
        "PIPELINE",
        "CALLS",
        "TOKENS",
        "IN/OUT",
        "CACHE",
        "R/W",
        "USD",
    ]
    assert lines[1].split() == ["2026-10-27", "brief-b", "1", "1/2", "0/0", "$0.5000"]
    assert lines[2].split() == ["2026-10-28", "brief-a", "2", "110/55", "0/0", "$0.2500*"]
    assert lines[3].split() == ["2026-10-28", "brief-b", "1", "0/500", "0/0", "$0.0200~"]
    assert lines[4].split() == ["TOTAL", "4", "111/557", "0/0", "$0.7700*~"]
    assert "* 1 call of a model alias without `price`" in output
    assert "~ 1 call counted at the most" in output


@pytest.mark.parametrize(
    ("by", "keys"),
    [
        ("day", [["2026-10-27"], ["2026-10-28"]]),
        ("pipeline", [["brief-a"], ["brief-b"]]),
        ("model", [["-", "(default)"], ["anthropic", "m1"], ["anthropic", "m2"]]),
    ],
)
def test_usage_groups_by(ledger_root: Path, by: str, keys: list[list[str]]) -> None:
    result = cli.invoke(app, ["usage", "-i", str(ledger_root), "--by", by, "--json"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    fields = report["by"]
    assert [[row[f] for f in fields] for row in report["rows"]] == keys
    assert report["total"]["calls"] == 4


def test_usage_json_and_filters(ledger_root: Path) -> None:
    args = ["usage", "-i", str(ledger_root), "-p", "brief-a", "--since", "2026-10-28", "--json"]
    result = cli.invoke(app, [*args, "--until", "2026-10-28"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["instance"] == str(ledger_root.resolve())
    assert (report["pipeline"], report["since"], report["until"]) == (
        "brief-a",
        "2026-10-28",
        "2026-10-28",
    )
    assert report["rows"] == [
        {
            "day": "2026-10-28",
            "pipeline": "brief-a",
            "calls": 2,
            "tokens_in": 110,
            "tokens_out": 55,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "usd": 0.25,
            "unpriced_calls": 1,
            "estimated_calls": 0,
        }
    ]
    assert report["total"]["usd"] == 0.25


def test_usage_without_a_store_lists_nothing(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance", ("brief-a",))
    assert _usage("-i", str(root)) == (0, "no LLM usage\n")
    assert not (root / "state").exists()
    result = cli.invoke(app, ["usage", "-i", str(root), "--json"])
    assert json.loads(result.stdout)["rows"] == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--since", "28/10/2026"], "expected a date, YYYY-MM-DD"),
        (["--until", "2026-13-01"], "expected a date, YYYY-MM-DD"),
        (["--since", "2026-10-29", "--until", "2026-10-28"], "--since is after --until"),
        (["--by", "week"], "'week' is not one of"),
    ],
)
def test_usage_errors_exit_2(ledger_root: Path, args: list[str], message: str) -> None:
    result = cli.invoke(app, ["usage", "-i", str(ledger_root), *args, "--json"])
    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["kind"] == "usage"
    assert message in error["message"]


def test_usage_never_migrates_an_older_store(ledger_root: Path) -> None:
    db = ledger_root / "state" / "horae.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    code, output = _usage("-i", str(ledger_root))
    assert code == 1
    assert "run `horae run` once to migrate it" in " ".join(output.split())
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
    finally:
        conn.close()


def test_usage_problems_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "instance"
    root.mkdir()
    (root / "horae.yaml").write_text("budgets: {daily: {}}\n", encoding="utf-8")
    result = cli.invoke(app, ["usage", "-i", str(root), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "config_invalid"
    (root / "horae.yaml").write_text("{}\n", encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "horae.db").write_text("not a database", encoding="utf-8")
    result = cli.invoke(app, ["usage", "-i", str(root), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "store_error"

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", str(root / "state" / "horae.db"))

    monkeypatch.setattr(Store, "open", denied)
    result = cli.invoke(app, ["usage", "-i", str(root), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "store_error"
