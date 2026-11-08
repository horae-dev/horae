# SPDX-License-Identifier: Apache-2.0
"""Minting the `/act` links of a notification's buttons (#38, schemas decision 54 A, C).

Only the core holds the key: a notifier never builds a URL. When the apply phase sends a
`notify.push` with `actions`, it asks the `LinkMinter` for one link per action: the task must
be in the store's task index (its `(status, due, snoozed_until)` is bound into the token, so
the link is stale once the task changes); the URL is `http.public_url` + `/act/` + the token,
the base from `horae.yaml` only (never a request's `Host`). **The button's label is the
core's** (`BUTTON_LABELS`, from the kind and the duration): a proposer's `label` is ignored,
so the text on the button always says what the link does.

An action is left out when its task is not in the index, is closed (`done` or `cancelled`),
or is a **recurring** task and the action closes it (`task.done`: a recurring task is ticked
in Obsidian; a snooze stays allowed). Whether a task repeats comes from the task index
(`TaskEntry.recurring`, set when a sink said so) and from the run's collected data (a
collected task naming the fingerprint with `recurring: true`, e.g. `obsidian.open_tasks`),
which also updates the index. Without the key, `http.public_url`, a route (`tasks_route`) or
a route whose plugin may apply `task.update_status` (`route_permitted`, as `horae serve`
checks), nothing is minted and the notification is sent without buttons. Each such reason is
logged once per process (`horae.api.act`), never with a token or a URL.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from horae.api.act import _route
from horae.api.act._key import ActKey, ActKeyError, load_act_key
from horae.api.act._token import mint, state_digest
from horae.apply.sink import ActionLink
from horae.config import Instance
from horae.contracts.intents import NotifyAction
from horae.store import Store, TaskEntry

logger = logging.getLogger("horae.api.act")
_said: set[str] = set()

BUTTON_LABELS: dict[tuple[str, str | None], str] = {
    ("task.done", None): "Done",
    ("task.snooze", "1h"): "Snooze 1h",
    ("task.snooze", "3h"): "Snooze 3h",
    ("task.snooze", "tomorrow"): "Tomorrow",
}
"""The text of each button, built by the core from its kind and duration (never the
proposer's `label`)."""

CLOSED = frozenset({"done", "cancelled"})
"""Task statuses no button is minted for."""
_CLOSING_KINDS = frozenset({"task.done"})
"""The kinds that close a task: never minted for a recurring task."""
_FINGERPRINT_FIELDS = ("fingerprint", "source_fingerprint", "task", "key")
_MAX_WALK = 100_000


def log_once(reason: str) -> None:
    """Log `reason` (a fixed text, never a value) once per process."""
    if reason not in _said:
        _said.add(reason)
        logger.warning("act: %s", reason)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def collected_recurrence(data: Any) -> dict[str, bool]:
    """Which task fingerprints the collected `data` says repeat or not: every object with a
    boolean `recurring` maps its `fingerprint`, `source_fingerprint`, `task` and `key` string
    values to it (`True` wins when two objects disagree: fail safe). Walks at most
    `_MAX_WALK` values."""
    found: dict[str, bool] = {}
    stack: list[Any] = [data]
    walked = 0
    while stack and walked < _MAX_WALK:
        item = stack.pop()
        walked += 1
        if isinstance(item, dict):
            flag = item.get("recurring")
            if isinstance(flag, bool):
                for name in _FINGERPRINT_FIELDS:
                    value = item.get(name)
                    if isinstance(value, str):
                        found[value] = found.get(value, False) or flag
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


@dataclass(frozen=True)
class LinkMinter:
    """Mints `/act` links under `public_url` (an `ActMinter`, `horae.apply.apply`).
    `recurring`: what the run's collected data says about tasks that repeat
    (`collected_recurrence`)."""

    key: ActKey
    public_url: str
    clock: Callable[[], datetime] = _utc_now
    recurring: Mapping[str, bool] = field(default_factory=dict)

    def _recurring(self, task: TaskEntry, store: Store) -> bool:
        seen = self.recurring.get(task.fingerprint)
        if seen is None or seen == task.recurring:
            return task.recurring
        try:
            store.set_task_recurring(task.fingerprint, seen, at=self.clock())
        except Exception as exc:
            logger.warning("act: task index not updated (%s)", type(exc).__name__)
        return seen

    def mint(self, actions: Sequence[NotifyAction], store: Store) -> tuple[ActionLink, ...]:
        now = int(self.clock().timestamp())
        links: list[ActionLink] = []
        for action in actions:
            task = store.get_task(action.task)
            if task is None:
                log_once("a button's task is not in the task index: sent without it")
                continue
            if task.status in CLOSED:
                log_once("a button's task is closed: sent without it")
                continue
            if action.kind in _CLOSING_KINDS and self._recurring(task, store):
                log_once("a done button for a recurring task: sent without it")
                continue
            token = mint(
                self.key,
                kind=action.kind,
                task=action.task,
                state=state_digest(task.status, task.due, task.snoozed_until),
                now=now,
                duration=action.duration,
            )
            label = BUTTON_LABELS[(action.kind, action.duration)]
            links.append(ActionLink(label, f"{self.public_url}/act/{token}"))
        return tuple(links)


IntentsOf = Callable[[str], frozenset[str]]
"""The approved `permissions.intents` of the plugin serving a tool."""


def installed_intents(instance: Instance) -> IntentsOf:
    """`IntentsOf` over the instance's installed plugins (`horae.lock` and `plugins/`), as
    `horae serve` reads them."""

    def intents_of(tool: str) -> frozenset[str]:
        from horae.cli_host import plugins_dir
        from horae.plugins.host import HostRegistry

        registry = HostRegistry.from_directory(instance.lock, plugins_dir(instance.root))
        return frozenset(registry.plugin_for_tool(tool).manifest.permissions.intents)

    return intents_of


def act_minter(
    instance: Instance,
    *,
    clock: Callable[[], datetime] = _utc_now,
    collected: Any = None,
    intents_of: IntentsOf | None = None,
) -> LinkMinter | None:
    """The instance's minter, or `None` (logged once) without `http.public_url`, a route, a
    route whose plugin may apply `task.update_status` (`intents_of`, by default the installed
    plugins'), or a usable `HORAE_ACT_KEY`. `collected`: the run's collected data (its
    `inputs`), read for recurring tasks."""
    public_url = instance.config.http.public_url
    if public_url is None:
        return None  # links are not configured: nothing to say
    route = _route.tasks_route(instance.pipelines)
    if route is None:
        log_once("no single `mode: tasks` act entry: notifications are sent without buttons")
        return None
    if not _route.route_permitted(route, intents_of or installed_intents(instance)):
        log_once(
            "the tasks entry's plugin may not apply task.update_status: notifications are "
            "sent without buttons"
        )
        return None
    try:
        key = load_act_key(instance.secret_resolver())
    except ActKeyError as exc:
        log_once(f"{exc}: notifications are sent without buttons")
        return None
    if key is None:
        log_once("HORAE_ACT_KEY is not set: notifications are sent without buttons")
        return None
    return LinkMinter(key, public_url, clock, collected_recurrence(collected))


__all__ = [
    "BUTTON_LABELS",
    "CLOSED",
    "IntentsOf",
    "LinkMinter",
    "act_minter",
    "collected_recurrence",
    "installed_intents",
    "log_once",
]
