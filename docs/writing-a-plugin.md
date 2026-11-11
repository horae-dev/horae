# Writing a plugin

A Horae plugin is an MCP server over stdio that the core starts for one run. It returns data
and **proposes intents**; it never writes anything itself
([ADR 0002](adr/0002-intents-applied-by-the-core.md),
[ADR 0003](adr/0003-mcp-as-the-plugin-protocol.md)). You can write one in any language that
speaks MCP; the Python SDK, `horae-sdk`, does the protocol work for you.

!!! warning "Provisional"
    The plugin contract (schemas decisions 35 and 36 and the manifest) is provisional, and the
    SDK is 0.x: its API can still change between minor versions. The SDK's changelog notes
    are in the [SDK reference](reference/sdk.md). Installing a plugin with `horae plugin add`
    is not on `main` yet ([concepts](concepts.md#the-marketplace)).

## 1. Start from the template

[`horae-dev/template-plugin`](https://github.com/horae-dev/template-plugin) is a GitHub
template repository: "Use this template" gives a repository whose CI passes unchanged. It
holds an example plugin (a source that proposes a `task.create` per message), a `plugin.yaml`
checked by the SDK's helpers, recorded-input tests, and CI. Rename the example with
`scripts/rename.py`, then write your own tools. [What the template holds and how to use
it](plugins/template.md).

## 2. The roles

| Role | Declared in `plugin.yaml` | SDK | Called by |
|---|---|---|---|
| source | `provides.sources` | `@plugin.source(ref)` or a `Source` subclass | a `collect` step |
| transform | `provides.transforms` | `@plugin.transform(ref)` or a `Transform` subclass | a `transform` step |
| sink | `provides.sinks` | a `Sink` subclass | the core, to apply intents |
| notifier | `provides.notifiers` | a `Notifier` subclass | the core, for `notify` |

A minimal source, from the SDK's `examples/hello`:

```python
from horae_sdk import Call, Output, Permissions, Plugin, intents

plugin = Plugin("hello", version="0.1.0", permissions=Permissions(intents=["task.create"]))


@plugin.source("hello.greet")
def greet(call: Call, name: str = "world") -> Output:
    task = intents.task_create(f"Greet {name} back", key=intents.fingerprint("hello", name))
    return Output({"greeting": f"Hello, {name}!"}, intents=[task])


if __name__ == "__main__":
    plugin.run()
```

Rules that matter, in the order they bite:

- **Never `print`**: stdout is the protocol channel. Log to stderr.
- A tool's parameters are the pipeline's **trusted** configuration. `call.input` (a
  transform's input, the previous step's data) is **untrusted**: it may be a mail. Never take
  a path to read or write from it.
- Build every note or task from untrusted text with the SDK's `intents` builders
  (`task_create` escapes titles for Obsidian Tasks).
- Return data that is JSON. Raise `PluginError("message")` for an error the user should see.

## 3. Declare the least you need

`plugin.yaml` is where a plugin states its permissions, which the user approves and the core
pins in `horae.lock`:

```yaml
name: github
version: 0.1.0
provides:
  sources: [github.review_requests]
permissions:
  network: [api.github.com]
  secrets: [GITHUB_TOKEN]
  paths: {}
  intents: []
runtime: { protocol: mcp-stdio, command: ["python", "-m", "horae_github"] }
```

Generate it from the code (`plugin.manifest(...)`, `dump_manifest`) rather than by hand, and
let a test compare the two.

!!! note "Third-party plugins: a module command, static metadata, no build"
    A plugin that is not first-party (any index but the Horae marketplace, or a direct install
    from a git URL) runs only in a container, and Horae **never builds its project**: only the
    dependencies of its `uv.lock` are installed, from hashed wheels, and its sources are
    imported from its tree (`src/<module>` if it has a `src/` directory, else the tree root).
    So `setup.py`, build hooks and console scripts never run or exist. Such a plugin must:

    - start with the venv's interpreter running a module: `command: ["python", "-m",
      "<module>"]` (a console-script command is refused at install);
    - declare its `[project]` metadata statically (no `dynamic = [...]`: uv would run the
      build backend to compute it, so it is refused).

    See [plugin containers](operations/plugin-containers.md#installing-third-party-plugins).

## 4. SDK 0.6 APIs

| API | Use |
|---|---|
| `Plugin`, `Permissions` | register tools; declare `intents`, `network`, `network_local`, `secrets`, `optional_secrets`, `read` and `write` paths |
| `Call`, `Output`, `PluginError` | a tool's request and result; `call.input`, `call.paths` |
| `call.host_allowed(url)` | `True`, `False` or `None` (unknown core): is this `https` URL on a host approved in `horae.lock`? Check the URL you are about to connect to, after redirects |
| `network_local=["127.0.0.1:1143"]` (0.6) | a local-network service (Home Assistant, a self-hosted ntfy, a mail bridge), approved as `host:port`; `call.host_allowed` also accepts `http` there; `call.address_allowed(host, port)` for non-HTTP protocols |
| `intents.*` | `note_create`, `note_append_section`, `note_replace_owned_block`, `task_create`, `task_update_status`, `notify_push`, `http_post`; `intents.fingerprint(...)` for stable task identities |
| `Sink`, `Notifier`, `Applied` | `identity`, `apply`, and for note sinks `read`, `restore`, `parent_exists`; `restore_status` makes a status change undoable |
| `horae_sdk.testing` | `replay_all`, `call`, `operation`: recorded-input tests |

Details, with the edge cases (the exact host matching rules, Obsidian Tasks escaping,
reminder order, upgrading a sink from 0.4): [SDK reference](reference/sdk.md).

## 5. Test with recorded inputs

A recording is one call as the core makes it, and what the plugin must answer. The harness
replays it through MCP, with the same arguments and `_meta` keys as the real host:

```python
from pathlib import Path

from horae_sdk.testing import replay_all


def test_recordings() -> None:
    replay_all(plugin, Path(__file__).parent / "recordings")
```

`HORAE_SDK_RECORD=1 pytest` rewrites the recordings from the current outputs (refused in CI;
review the diff). **Recordings must be synthetic**: no personal data, no secret. Include a
hostile input (a subject with markup or a newline) to see it neutralized.

## 6. Publishing to the marketplace

The index is [`horae-dev/marketplace`](https://github.com/horae-dev/marketplace): an entry
maps a plugin name and a semver to the **full commit SHA** that the tag `v<version>` points
to. Release a tagged version first, then open a pull request that adds the entry; the
marketplace's CI clones the repository, checks that the pinned commit exists and matches the
tag, and validates the `plugin.yaml` there against the core's validator.

The marketplace's contribution guide and review policy for third-party plugins is
[`CONTRIBUTING.md`](https://github.com/horae-dev/marketplace/blob/main/CONTRIBUTING.md)
(proposed in [marketplace#10](https://github.com/horae-dev/marketplace/pull/10); it is a 404
until that pull request is merged). Read it before you open your first entry, and read the
[security model](security.md) for what a reviewer looks at: permissions, untrusted input
handling and supply chain (threat boundary B6).

You do not need the marketplace to try a plugin: the planned `horae plugin add
https://github.com/you/plugin-x@<sha>` installs from any git URL at a pinned commit.
