# SPDX-License-Identifier: Apache-2.0
"""Signed single-use action links, `/act/<token>` (#38, schemas decision 54).

A tap on a notification button changes **one task** in **one predefined way**, once, within
24 hours, without SSO: the token is the only credential.

- `horae.api.act._key`: the dedicated HMAC key `HORAE_ACT_KEY` and its derived key id;
- `horae.api.act._token`: the fixed binary token, its HMAC and its strict verification;
- `horae.api.act._route`: the one `mode: tasks` entry that applies a button's change;
- `horae.api.act._mint`: the links minted when a notification is sent (`LinkMinter`);
- `horae.api.act._limit`: the in-process rate limit and the client address;
- `horae.api.act._page`: the static pages and their headers;
- `horae.api.act._service`: what `GET` (shows, writes nothing) and `POST` (applies once) do;
- `horae.api.act._routes`: the FastAPI routes (loaded by `horae.api._app` only).

This package's `__init__` imports no web framework: the runner mints links with it.
"""

from horae.api.act._key import ACT_KEY_NAME, MIN_KEY_BYTES, ActKey, ActKeyError, load_act_key
from horae.api.act._mint import LinkMinter, act_minter
from horae.api.act._route import ActRoute, route_permitted, tasks_route
from horae.api.act._token import (
    MAX_AHEAD_S,
    TOKEN_PATTERN,
    TOKEN_TTL_S,
    ActToken,
    mint,
    state_digest,
    verify,
)

__all__ = [
    "ACT_KEY_NAME",
    "MAX_AHEAD_S",
    "MIN_KEY_BYTES",
    "TOKEN_PATTERN",
    "TOKEN_TTL_S",
    "ActKey",
    "ActKeyError",
    "ActRoute",
    "ActToken",
    "LinkMinter",
    "act_minter",
    "load_act_key",
    "mint",
    "route_permitted",
    "state_digest",
    "tasks_route",
    "verify",
]
