# Horae as an MCP server (read-only)

`horae mcp serve` lets an MCP client (Claude Code, another agent, a phone chat app) look at a
Horae instance without becoming its brain. This is the read-only part (#54) and its token
authentication (#56); `pipeline.run` with approval (#55) is a separate story. Design:
[decision 65](../schemas/README.md#provisional-decisions-54-pending-maintainer-review) and
[decision 75](../schemas/README.md#provisional-decisions-56-pending-maintainer-review), threats
[T17, T42, T43](security.md).

- **Transport: stdio by default.** The client starts `horae mcp serve` and talks to it over
  the process's stdin and stdout. Horae opens no port and asks for no token: whoever can start
  the command can already read the instance's store.
- **Or HTTP with a token.** `horae mcp serve --http` serves the same tools over streamable
  HTTP on `127.0.0.1:8765`, and every request needs a bearer token (see
  [Over HTTP with a token](#over-http-with-a-token)).
- **Read-only.** Every call opens the store read-only and closes it. No tool takes a path, a
  command or a secret, and none changes a note, a task or a run.
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

   Scopes: `read` (default; the three tools) and `pipeline.run` (reserved for #55, which only
   lets a client ask for a dry run; approving stays yours). Add `--scope pipeline.run` only once
   #55 ships and you want that client to use it.
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

## Manual check with Claude Code

This check is manual on purpose: it needs a logged-in Claude Code and an instance with data, so
it is not part of CI. The automated tests (`tests/test_mcp_server.py`) cover the same tools with
an in-process client and with a real `horae mcp serve` child process over stdio.

1. Make an instance with a few runs: `horae init demo && horae run daily-fixture -i demo`
   (a task index fills when a pipeline proposes tasks; an empty list is a valid answer).
2. Add the server: `claude mcp add horae -- horae mcp serve -i "$PWD/demo"`.
3. Start `claude`, run `/mcp`: `horae` must be listed as connected with 3 tools
   (`tasks.today`, `brief.latest`, `runs.recent`).
4. Ask: "Use the horae runs.recent tool and list the runs." The answer must show the run of
   step 1 with its pipeline, status and cost.
5. Ask: "Use horae tasks.today" and "Use horae brief.latest". Each returns without an error;
   `brief.latest` shows `run: null` unless a pipeline is named `daily-brief` (try
   `--brief-pipeline daily-fixture` in the `claude mcp add` command to see the run of step 1).
6. Confirm nothing changed: `horae runs -i demo` lists the same runs as before the questions.
7. Remove it when done: `claude mcp remove horae`.

You can also run the server by hand: `horae mcp serve -i demo` waits for MCP messages on stdin
(end it with Ctrl-D).
