# ADR 0004: Git marketplace with commit-SHA pinning

- **Status:** Proposed
- **Date:** 2026-10-27

## Context

Plugins carry secrets and can propose writes, so how they are distributed is a security decision.
PLAN.md notes that public skill hubs have carried malware (OpenClaw's, in 2026) and wants plugin
trust to rest on "declared, enforced permissions; installed from git and pinned to a commit"
([§1](../PLAN.md#1-why-horae-the-gap)). Horae is a personal, self-hosted project with no hosted
service ([§2.3](../PLAN.md#23-non-goals-v0x)), so it should not depend on running a registry.

## Decision

Horae distributes plugins through git-based marketplaces and installs each one pinned to a commit
SHA; there is no central registry.

1. **No central registry.** A marketplace is a git repository with a `marketplace.json` that maps a
   plugin name to its repo, its versions (each pinned to a commit SHA) and a tier. The official one
   is `horae-dev/marketplace`; any git URL can be configured, so anyone can run their own
   ([§4](../PLAN.md#4-marketplace-git-only)).
2. **`horae plugin add <name>@<version>`**:
   1. resolves the name in the configured marketplaces;
   2. `git clone`s the repo and checks out the pinned **commit SHA**, refusing if the tag and the
      SHA disagree. The clone runs as `git clone -- <repo>` with `protocol.allow` restricted to
      `https`
      ([schemas README](../../schemas/README.md#strings-that-reach-the-outside-world));
   3. shows the requested permissions and asks for approval;
   4. writes `horae.lock` (name, version, SHA, approved permissions);
   5. installs the plugin's dependencies in **its own venv** with `uv`.
3. **Direct installs** without a marketplace: `horae plugin add https://…/plugin-x@<sha>`.
4. **Nothing updates itself.** `upgrade` shows a permissions diff; new hosts, secrets or intents
   need a new approval. `audit` lists installed plugins, their permissions and last activity.
5. The marketplace repo's CI validates every entry (clone, check SHA, run the plugin's tests
   against the latest core) ([§6](../PLAN.md#6-repositories)).

The lockfile contract is [`schemas/lockfile/v1.json`](../../schemas/lockfile/v1.json), generated
from [`src/horae/contracts/lockfile.py`](../../src/horae/contracts/lockfile.py): per plugin,
`version`, `repo` (`https://` only), `commit` (a full 40- or 64-hex commit id, never a short SHA or
a tag) and the approved `permissions`. It has a `schema_version`, and its canonical form sorts
plugins and permission lists so that a lockfile diff shows only real changes. Run records carry the
`commit` of each plugin used (when installed from git) and an optional `lock_digest`
([`schemas/run-record/v1.json`](../../schemas/run-record/v1.json)).

## Consequences

### Positive

- Installs are reproducible and tamper-evident: a moved tag or a rewritten branch does not change
  what runs, and a tag/SHA mismatch is refused (M4 "done when": a clean instance refuses a tampered
  SHA, [M4](../PLAN.md#m4--marketplace-1-week)).
- Permission changes are always visible and always need approval.
- No infrastructure to run; forks and private marketplaces work the same way.

### Negative

- Discovery and search are limited to what a `marketplace.json` lists.
- Pinning to a commit trusts the git host and the repo at that commit; until signed tags exist, a
  compromised marketplace can point a name at a malicious commit (the threat model lists "a
  compromised marketplace", [§9](../PLAN.md#9-quality-and-security-practice)). Approval of the
  permissions shown at install is the user's main defence.
- Users must upgrade manually; security fixes in plugins do not arrive on their own.
- One venv per plugin costs disk space.

### Open questions

- `marketplace.json` has no schema yet; only `horae.lock` is specified in M0.
- The lockfile syntax is not given in PLAN.md (the example is YAML); the schema validates the
  parsed document and adds `repo`, which PLAN's list (name, version, SHA, approved permissions)
  does not mention ([schemas decision
  20](../../schemas/README.md#decisions-provisional-maintainer-to-confirm)).
- Signed tags (gitsign or sigstore) and a "tests pass on horae X.Y" badge are "later" in §4.

## Alternatives considered

- **PyPI or another package index.** Ready-made tooling, but Python-only, and a version is a name
  the publisher controls, not a commit of the source; Horae wants plugins in any language, pinned
  to a commit ([§1](../PLAN.md#1-why-horae-the-gap), [§4](../PLAN.md#4-marketplace-git-only)).
- **A hosted Horae registry.** Better discovery, but a service to run and a single point of
  compromise, at odds with a self-hosted, non-commercial project.
- **Pin to tags only.** Readable, but tags can move; the SHA is the identity, the tag is checked
  against it.

## References

- [PLAN.md §1](../PLAN.md#1-why-horae-the-gap), [§4](../PLAN.md#4-marketplace-git-only),
  [§6](../PLAN.md#6-repositories), [§9](../PLAN.md#9-quality-and-security-practice),
  [M4](../PLAN.md#m4--marketplace-1-week)
- [`schemas/lockfile/v1.json`](../../schemas/lockfile/v1.json),
  [`schemas/examples/lockfile/valid/horae.lock.yaml`](../../schemas/examples/lockfile/valid/horae.lock.yaml)
