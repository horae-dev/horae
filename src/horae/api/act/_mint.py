# SPDX-License-Identifier: Apache-2.0
"""Minting the `/act` links of a notification's buttons (#38, schemas decision 54 A).

Only the core holds the key: a notifier never builds a URL. When the apply phase sends a
`notify.push` with `actions`, it asks the `LinkMinter` for one link per action: the task must
be in the store's task index (its `(status, due, snoozed_until)` is bound into the token, so
the link is stale once the task changes); the URL is `http.public_url` + `/act/` + the token,
the base from `horae.yaml` only (never a request's `Host`). An action whose task is not in the
index is left out; without the key, `http.public_url` or a route (`tasks_route`), nothing is
minted and the notification is sent without buttons. Each such reason is logged once per
process (`horae.api.act`), never with a token or a URL.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from horae.api.act._key import ActKey, ActKeyError, load_act_key
from horae.api.act._route import tasks_route
from horae.api.act._token import mint, state_digest
from horae.apply.sink import ActionLink
from horae.config import Instance
from horae.contracts.intents import NotifyAction
from horae.store import Store

logger = logging.getLogger("horae.api.act")
_said: set[str] = set()


def log_once(reason: str) -> None:
    """Log `reason` (a fixed text, never a value) once per process."""
    if reason not in _said:
        _said.add(reason)
        logger.warning("act: %s", reason)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LinkMinter:
    """Mints `/act` links under `public_url` (an `ActMinter`, `horae.apply.apply`)."""

    key: ActKey
    public_url: str
    clock: Callable[[], datetime] = _utc_now

    def mint(self, actions: Sequence[NotifyAction], store: Store) -> tuple[ActionLink, ...]:
        now = int(self.clock().timestamp())
        links: list[ActionLink] = []
        for action in actions:
            task = store.get_task(action.task)
            if task is None:
                log_once("a button's task is not in the task index: sent without it")
                continue
            token = mint(
                self.key,
                kind=action.kind,
                task=action.task,
                state=state_digest(task.status, task.due, task.snoozed_until),
                now=now,
                duration=action.duration,
            )
            links.append(ActionLink(action.label, f"{self.public_url}/act/{token}"))
        return tuple(links)


def act_minter(
    instance: Instance, *, clock: Callable[[], datetime] = _utc_now
) -> LinkMinter | None:
    """The instance's minter, or `None` (logged once) without `http.public_url`, a route or a
    usable `HORAE_ACT_KEY`."""
    public_url = instance.config.http.public_url
    if public_url is None:
        return None  # links are not configured: nothing to say
    if tasks_route(instance.pipelines) is None:
        log_once("no single `mode: tasks` act entry: notifications are sent without buttons")
        return None
    try:
        key = load_act_key(instance.secret_resolver())
    except ActKeyError as exc:
        log_once(f"{exc}: notifications are sent without buttons")
        return None
    if key is None:
        log_once("HORAE_ACT_KEY is not set: notifications are sent without buttons")
        return None
    return LinkMinter(key, public_url, clock)


__all__ = ["LinkMinter", "act_minter", "log_once"]
