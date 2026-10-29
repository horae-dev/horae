# Design note: signed single-use action links (`/act`)

- **Issue:** [#38](https://github.com/horae-dev/horae/issues/38) (Human-led, security, M3). Threats
  **T13, T14, T16** (and T15 with #41) in [docs/security.md](../security.md#b4-phone--act); asset
  A3; story updates in [§10](../security.md#10-story-updates-requested).
- **Status:** Draft, for maintainer review. No code until the questions below are answered.
- **Sources:** [PLAN §3.5](../PLAN.md#35-interfaces) (`/act/<signed-token>`, single use, 24 h, key
  from env), §4 (`plugin-ntfy` "Done" / "Snooze" buttons, `obsidian://` deep links), §7 (only
  `/act/` outside SSO), §9 ("a stolen `/act` token");
  [ADR 0002](../adr/0002-intents-applied-by-the-core.md) and its #17 amendment; schemas decisions
  25, 30, 34 (apply, journal, compare-and-swap), 43 H (secret length);
  `src/horae/contracts/intents.py` (`TaskUpdateStatus`), `src/horae/secrets/`, the `tasks` table
  (migration 2). Depends on #37 (FastAPI app); the SSO exemption test is #41's.

## Goal and non-goals

A tap on a notification button (or a link in the daily note) changes **one task** in **one
predefined way**, once, within 24 h, without SSO. The token is the only credential, so it must be
unforgeable, bound, single use, short-lived and unreadable as a capability anywhere but `/act`.

Acceptance criteria: (AC1) a token works once, reuse and expiry are refused; (AC2) no secrets and no
free-form URLs in tokens; (AC3) rate limiting; (AC4, T14) `GET` consumes nothing, `POST` applies;
(AC5, T16) dedicated key, rotation invalidates every outstanding token.

Non-goals: minting links inside LLM text (the action guard stays as is); undo of task changes (not
journalled as undoable, decision 34 D7); the ntfy plugin and its iOS behaviour (#72, #65); Traefik
configuration (#41).

## A. What a link can do

The token carries an **action kind** from a closed set, never an intent document:

| Kind | Applies | Bound to |
|---|---|---|
| `task.done` | `task.update_status {status: done}` | fingerprint, expected state |
| `task.snooze` | `task.update_status {status: snoozed, until}` | fingerprint, expected state, a duration from a closed set |

`until` is computed by the core at `POST` time from the duration (question 6). Adding a kind is a
code change in the core, like adding an intent (ADR 0002). `notify.push`, `http.post` and `note.*`
can never be reached through `/act`.

**Minting.** Only the core holds the key. A notifier never builds a URL: the `notify.push` contract
gains an optional `actions` list (at most 3, ntfy's limit) of `{label, kind, task, duration?}`; the
core checks `task` like any task target (decision 30: a fingerprint the run collected), mints the
URLs at apply time and passes them to the notifier with the call. This is a schema change
(`schemas/` is sensitive, question 2). Daily-note links reuse the same minting from the
daily-brief renderer, in a follow-up (question 3).

## B. Token format

**Recommendation: a fixed binary layout, HMAC-SHA256, base64url without padding, one segment.**

```
token = b64url( payload || tag )
payload = ver(1) kid(4) kind(1) exp(8, unix s, big-endian) nonce(16)
          state(8) dur(1) fp_len(1) fingerprint(1..128, decision 3 alphabet)
tag = HMAC-SHA256(key, "horae-act/v1\0" || payload)      (32 bytes, not truncated)
```

- At most 168 + 32 = 200 bytes, **at most 267 characters**. The route accepts
  `^[A-Za-z0-9_-]{98,267}$` and nothing else; the decoder is strict (re-encoding must give the same
  string, so one token has one spelling), checks lengths, `ver`, a known `kind` and the fingerprint
  alphabet **after** the tag is verified with `hmac.compare_digest`.
- `state` is the first 8 bytes of SHA-256 over the task's current `(status, due, snoozed_until)`
  at mint time: the compare-and-swap value (section F). `dur` is 0 for `task.done`.
- **No secrets, no URLs, no titles:** only a kind, a fingerprint, numbers and random bytes (AC2).
  The base URL comes from trusted configuration (`http.public_url` in `horae.yaml`), never from the
  token or the request (`Host` / `X-Forwarded-Host` are ignored).
- The payload is **signed, not encrypted**: whoever sees the link can decode the fingerprint
  (e.g. `github:issue:123`). Question 4 weighs a server-side row instead.

Alternatives: canonical JSON + `.`-separated MAC (readable, but a second parser and canonicalisation
rules); JWT/PASETO (algorithm agility and libraries we do not need); an opaque random id with
everything in a store row (smallest exposure, but a write per minted button and a lookup before
the MAC can reject garbage).

## C. The key

- A **dedicated secret**, `HORAE_ACT_KEY`, resolved by `SecretResolver` (so it is redacted
  everywhere, decision 25 / #53), never shared with another use. **At least 32 bytes** (256 bits);
  shorter is refused at startup and a `horae doctor` error; docs say `openssl rand -base64 48`.
  Missing key: `/act` answers 503, the core mints no actions and the notification is sent without
  buttons (logged once).
- **Key id** `kid = HMAC-SHA256(key, "horae-act/kid")[:4]`: derived, no configuration, changes with
  the key, reveals nothing about it. It lets a token minted under another key be refused before
  anything else.
- **Rotation invalidates every outstanding token, no grace window** (AC5). One key is loaded;
  a token whose `kid` differs is refused. Since a token lives at most 24 h, a grace window
  (`HORAE_ACT_KEY_PREVIOUS`) buys little and contradicts the criterion; rotating after a suspected
  leak must kill all links at once. The consumed-nonce rows of the old key are kept until they
  expire (harmless).

## D. Single use and replay

New table (migration 8):

```sql
CREATE TABLE act_consumed (
    nonce       BLOB PRIMARY KEY CHECK (length(nonce) = 16),
    kid         BLOB NOT NULL,
    expires_at  INTEGER NOT NULL,      -- the token's exp
    consumed_at TEXT NOT NULL,
    run_id      TEXT REFERENCES runs (id) ON DELETE SET NULL,
    outcome     TEXT NOT NULL          -- applied | stale | failed
) STRICT;
```

- **Consume first, in the transaction that journals the change.** `POST`: verify the token, take
  the apply lock (decision 34 D6, waiting at most a few seconds, else 503 and the token stays
  usable), then one `BEGIN IMMEDIATE` that inserts the nonce (a primary-key conflict means
  "already used") **and** appends the journal entry; commit; then `Sink.apply`; then mark the
  outcome. A crash after the commit leaves the token used and the change journalled as not applied:
  **at most once**, never twice. A sink failure or a stale target also leaves it used (single use
  means a single attempt); the page says so.
- Each `POST` is a small run of the reserved pipeline `_act` (pipeline names never start with
  `_`), so the change has a run id, a record and a journal like any other.
- **Cleanup:** rows with `expires_at < now - 1 h` are deleted opportunistically on each `POST` (one
  bounded `DELETE`) and by `prune_runs`. Dropping them is safe because expiry already refuses those
  tokens; the extra hour covers a backward clock step.

## E. Expiry and clocks

`exp = mint time + 24 h`, refused when `now >= exp`. Minting and checking run on the same host and
clock, so there is **no skew leeway**. A token whose `exp` is more than 24 h + 5 min in the future
is refused too (guards a clock that jumped forward at mint, or a bug). The nonce row outliving the
token by an hour (section D) covers small backward steps.

## F. Binding to ADR 0002 and compare-and-swap

`POST` never trusts the token for more than *which task* and *which kind*: the core builds the
`task.update_status` intent itself, plans it through the normal path (permission of the sink that
owns the task, decision 30 routing, journal) and applies it with the sink's compare-and-swap.
Before that it recomputes `state` from the current task row: a different value (task already done,
snoozed again, due date moved, task gone) is **refused as `stale`**, nothing is applied, and the
page says "this task changed since the notification". Binding the whole state, not just
`status`, means a button can never act on a task the user has edited in the meantime. Repeated
re-collection that leaves those three fields unchanged does not make the link stale.

## G. `GET` shows, `POST` applies

- `GET /act/<token>`: rate limit, verify (format, tag, kid, expiry, not yet consumed, read only),
  then render a confirmation page with one `<form method="post">` button. It **writes nothing** (no
  nonce, no run, no log line with the token), so link previews, prefetchers and security scanners
  leave the token usable (AC4).
- `POST /act/<token>`: the same checks, then section D. A notification `http` action button posts
  directly (one tap); a `view` link or a daily-note link opens the page first (question 1).
- **CSRF:** the token in the path is the credential and is unguessable, so a cross-site form cannot
  post without already holding it. In addition: if `Origin` is present it must equal
  `http.public_url`'s origin, and `Sec-Fetch-Site`, if present, must be `same-origin` or `none`
  (the ntfy app's native client sends neither). No cookies are set or read, so there is no session
  to ride.
- **Headers on every `/act` response:** `Content-Security-Policy: default-src 'none';
  style-src 'sha256-<inline>'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`,
  `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `X-Robots-Tag: noindex`. No JavaScript, no external fonts, images or styles, no Open Graph tags
  (so previews show nothing useful).

**What the page shows:** the action ("Mark as done", "Snooze until tomorrow 09:00") and the task
**title** from the `tasks` table, escaped and cut to 120 characters, plus the due date if any. It
never shows note content, the fingerprint, the target path or anything else from the vault.
Anyone holding a valid token (including a previewer) sees that title; that is the price of the
user being able to check what they confirm (question 5).

## H. Rate limiting

In process, before any decoding or store access, for `GET` and `POST`: a token bucket **per client
IP** (10 requests/min, burst 5) and a **global** one (60/min), both answered `429` with
`Retry-After`. A request with an invalid token costs 3. The per-IP table is an LRU of at most 4096
entries. The client IP is the peer address, or the last `X-Forwarded-For` hop only when the peer
is a configured trusted proxy (Traefik); otherwise the header is ignored. Assumes one worker
process (the reference deployment); several workers would each have their own buckets, documented.

## I. SSO exemption

Only the `/act/` prefix is outside SSO (#41 tests the rendered Traefik template). In the app, the
`/act` router depends on no SSO header and reads none (no `X-Forwarded-User` trust); the router
matches exactly `/act/{token}` with the pattern above, so `%2F`, `..` or a trailing path cannot
reach `/runs` or `/healthz` through the exempt prefix (tested in the app as well).

## J. Logging and redaction

- The token never reaches a log, the run record or an error: uvicorn's access log is replaced, for
  `/act`, by one line `act <kind> <ref> <outcome>` where `ref` is the first 8 hex digits of
  SHA-256(nonce), only for tokens whose tag verified (an unverified string is logged as `invalid`
  with its length).
- The run record of `_act` keeps the intent and the `ref`, not the token.
- The operator guide (#41) tells the operator to turn off Traefik access logs for the `/act`
  router or drop its path, since Traefik logs paths before Horae sees them.

## K. Failure responses

Every pre-authorisation failure (bad format, bad tag, unknown kid, expired, already used, unknown
task) gives **the same page with the same status** (`404`, "This link is not valid any more"),
after the same work (the tag is always computed over a fixed-size buffer), so the response does not
say which check failed. Distinct answers only where they leak nothing about the token: `429` (rate
limit), `503` (no key, apply lock busy). After a successful `POST` consume, the holder of a valid
token gets the real outcome: applied, `stale` (changed since) or failed.

## Test plan (one test file per concern, `tests/test_act_*.py`)

| Criterion | Tests |
|---|---|
| AC1 single use, expiry | `test_a_token_applies_once_and_is_refused_after`, `test_two_concurrent_posts_apply_once` (two threads, one apply), `test_an_expired_token_is_refused`, `test_a_token_expiring_too_far_ahead_is_refused`, `test_a_crash_after_consume_never_applies_twice`, `test_cleanup_keeps_unexpired_nonces` |
| AC2 no secrets or URLs | `test_a_token_decodes_to_kind_fingerprint_numbers_only` (property test over minted tokens: no key bytes, no `://`, length ≤ 267), `test_the_base_url_comes_from_config_not_the_host_header` |
| AC3 rate limit | `test_per_ip_limit_returns_429`, `test_global_limit_returns_429`, `test_forwarded_for_is_ignored_from_an_untrusted_peer`, `test_invalid_tokens_cost_more` |
| AC4 GET / POST | `test_a_get_leaves_the_token_usable` (GET twice, then POST applies), `test_get_writes_nothing_to_the_store`, `test_a_cross_origin_post_is_refused`, `test_the_page_has_no_script_or_external_asset_and_strict_csp` |
| AC5 key | `test_rotating_the_key_refuses_every_outstanding_token`, `test_a_short_or_missing_key_disables_act`, `test_a_forged_or_tampered_token_is_refused` (every byte flipped) |
| Binding, CAS | `test_a_stale_task_is_refused_and_consumes_the_token`, `test_the_intent_is_built_by_the_core_from_the_kind`, `test_a_link_cannot_reach_another_intent_type` |
| Failures, logs | `test_every_invalid_token_gets_the_same_response`, `test_the_token_never_appears_in_logs_or_the_store` (scan DB + WAL and `caplog`), `test_the_page_shows_the_title_only` |
| Routing | `test_only_the_exact_act_route_is_served_without_sso` (`%2F`, `..`, trailing segments) |

## Risks

- A leaked link (shoulder, screenshot, shared phone) acts once within 24 h: bounded by kind and
  CAS; the user sees the result in the vault.
- Previewers see the task title (question 5).
- Fingerprints are visible in the link (question 4).
- Multiple workers weaken the in-process rate limit (documented; single worker by default).

## Questions for the maintainer

1. **One tap or always confirm?** Should a notification `http` action button `POST` directly (one
   tap, the ntfy app sends it), while `view` links and daily-note links open the confirmation page?
   *Recommendation: yes; previews never `POST`, and #72 checks the iOS behaviour first.*
2. **Contract change.** Add `notify.push.actions` (at most 3 `{label, kind, task, duration?}`),
   with the core minting URLs at apply time and the notifier never seeing the key?
   *Recommendation: yes, in this story (one schema change, `schemas/` reviewed).*
3. **Daily-note links.** Same minting from the daily-brief renderer in this story, or a follow-up?
   A link written to a note lives in the vault (and LiveSync) after it has expired.
   *Recommendation: follow-up story; #38 ships notification buttons only.*
4. **Signed payload or server-side row?** Keep the stateless signed payload (fingerprint visible in
   the link), or mint an opaque random id stored with its action (nothing readable, a write per
   button)? *Recommendation: signed payload; fingerprints are identifiers, not secrets, and the
   issue asks for HMAC-signed tokens. Revisit if a source puts personal data in fingerprints.*
5. **Confirmation page content.** Task title (escaped, 120 characters) and due date, nothing else?
   *Recommendation: yes; a generic "mark a task done?" page would make the confirmation useless.*
6. **Snooze durations.** A closed set resolved at `POST` time: `1h`, `3h`, `tomorrow` (09:00 in the
   instance time zone)? *Recommendation: yes, these three.*
7. **Consume on failure.** A stale target or a sink failure uses the token up (single attempt), but
   a busy apply lock (503) does not? *Recommendation: yes.*
8. **Rotation grace window.** None, as the criterion says? *Recommendation: none.*
9. **Rate limits.** 10/min per IP (burst 5), 60/min global, invalid tokens cost 3?
   *Recommendation: yes, as constants first, configurable later if needed.*
10. **Expected-state binding.** Bind `(status, due, snoozed_until)`, so an edited task makes its
    link stale, rather than `status` alone? *Recommendation: the three fields.*
