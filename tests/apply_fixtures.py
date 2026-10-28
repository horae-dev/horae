# SPDX-License-Identifier: Apache-2.0
"""A synthetic pipeline proposing every intent type, run with the engine's fixture ports."""

import asyncio
import datetime
from typing import Any

from horae.contracts import Pipeline, RunRecord
from horae.engine import ToolResult, execute, make_context
from horae.engine.testing import FixturePlugins

START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
RUN_ID = "20261027T053000Z-dry-brief-01"
DAILY = "Daily/2026-10-27.assistant.md"
INBOX = "Inbox.assistant.md"
HOOK = "https://hooks.example.org/brief"

INTENTS: list[dict[str, Any]] = [
    {
        "type": "note.replace_owned_block",
        "path": DAILY,
        "block": "horae-brief",
        "content": "- [ ] Review PR 42\n",
    },
    {"type": "note.append_section", "path": INBOX, "heading": "Brief", "content": "2 items"},
    {"type": "task.create", "title": "Review PR 42", "fingerprint": "fp-new", "due": "2026-10-27"},
    {"type": "task.update_status", "task": "fp-old", "status": "done"},
    {"type": "http.post", "url": HOOK, "body": {"items": 2}},
    {"type": "notify.push", "title": "Brief", "body": "2 items today"},
]


def _manifest(name: str, intents: list[str], **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0.1,<0.2",
        "provides": provides,
        "permissions": {"intents": intents, "network": ["hooks.example.org"]},
    }


def plugins(intents: list[dict[str, Any]] | None = None) -> FixturePlugins:
    """Fixture plugins: a source, a transform proposing `intents`, and the sinks."""
    proposed = INTENTS if intents is None else intents
    fixture = FixturePlugins()
    fixture.add(
        _manifest("items", [], sources=["items.today"]),
        {"items.today": lambda p, i: [{"task": "fp-old", "title": "Old task"}]},
    )
    fixture.add(
        _manifest(
            "brief",
            sorted({str(intent["type"]) for intent in INTENTS}),
            transforms=["brief.compose"],
        ),
        {"brief.compose": lambda p, i: ToolResult(data={"n": 2}, intents=proposed)},
    )
    fixture.add(
        _manifest(
            "vault",
            [
                "note.append_section",
                "note.replace_owned_block",
                "task.create",
                "task.update_status",
            ],
            sinks=["vault.write_note", "vault.tasks"],
        )
    )
    fixture.add(_manifest("hooks", ["http.post"], sinks=["hooks.post"]))
    fixture.add(_manifest("ntfy", ["notify.push"], notifiers=["ntfy.push"]))
    return fixture


PIPELINE = Pipeline.model_validate(
    {
        "name": "dry-brief",
        "steps": [
            {"collect": ["items.today"]},
            {"transform": ["brief.compose"]},
            {
                "act": [
                    {"vault.write_note": {"path": DAILY, "mode": "replace-owned"}},
                    {"vault.write_note": {"path": INBOX, "mode": "append-section"}},
                    {"vault.tasks": {"mode": "tasks"}},
                    {"hooks.post": {"url": HOOK, "mode": "post"}},
                ]
            },
            {"notify": [{"ntfy.push": {"when": "07:30"}}]},
        ],
    }
)


async def run_pipeline(*, dry_run: bool = True, fixture: FixturePlugins | None = None) -> RunRecord:
    fixture = plugins() if fixture is None else fixture
    return await execute(
        PIPELINE,
        run_id=RUN_ID,
        context=make_context(START, "Europe/Paris"),
        started_at=START,
        plugins=fixture,
        tools=fixture,
        dry_run=dry_run,
        clock=lambda: START,
        monotonic=lambda: 1.0,
    )


def record(*, dry_run: bool = True) -> RunRecord:
    return asyncio.run(run_pipeline(dry_run=dry_run))
