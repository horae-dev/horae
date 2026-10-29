# Container image: pulling, running, verifying

- **Issue:** [#40](https://github.com/horae-dev/horae/issues/40); threat T24 in
  [docs/security.md](../security.md#b8-horaes-own-supply-chain).
- **Image:** `ghcr.io/horae-dev/horae`

## Tags

| Tag | Moves | Use |
|---|---|---|
| `edge` | every push to `main` | trying the latest main |
| `sha-<short>` | never | pinning to a commit |
| `X.Y.Z`, `X.Y`, `X` (`X` only from 1.0) | `v*` tags | releases |

Pin by digest in production (`ghcr.io/horae-dev/horae@sha256:...`); tags can move, digests cannot.

## Verify before you run

Every pushed image is signed with [cosign](https://github.com/sigstore/cosign) keyless signing
(GitHub OIDC, no long-lived key) and carries an SBOM and build provenance. CI verifies the
signature right after signing; verify it yourself too:

```
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/horae-dev/horae/\.github/workflows/image\.yml@refs/(heads/main|tags/v.+)$' \
  ghcr.io/horae-dev/horae@sha256:<digest>
```

Read the SBOM:

```
docker buildx imagetools inspect ghcr.io/horae-dev/horae:edge --format '{{ json .SBOM }}'
```

## Running

The image runs as the non-root user `10001:10001` and works with a read-only root filesystem.
The only writable places are:

- `/instance`, a declared volume: the instance directory (`horae.yaml`, `pipelines/`, `state/`,
  `secrets/`). `HORAE_INSTANCE=/instance` and it is the working directory. A bind mount must be
  owned by uid 10001.
- `/tmp`, which must be a tmpfs (`HOME` and `TMPDIR` point there).

```
docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -v horae-instance:/instance ghcr.io/horae-dev/horae:edge run daily-brief
```

Compose equivalent: `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, a volume on `/instance`.
The image has no shell tools beyond the base distribution's, no pip, no uv and no compiler. Its
`HEALTHCHECK` runs `horae --version` until the HTTP API ([#37](https://github.com/horae-dev/horae/issues/37))
gives it `/healthz`.

`scripts/smoke-image.sh IMAGE` is the CI smoke test (non-root, read-only, no network, `--version`
and `--help`); run it locally after `docker build -t horae:test .`.
