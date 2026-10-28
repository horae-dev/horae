# Vendored fixtures

## graft.v1.schema.json

- Source repository: https://github.com/mmornati/graft
- Source path: `schema/graft.v1.schema.json`
- Commit: `3f40dd101286bb95c4b356eaf10a9e163bf1903d`
- Vendored with the permission of its author: `mmornati/graft` is the maintainer's own project,
  and Graft is the review service used on this repository.

It is used only by `tests/test_graft_policy.py` to validate `graft.yaml` offline.

## instance/

A synthetic Horae instance (`horae.yaml`, `pipelines/`, `horae.lock`) for the configuration
loader and `horae doctor` tests. The pipelines and the lockfile are copies of examples in
`schemas/examples/`. It holds no secret values: tests create them in a temporary directory.

## plugins/

Synthetic plugins for the plugin host tests (#24), both run with the test interpreter
(`sys.executable`) and touching nothing outside their own process:

- `probe/`: a stdio MCP server built with the `mcp` SDK (`server.py`) and its `plugin.yaml`.
  Its tools report the environment, sleep, crash, write stderr, propose intents through
  `_meta["dev.horae/intents"]`, echo values and start a child process. The secret it declares
  (`PROBE_TOKEN`) is a synthetic value the tests write to a temporary secrets directory.
- `raw/raw_server.py`: a hand-written JSON-RPC server that misbehaves on purpose (junk and
  oversized messages, error responses, a sampling request, a startup that never answers, a
  process that ignores `SIGTERM`).

## llm/anthropic/

Recorded-response fixtures for the `anthropic` backend tests (#28,
`tests/test_llm_anthropic.py`): synthetic Messages API answers, each `{status, body}`, served
by an `httpx2.MockTransport` (no network). Success (with and without prompt-cache tokens),
refusal, `max_tokens` truncation, tool use, text that is not JSON, and the error bodies of
HTTP 400, 401, 429, 500 and 529. Ids, texts and token counts are invented.
