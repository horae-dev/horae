# Container image: pulling, running, verifying

- **Issue:** [#40](https://github.com/horae-dev/horae/issues/40); threat T24 in
  [docs/security.md](../security.md#b8-horaes-own-supply-chain).
- **Image:** `ghcr.io/horae-dev/horae`, for `linux/amd64` and `linux/arm64`.

## Tags

| Tag | Points at | Moves |
|---|---|---|
| `sha-<short>` (7 hex digits) | the image built from that commit of `main` | never |
| `edge` | the image of the newest commit of `main` | on every push to `main` (never backwards: a re-run of an older commit does not move it) |
| `X.Y.Z` | the `main` image of the commit tagged `vX.Y.Z` | never |
| `X.Y` | the newest `X.Y.Z` release | on each patch release |
| `X` | the newest `X.*` release, **from 1.0 only** | on each release; there is no floating `0` |

There is no `latest` tag in 0.x. Pin by digest in production
(`ghcr.io/horae-dev/horae@sha256:...`): tags can move, digests cannot.

How they are made:

- **Push to `main`**: the `publish` job of `.github/workflows/ci.yml`, after **CI OK** passed for
  that commit. Each platform is built and smoke-tested (`scripts/smoke-image.sh`; arm64 under
  QEMU), the multi-platform image is pushed **by digest, untagged**, its layers are checked to be
  the tested ones, its SBOM is checked, it is signed and the signature verified, and only then
  are `edge` and `sha-<short>` added. A tag never points at an untested or unsigned digest.
- **Tag `vX.Y.Z`**: `.github/workflows/image.yml` builds nothing. The tagged commit must be on
  `main`; the workflow takes the `sha-<short>` digest of that commit, checks its revision label
  and its signature, smoke-tests it (its `horae --version` must report `X.Y.Z`), then adds the
  release tags to that same digest. If `main` never published that commit (for instance its CI
  failed or was cancelled), the release fails: re-run that commit's CI on `main`, then the tag
  workflow. Only plain semver tags (`vX.Y.Z`) are releases; anything else fails.

`horae --version` reports `__version__` from `src/horae/__init__.py` at the image's commit: bump
it in the commit you tag (the release check refuses a mismatch). The image label
`org.opencontainers.image.version` holds the same value and `org.opencontainers.image.revision`
the commit.

## Verify before you run

Every published digest is signed with [cosign](https://github.com/sigstore/cosign) keyless signing
(GitHub OIDC, no long-lived key) by the `publish` job of `ci.yml` on `main`, and carries an SPDX
SBOM and build provenance for each platform. Release tags reuse that digest, so the identity is
the same for `edge`, `sha-*` and releases:

```
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/horae-dev/horae/.github/workflows/ci.yml@refs/heads/main \
  --certificate-github-workflow-repository horae-dev/horae \
  --certificate-github-workflow-trigger push \
  ghcr.io/horae-dev/horae@sha256:<digest>
```

Add `--certificate-github-workflow-sha <commit>` to also bind it to the commit you expect. The
signature is on the multi-platform index digest, which is what every tag points at.

Read the SBOM (one per platform):

```
docker buildx imagetools inspect ghcr.io/horae-dev/horae:edge --format '{{ json .SBOM }}'
```

## Running

The image runs as the non-root user `10001:10001`, under [tini](https://github.com/krallin/tini)
as PID 1 (`ENTRYPOINT ["tini", "--", "horae"]`), and works with a read-only root filesystem. It
declares no `VOLUME`: mount one on `/instance` yourself (`-v`), or your instance lives in the
container and is lost with it. The only writable places are:

- `/instance`, the instance directory: `horae.yaml`, `horae.lock`, `pipelines/`, `plugins/`,
  `state/` and `vault/`. `HORAE_INSTANCE=/instance` and it is the working directory. A new named
  volume starts owned by 10001 (mode 0700); a bind mount must be owned by uid 10001.
- `/tmp`, which must be a tmpfs (`HOME` and `TMPDIR` point there).

Create the instance once, then run the example pipeline:

```
docker volume create horae
opts=(--rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges
      -v horae:/instance)
docker run "${opts[@]}" ghcr.io/horae-dev/horae:edge init /instance
docker run "${opts[@]}" ghcr.io/horae-dev/horae:edge doctor
docker run "${opts[@]}" --network none ghcr.io/horae-dev/horae:edge run daily-fixture
```

The note lands in the volume's `vault/Daily/`. Compose equivalent: `read_only: true`,
`tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, a volume on
`/instance`.

The image has no pip, no ensurepip, no uv and no compiler.

### `horae serve`, the HTTP API and the health check

The default command is `horae serve` ([#37](https://github.com/horae-dev/horae/issues/37),
[decision 51](../../schemas/README.md#decisions)): the built-in scheduler and the HTTP API
(`GET /healthz`, `GET /runs`, `GET /runs/<id>`), read-only, on port 8080. The image sets
`HORAE_HOST=0.0.0.0` so that the reverse proxy can reach it on the container network. **The API
has no authentication of its own**: put it behind the proxy's SSO middleware (only `/act/` is
exempt, #41) and never publish the port (`-p 8080:8080` on a public interface exposes the run
list to anyone). Set `HORAE_HOST=127.0.0.1` when nothing else needs to reach it.

Set `HORAE_ALLOWED_HOSTS` to the public hostname the proxy serves Horae on (space-separated if
several, e.g. `HORAE_ALLOWED_HOSTS=horae.example.org`): the API refuses (`400 invalid_host`)
any request whose `Host` is not loopback or one of these, its defence against DNS rebinding.
Without it `horae serve` warns at start, and each refused `Host` is logged once. A neighbour
container calling `http://horae:8080/` needs `horae` in the list too (`_` is allowed, as in
Docker service names). The Host check is a defence against browsers, not neighbours: any
container on Horae's network can reach port 8080 and send any `Host`, so put Horae on a network
shared only with the proxy.

```
docker run -d --name horae "${opts[@]}" --stop-timeout 20 ghcr.io/horae-dev/horae:edge
docker inspect --format '{{.State.Health.Status}}' horae   # healthy once /healthz answers 200
```

Its `HEALTHCHECK` asks `/healthz` over loopback with the standard library (`python -c` and
`urllib`, no proxy; there is no curl or wget in the image): `200` when the store is readable and
the scheduler runs (or `HORAE_SCHEDULER=external`), `503` otherwise. A container running
another command (`init`, `doctor`, `run`) reports unhealthy, which is harmless for a one-off. If
you set `HORAE_PORT`, the check follows it; a `serve --port N` command does not (the check keeps
asking 8080), so change the port with `HORAE_PORT`. If you set `HORAE_HOST` to `::` (IPv6 only)
or to an address other than `0.0.0.0` or loopback, override the check. A healthy container does
not prove the proxy can reach Horae: the check uses a loopback `Host`, so a wrong
`HORAE_ALLOWED_HOSTS` only shows in the logs. Give the container at least 20 s to stop
(`--stop-timeout 20`, compose `stop_grace_period: 20s`).

### Stable paths, and upgrading

`/opt/venv` (the virtualenv, whose `bin/python` runs Horae) and `/instance` are a **stable
contract** of the image: they will not change between releases without a migration note.
`horae init` writes absolute paths into the instance: `horae.lock` holds the vault's path
(`/instance/vault`), and `plugins/<name>/plugin.yaml` runs each built-in plugin as
`/opt/venv/bin/python -m horae.plugins.builtin.<module>`.

On an image upgrade, nothing in the instance is rewritten. The built-in plugins' **code** is the
new image's (their command runs the image's interpreter), but their installed manifests in
`plugins/` and their pins in `horae.lock` (version `0.1.0`, a placeholder commit, the approved
permissions) stay as `init` wrote them. Horae checks that each installed manifest matches its pin,
so the instance keeps working; it does not notice that the image ships a newer built-in plugin
version, and v1 has no command to reinstall them (plugin install and verify is #44, #46).
**Manual step** when a release notes a built-in plugin version or permission change: run
`horae init` into a new empty volume, review the new `plugins/` and `horae.lock` entries, and copy
them into your instance (or move your `pipelines/`, `vault/` and `state/` to the new one).

## Testing locally

`scripts/smoke-image.sh IMAGE [PLATFORM]` is the CI smoke test: non-root, PID 1 is tini,
`--version` format (and `EXPECTED_VERSION` if set), a read-only root filesystem probed as root
against a writable control, no pip/ensurepip/uv/compiler, then `init`, `doctor` and
`run daily-fixture` on named volumes with no network, and finally the default `horae serve`:
healthy by the image's own `HEALTHCHECK`, `/runs` answering, exit 0 on `docker stop`. Run it after `docker build -t horae:test .`;
pull requests run it on native amd64.
