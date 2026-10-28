# SPDX-License-Identifier: Apache-2.0
"""Daily budgets and the usage ledger around the run's LLM backend (#33, schemas decision 41).

`LedgerLlm` wraps the `LlmBackend` of one run. For every attempt the engine makes:

1. **Prepare**, before any lock: for `ModelBackends`, `prepare` resolves the alias, reads the
   skill's prompt (the `PromptSource`, which may read files) and sizes the input bound. None
   of it depends on the budget, so it stays out of the store's write lock.
2. **Reserve**, in one store transaction (`BEGIN IMMEDIATE`, so the processes of other runs
   wait): read the local day's spend from the ledger (`Store.day_spend`: settled calls at
   their cost, calls in flight at their reservation), cap the attempt's budget with what is
   left of `horae.yaml` `budgets.daily` and `budgets.pipelines.<name>.daily` (refusing with
   `budget_exceeded`, no call, when nothing is left or when what is left cannot buy a
   minimum useful output, `MIN_OUTPUT_TOKENS`), render the request with that budget
   (`ModelBackends.render(capped, prepared)`: pure sizing and payload, no I/O; its own
   pre-flight may refuse it too, and the refusal then names the daily ceiling) and reserve
   the most it can cost (`ModelBackends.worst_case`; for another backend, the capped budget
   itself). Two runs can then never both spend the last of a ceiling: the second one sees
   the first one's reservation.
3. **Call** the provider, outside the transaction.
4. **Settle** the reservation with what the call cost, right after it (the reported usage,
   also of a failed call; zero for a backend's cached response). A settled cost above the
   reservation is logged (amounts only) and marked `over_reservation`. A refusal that cost
   nothing (`NOT_BILLED`) releases it. A call whose cost is unknown (a timeout, a lost
   connection, a cancellation) keeps its reservation as an estimate; a cancellation while
   the output was being validated knows it (`horae.engine.ports.attached_usage`, decision
   44 E) and settles with it. A process that dies
   before settling leaves the reservation, which keeps counting (a crash never frees
   budget) until its run is abandoned or recovered (`Store.abandon_run`), which settles it
   at the reservation, `estimated`.

The day is the instance's `timezone` day when the call is reserved, stored with the row: a
later `timezone` change does not move past rows. Dry runs are charged (they call the model;
"a dry run writes nothing" is about the user's data, not Horae's own state). The engine
still applies the step's own budget across its attempts, and marks the step `degraded` on
`budget_exceeded`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from horae.contracts.instance import BudgetsConfig, DailyBudget
from horae.contracts.pipeline import Budget
from horae.contracts.run import Usage
from horae.engine import LlmBackend
from horae.engine.ports import LlmError, LlmRequest, LlmResponse, attached_usage
from horae.llm import ModelBackends
from horae.llm.base import PreparedRequest, RenderedRequest
from horae.store import DaySpend, Store, StoreError

_log = logging.getLogger(__name__)

NOT_BILLED = frozenset(
    {
        "budget_exceeded",
        "backend_unavailable",
        "skill_unavailable",
        "params_invalid",
        "invalid_request",
        "auth_failed",
        "rate_limited",
        "overloaded",
    }
)
"""`LlmError` kinds that mean the provider did not process the request (or no request was
made): a reservation is released, not charged, when such an error reports no usage."""

MIN_OUTPUT_TOKENS = 256
"""The least output a daily ceiling must leave an attempt (decision 41 A): when the capped
allowance buys fewer output tokens than `min(MIN_OUTPUT_TOKENS, what the step's own budget
buys)`, the call is refused (`budget_exceeded`) instead of sent to be cut short."""

_FREE = Usage(tokens_in=0, tokens_out=0, usd=0.0)


def local_day(at: datetime, timezone: str) -> date:
    """The day `at` falls on in `timezone` (the ledger's `day`)."""
    return at.astimezone(ZoneInfo(timezone)).date()


def cap(
    budget: Budget, ceilings: list[tuple[str, DailyBudget, DaySpend]], day: date
) -> tuple[Budget, str | None]:
    """`(budget lowered to what each (name, ceiling, spent) leaves for day, the name of the
    last ceiling that lowered it or None)`; raises `LlmError("budget_exceeded")` when one of
    them leaves nothing, or fewer output tokens than `min(MIN_OUTPUT_TOKENS,
    budget.tokens_out)`."""
    usd, tokens_out = budget.usd, budget.tokens_out
    floor = min(MIN_OUTPUT_TOKENS, budget.tokens_out)
    binding: str | None = None
    for name, ceiling, spent in ceilings:
        if ceiling.usd is not None:
            left = ceiling.usd - spent.usd
            if not left > 0:
                raise LlmError(
                    "budget_exceeded",
                    f"the {name} daily usd ceiling ({ceiling.usd:g}) is reached for {day}",
                )
            if left < usd:
                usd, binding = left, name
        if ceiling.tokens_out is not None:
            tokens_left = ceiling.tokens_out - spent.tokens_out
            if tokens_left < 1:
                raise LlmError(
                    "budget_exceeded",
                    f"the {name} daily tokens_out ceiling ({ceiling.tokens_out}) is reached "
                    f"for {day}",
                )
            if tokens_left < floor:
                raise LlmError(
                    "budget_exceeded",
                    f"the {name} daily tokens_out ceiling ({ceiling.tokens_out}) leaves "
                    f"{tokens_left} output tokens for {day}, under the minimum useful output "
                    f"({floor})",
                )
            if tokens_left < tokens_out:
                tokens_out, binding = tokens_left, name
    return Budget(usd=usd, tokens_out=tokens_out), binding


def cap_budget(
    budget: Budget, ceilings: list[tuple[str, DailyBudget, DaySpend]], day: date
) -> Budget:
    """`budget`, lowered to what each `(name, ceiling, spent)` leaves for `day` (`cap`)."""
    return cap(budget, ceilings, day)[0]


@dataclass
class LedgerLlm:
    """The `LlmBackend` of one run: daily ceilings and the usage ledger (module docstring)."""

    inner: LlmBackend
    store: Store
    run_id: str
    pipeline: str
    step: int
    """The index of the pipeline's `llm` step (one per pipeline, decision 14)."""
    budgets: BudgetsConfig
    timezone: str
    clock: Callable[[], datetime]

    def _ceilings(self, day: date) -> list[tuple[str, DailyBudget, DaySpend]]:
        ceilings: list[tuple[str, DailyBudget, DaySpend]] = []
        if self.budgets.daily is not None:
            ceilings.append(("instance", self.budgets.daily, self.store.day_spend(day)))
        own = self.budgets.pipelines.get(self.pipeline)
        if own is not None:
            spent = self.store.day_spend(day, pipeline=self.pipeline)
            ceilings.append((f"pipeline {self.pipeline!r}", own.daily, spent))
        return ceilings

    def _render(
        self,
        inner: ModelBackends,
        request: LlmRequest,
        capped: LlmRequest,
        prepared: PreparedRequest,
        binding: str | None,
        day: date,
    ) -> RenderedRequest:
        """`capped`, rendered; a refusal (or a `max_tokens` under the minimum useful output)
        that only the daily ceiling `binding` causes names it (decision 41 A)."""
        try:
            rendered = inner.render(capped, prepared)
        except LlmError as error:
            if error.kind != "budget_exceeded" or binding is None:
                raise
            inner.output_limit(request, prepared)  # the step's own refusal, if it has one
            raise LlmError(
                "budget_exceeded",
                f"the {binding} daily budget leaves too little for {day}: {error.message}",
            ) from None
        if binding is not None:
            floor = min(MIN_OUTPUT_TOKENS, inner.output_limit(request, prepared))
            if rendered.max_tokens < floor:
                raise LlmError(
                    "budget_exceeded",
                    f"the {binding} daily budget leaves {rendered.max_tokens} output tokens "
                    f"for {day}, under the minimum useful output ({floor})",
                )
        return rendered

    def _reserve(self, request: LlmRequest) -> tuple[int, LlmRequest, RenderedRequest | None]:
        prepared: PreparedRequest | None = None
        if isinstance(self.inner, ModelBackends):
            prepared = self.inner.prepare(request)  # file reads: before the write lock
        at = self.clock()
        day = local_day(at, self.timezone)
        try:
            with self.store.transaction():
                budget, binding = cap(request.budget, self._ceilings(day), day)
                capped = replace(request, budget=budget)
                rendered: RenderedRequest | None = None
                if isinstance(self.inner, ModelBackends) and prepared is not None:
                    rendered = self._render(self.inner, request, capped, prepared, binding, day)
                    bound = self.inner.worst_case(rendered)
                    backend: str | None = rendered.backend
                    model: str | None = rendered.model
                else:
                    bound = Usage(
                        tokens_in=0, tokens_out=capped.budget.tokens_out, usd=capped.budget.usd
                    )
                    backend = model = None
                entry = self.store.reserve_usage(
                    self.run_id,
                    step=self.step,
                    attempt=request.attempt,
                    alias=request.model,
                    at=at,
                    day=day,
                    usd=bound.usd,
                    tokens_out=bound.tokens_out,
                    tokens_in=bound.tokens_in,
                    backend=backend,
                    model=model,
                    priced=bound.priced,
                )
        except StoreError as error:
            raise LlmError(
                "host_error", f"the usage ledger cannot reserve the call ({type(error).__name__})"
            ) from None
        return entry, capped, rendered

    def _settle(
        self, entry: int, usage: Usage | None, backend: str | None = None, model: str | None = None
    ) -> None:
        try:
            over = self.store.settle_usage(
                entry, usage, at=self.clock(), backend=backend, model=model
            )
        except StoreError as error:  # the reservation stays: it keeps counting
            _log.warning("usage ledger: cannot settle a call (%s)", type(error).__name__)
            return
        if over is not None:
            _log.warning(
                "usage ledger: a call cost more than its reservation "
                "(usd %.6f > %.6f or tokens_out %d > %d)",
                over.usd,
                over.reserved_usd,
                over.tokens_out,
                over.reserved_tokens_out,
            )

    def _release(self, entry: int) -> None:
        try:
            self.store.release_usage(entry)
        except StoreError as error:
            _log.warning("usage ledger: cannot release a call (%s)", type(error).__name__)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        entry, capped, rendered = self._reserve(request)
        try:
            if rendered is not None and isinstance(self.inner, ModelBackends):
                response = await self.inner.send(rendered)
            else:
                response = await self.inner.complete(capped)
        except LlmError as error:
            usage = getattr(error, "usage", None)
            if isinstance(usage, Usage):
                self._settle(entry, usage)
            elif error.kind in NOT_BILLED:
                self._release(entry)
            else:
                self._settle(entry, None)
            raise
        except BaseException as error:
            # A cancellation while the output was being validated carries what the attempt
            # cost (schemas decision 44 E); any other one keeps the reservation, estimated.
            self._settle(entry, attached_usage(error))
            raise
        usage = getattr(response, "usage", None)
        if getattr(response, "cached", False) is True:
            usage = _FREE
        self._settle(
            entry,
            usage if isinstance(usage, Usage) else None,
            getattr(response, "backend", None),
            getattr(response, "model", None),
        )
        return response


__all__ = ["MIN_OUTPUT_TOKENS", "NOT_BILLED", "LedgerLlm", "cap", "cap_budget", "local_day"]
