# SPDX-License-Identifier: Apache-2.0
"""The `/act` pages (#38, schemas decision 54 G, K).

Static HTML written by Horae: no script, no external font, image or style, no Open Graph or
other meta tags a link preview could use, one inline `<style>` allowed by its hash in the
Content-Security-Policy. What a page shows of a task is its **title** (HTML-escaped, cut to
`MAX_TITLE` characters) and its **due date** (maintainer decision 5); never the fingerprint,
the note it lives in or anything else from the vault. Every page has the same headers
(`PAGE_HEADERS`): strict CSP, `no-store`, `no-referrer`, `nosniff`, `noindex`; except that the
confirmation page (`confirm`) has `Referrer-Policy: strict-origin` (`CONFIRM_REFERRER_POLICY`):
under `no-referrer` a browser sends `Origin: null` with the form's `POST`, which the origin
check refuses. `strict-origin` sends the origin only, never the path, so the token does not
leave in a `Referer`.

Every refusal of a token (malformed, forged, another key, expired, already used, unknown
task, a cross-origin `POST`) is the same `NOT_VALID` page with status 404: it does not say
which check failed (decision 54 K).
"""

import base64
import hashlib
import html
from dataclasses import dataclass
from datetime import date

MAX_TITLE = 120
"""The most characters of a task title a page shows."""

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.5;color:#1b1b1b;background:#fafafa}"
    "h1{font-size:1.25rem}p.task{font-weight:600}"
    "button{font-size:1rem;padding:.6rem 1.2rem;border-radius:.4rem;border:1px solid #444;"
    "background:#fff;cursor:pointer}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}"
    "button{background:#2a2a2a;color:#eee;border-color:#888}}"
)
STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("ascii")).digest()).decode("ascii")

CONTENT_SECURITY_POLICY = (
    f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
PAGE_HEADERS: dict[str, str] = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-robots-tag": "noindex, nofollow",
    "x-frame-options": "DENY",
}

CONFIRM_REFERRER_POLICY = "strict-origin"
"""The confirmation page's `Referrer-Policy`: the browser then sends `Origin: <public_url>`
with the form's `POST` (under `no-referrer` it sends `Origin: null`, refused)."""

ACTION_TEXT: dict[tuple[str, str | None], str] = {
    ("task.done", None): "Mark as done",
    ("task.snooze", "1h"): "Snooze for 1 hour",
    ("task.snooze", "3h"): "Snooze for 3 hours",
    ("task.snooze", "tomorrow"): "Snooze until tomorrow 09:00",
}


@dataclass(frozen=True)
class Page:
    """A page and its status."""

    status: int
    body: str
    retry_after: int | None = None
    referrer_policy: str = "no-referrer"


def _document(heading: str, *parts: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>Horae</title><style>{_STYLE}</style></head><body>"
        f"<h1>{html.escape(heading)}</h1>{''.join(parts)}</body></html>"
    )


def _p(text: str, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<p{attr}>{html.escape(text)}</p>"


def shown_title(title: str) -> str:
    """A task title as a page shows it: at most `MAX_TITLE` characters (then `…`)."""
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1] + "…"


def _task(title: str, due: date | None) -> list[str]:
    parts = [_p(shown_title(title), "task")]
    if due is not None:
        parts.append(_p(f"Due {due.isoformat()}"))
    return parts


def confirm(kind: str, duration: str | None, title: str, due: date | None) -> Page:
    """The `GET` page: what the link does, the task, one button that `POST`s to the same URL."""
    action = ACTION_TEXT[(kind, duration)]
    form = f'<form method="post"><button type="submit">{html.escape(action)}</button></form>'
    return Page(
        200, _document(f"{action}?", *_task(title, due), form), None, CONFIRM_REFERRER_POLICY
    )


def stale_preview(title: str, due: date | None) -> Page:
    """A `GET` for a task that changed since the link was made: nothing to confirm."""
    text = "This task changed since the notification; the link does nothing now."
    return Page(409, _document("Task changed", *_task(title, due), _p(text)))


def applied(kind: str, duration: str | None, until: str | None, title: str) -> Page:
    """After the change was applied."""
    done = "Marked as done." if kind == "task.done" else f"Snoozed until {until}."
    return Page(200, _document("Done", _p(shown_title(title), "task"), _p(done)))


def stale(title: str) -> Page:
    """The task changed since the link was made: nothing applied, the link is used up."""
    text = "This task changed since the notification: nothing was changed. The link is used up."
    return Page(409, _document("Task changed", _p(shown_title(title), "task"), _p(text)))


def not_changed(title: str, *, recurring: bool) -> Page:
    """The sink answered that it changed nothing (a recurring task, or one already in that
    state): the link is used up."""
    text = (
        "Not changed \N{EM DASH} tick it in Obsidian (a recurring task)."
        if recurring
        else "Not changed: the task already was in that state."
    )
    return Page(200, _document("Not changed", _p(shown_title(title), "task"), _p(text)))


def failed(title: str, *, timed_out: bool = False) -> Page:
    """The sink failed, or did not answer in time: the link is used up (single attempt)."""
    if timed_out:
        heading = "No answer"
        text = (
            "Your notes did not answer in time: the change may not have been applied. The "
            "link is used up; check the task in your notes."
        )
    else:
        heading = "Not applied"
        text = (
            "The change could not be applied. The link is used up; change the task in your notes."
        )
    return Page(502, _document(heading, _p(shown_title(title), "task"), _p(text)))


def maybe_applied(title: str) -> Page:
    """Something failed after the link was used up: the change may have been applied."""
    text = (
        "Something went wrong after the link was used: the change may have been applied. "
        "The link is used up; check the task in your notes."
    )
    return Page(500, _document("Maybe applied", _p(shown_title(title), "task"), _p(text)))


NOT_VALID = Page(404, _document("Link not valid", _p("This link is not valid any more.")))
TOO_MANY = Page(429, _document("Too many requests", _p("Try again in a minute.")))
UNAVAILABLE = Page(503, _document("Not available", _p("Action links are not available right now.")))
BUSY = Page(503, _document("Busy", _p("Horae is busy; try the link again in a moment.")))


__all__ = [
    "ACTION_TEXT",
    "BUSY",
    "CONFIRM_REFERRER_POLICY",
    "CONTENT_SECURITY_POLICY",
    "MAX_TITLE",
    "NOT_VALID",
    "PAGE_HEADERS",
    "TOO_MANY",
    "UNAVAILABLE",
    "Page",
    "applied",
    "confirm",
    "failed",
    "maybe_applied",
    "not_changed",
    "shown_title",
    "stale",
    "stale_preview",
]
