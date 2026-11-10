# Horae as an MCP server

`horae mcp serve` lets an MCP client (Claude Code, another agent, a phone chat app) look at a
Horae instance without becoming its brain: read-only tools (#54), token authentication (#56),
and `pipeline.run`, which only *asks* for a run that you approve yourself (#55). Design:
[decision 65](../schemas/README.md#provisional-decisions-54-pending-maintainer-review),
[decision 75](../schemas/README.md#provisional-decisions-56-pending-maintainer-review) and
[decision 77](../schemas/README.md#provisional-decisions-55-pending-maintainer-review), threats
[T17, T46 to T49](security.md).

- **Transport: stdio by default.** The client starts `horae mcp serve` and talks to it over
  the process's stdin and stdout. Horae opens no port and asks for no token: whoever can start
  the command can already read the instance's store.
- **Or HTTP with a token.** `horae mcp serve --http` serves the same tools over streamable
  HTTP on `127.0.0.1:8765`, and every request needs a bearer token (see
  [Over HTTP with a token](#over-http-with-a-token)).
- **Read-only, except asking.** The three read tools open the store read-only and close it.
  No tool takes a path, a command or a secret. `pipeline.run` makes a **dry run** and returns
  an approval id: nothing is applied until you approve it at the instance's terminal (see
  [Approving `pipeline.run`](#approving-pipelinerun)). No tool can approve.
- **Untrusted text.** Task titles, note paths and run errors come from notes and plugins. They
  are returned as data (clipped, one line, secrets redacted, `x-untrusted` in the schema) and
  the tool descriptions tell the client not to follow them. They still reach the client's
  context: add the server only to a client you would let read those notes.

## Tools

| Tool | Arguments | Returns (schema) |
|---|---|---|
| `tasks.today` | `limit` (1 to 100, default 50) | open tasks due today (instance time zone) or overdue, overdue first: [`schemas/mcp/tasks-today/v1.json`](../schemas/mcp/tasks-today/v1.json) |
| `brief.latest` | none | the latest run of the brief pipeline and the note paths it changed, no content: [`schemas/mcp/brief-latest/v1.json`](../schemas/mcp/brief-latest/v1.json) |
| `runs.recent` | `limit` (1 to 50, default 10) | the latest runs, newest first, as `horae runs --json` shows them: [`schemas/mcp/runs-recent/v1.json`](../schemas/mcp/runs-recent/v1.json) |
| `pipeline.run` | `pipeline` (a pipeline name) | a dry run (nothing applied), what it would change (one line per change: kind, action, target; no content) and an approval id with its expiry: [`schemas/mcp/pipeline-run/v1.json`](../schemas/mcp/pipeline-run/v1.json). Needs the scope `pipeline.run` |

Each result is `structuredContent` that validates against its schema, which the server also
publishes as the tool's `outputSchema`. An instance with no store or no data returns empty
lists (and `run: null` for `brief.latest`).

`brief.latest` reports the pipeline named `daily-brief`; pass `--brief-pipeline NAME` to
`horae mcp serve` if yours has another name.

## Add it to Claude Code

```
claude mcp add horae -- horae mcp serve -i /path/to/instance
```

Use an absolute instance path (or set `HORAE_INSTANCE` in the server's environment with
`claude mcp add -e HORAE_INSTANCE=/path/to/instance horae -- horae mcp serve`). With `uv` from a
checkout: `claude mcp add horae -- uv run --directory /path/to/horae horae mcp serve -i
/path/to/instance`. Add `-s user` or `-s project` to choose the scope.

## Over HTTP with a token

For a client that cannot start a local process (another machine, a container), serve the tools
over HTTP. Every request needs `Authorization: Bearer <token>`; anything else gets the same
`401 invalid_token`, whatever the reason.

1. Create a token (it is printed **once**: copy it now; Horae stores only its SHA-256):

   ```
   horae mcp token create laptop-claude -i /path/to/instance
   horae mcp token create phone -i /path/to/instance --expires-in 30
   ```

   Scopes: `read` (default; the three read tools) and `pipeline.run` (the tool `pipeline.run`:
   a dry run and an approval id; approving stays yours). Add `--scope pipeline.run` (with
   `--scope read` if it should read too) only for a client you want to let ask for runs.
2. Start the server (it refuses to start while no token is usable):

   ```
   horae mcp serve --http -i /path/to/instance            # http://127.0.0.1:8765/mcp
   ```

   `--port` changes the port. `--host 0.0.0.0` listens on every interface: only behind a TLS
   reverse proxy, since the token is otherwise sent in clear text; pass the proxy's public
   name with `--allowed-host`, or requests are refused by the Host check.
3. Add it to the client with the token in a header. Claude Code:

   ```
   claude mcp add --transport http horae http://127.0.0.1:8765/mcp \
     --header "Authorization: Bearer $HORAE_MCP_TOKEN"
   ```

   Keep the token out of shell history and committed config: read it from an environment
   variable or a secret store. Any MCP client that can send a header on streamable HTTP works.
4. Manage tokens:

   ```
   horae mcp token list -i /path/to/instance       # name, scopes, created, expires, last used, status; never the token
   horae mcp token revoke laptop-claude -i /path/to/instance
   ```

   A revoked token is refused from the server's next request on, without a restart. To rotate,
   create a new token, switch the client to it, then revoke the old one.

## Approving `pipeline.run`

`pipeline.run` never applies anything. It runs the pipeline as a dry run (collect steps and the
model run as for `horae run --dry-run`, and the model's cost counts against your daily budgets),
then returns what would change and an id such as `apr-k3j7x2m4q7ab`. The client should show you
both. Then, **on the instance's terminal**:

```
horae approval list -i /path/to/instance            # every request: pending, applied, denied, expired...
horae approval approve apr-k3j7x2m4q7ab -i /path/to/instance
horae approval deny apr-k3j7x2m4q7ab -i /path/to/instance
horae approval revoke --all -i /path/to/instance    # end every pending request
horae approval show apr-k3j7x2m4q7ab -i /path/to/instance   # its journal of transitions
```

- `approve` plans the dry run again, shows the whole change set (as `horae diff`, HTTP bodies
  in full) and asks you to retype a 6-character code printed with it. A note change that is
  binary or too large to show cannot be approved this way (`not_reviewable`): deny it. It
  needs an interactive terminal and has no `--yes`.
- **What this protects against:** a client that only has this MCP server (over stdio or with
  a token) cannot approve. The terminal check and the code are friction against approving by
  habit, **not a barrier** against an agent that also has a shell as you: it can open a
  pseudo-terminal and type the code. Such an agent can already run `horae run`; if you rely
  on the approval, do not give the same client both this server and a shell.
- It applies **exactly** what the dry run planned. If the note it would change was edited, or
  the pipeline, its plugins, models or lockfile changed, it refuses (`refused`) and applies
  nothing; ask for a new dry run. The check is made again under the apply lock just before
  writing. Interrupting it (Ctrl-C) lets the apply finish; the approval records what the run
  did.
- An approval works **once**. It expires after 15 minutes by default
  (`horae mcp serve --approval-ttl MINUTES`, 1 to 60). Revoking the MCP token that asked
  revokes its pending approvals at once.
- What it applies is an ordinary run (`horae runs`), journalled; `horae undo <run>` reverts it.
- A client may have at most 3 requests waiting and make 12 requests an hour.

## Manual check with Claude Code

This check is manual on purpose: it needs a logged-in Claude Code and an instance with data, so
it is not part of CI. The automated tests (`tests/test_mcp_server.py`) cover the same tools with
an in-process client and with a real `horae mcp serve` child process over stdio.

1. Make an instance with a few runs: `horae init demo && horae run daily-fixture -i demo`
   (a task index fills when a pipeline proposes tasks; an empty list is a valid answer).
2. Add the server: `claude mcp add horae -- horae mcp serve -i "$PWD/demo"`.
3. Start `claude`, run `/mcp`: `horae` must be listed as connected with 4 tools
   (`tasks.today`, `brief.latest`, `runs.recent`, `pipeline.run`).
4. Ask: "Use the horae runs.recent tool and list the runs." The answer must show the run of
   step 1 with its pipeline, status and cost.
5. Ask: "Use horae tasks.today" and "Use horae brief.latest". Each returns without an error;
   `brief.latest` shows `run: null` unless a pipeline is named `daily-brief` (try
   `--brief-pipeline daily-fixture` in the `claude mcp add` command to see the run of step 1).
6. Confirm nothing changed: `horae runs -i demo` lists the same runs as before the questions.
7. Ask: "Use horae pipeline.run on daily-fixture". The answer shows a dry run and an approval
   id; `horae approval list -i demo` shows it `pending`, and nothing was applied. In your own
   terminal, `horae approval approve <id> -i demo` shows the changes and asks for the code it
   prints; type it. Claude has no MCP tool to approve. Note that if Claude Code also has its
   Bash tool, it could approve through a pseudo-terminal: the code is friction, not a barrier.
8. Remove it when done: `claude mcp remove horae`.

You can also run the server by hand: `horae mcp serve -i demo` waits for MCP messages on stdin
(end it with Ctrl-D).
