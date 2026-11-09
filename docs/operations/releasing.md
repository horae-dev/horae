# Releasing

- **Issue:** [#58](https://github.com/horae-dev/horae/issues/58).
- **Scope:** the `horae` package (PyPI) and the `ghcr.io/horae-dev/horae` image, tag `vX.Y.Z`.
  `horae-sdk` is released separately by `release-sdk.yml` on `sdk-v*` tags.

Versions are [semver](https://semver.org/). Plain `X.Y.Z` only: no pre-release suffix is
published (the release workflow refuses `v0.2.0rc1`). In 0.x, any minor may break.

## Release steps (maintainer)

1. **Bump the version.** Edit `__version__` in `src/horae/__init__.py` (the single source; hatch
   reads it for the sdist and wheel, and the image reports it as `horae --version`).
2. **Generate the changelog section.**
   ```
   scripts/changelog.sh X.Y.Z
   ```
   It asks GitHub for the notes of the pull requests merged since the previous `vX.Y.Z` tag,
   grouped by label (`.github/release.yml`: Security, Features, Fixes, Documentation, Other), and
   prepends `## vX.Y.Z - <date>` to `CHANGELOG.md`. Read it, fix wording, and drop what does not
   belong. That text becomes the body of the GitHub Release as is.
3. **Open a pull request** with both changes, merge it (CI OK must pass). The merge commit must
   publish its image: wait for the `publish` job of CI on `main` to finish for that commit.
4. **Dry run (optional, no side effects).** Actions > Release > Run workflow, branch `main`,
   `tag: vX.Y.Z`, `dry_run: true`. It checks the tag format, that it equals `__version__`, and that
   `CHANGELOG.md` has the section, and prints the release notes. It builds, signs and publishes
   nothing and uses no environment. A manual run cannot do a real release.
5. **Tag the merge commit** (admins only, by the "Release tags" ruleset) and push the tag:
   ```
   git tag -s vX.Y.Z <merge-commit> && git push origin vX.Y.Z
   ```

## What runs

`.github/workflows/release.yml`, one run per tag (`concurrency` per tag, never cancelled):

| Job | Does | Permissions |
|---|---|---|
| `verify` | tag is `vX.Y.Z`, equals `__version__`, commit is on `main`, `CHANGELOG.md` has the section; `uv build` of the sdist and wheel; extracts the notes | `contents: read` |
| `publish` | uploads the two files to PyPI with trusted publishing and PEP 740 attestations. Environment **`pypi-core`**: waits for the required reviewer | `id-token: write` |
| `image` | `image.yml`: adds `X.Y.Z` and `X.Y` to the signed `main` image of that commit after re-verifying its signature and smoke-testing it. See [container-image.md](container-image.md) | `packages: write` |
| `github-release` | after both, creates the GitHub Release `vX.Y.Z` with the changelog section and the dists attached | `contents: write` |

A failed upload never overwrites: `skip-existing` is off, and PyPI refuses a file that exists. If
`publish` fails after the image was tagged (or the other way round), fix the cause and re-run the
failed jobs of the same run; do not move the tag. A version already on PyPI cannot be reused:
bump to the next patch.

### Environment approval

The `publish` job pauses at the `pypi-core` environment until a required reviewer approves it.
Check that the run is for the tag you pushed, and that `verify` printed the notes you expect,
before approving. The environment only accepts runs for `v*` tags (deployment tag rule), so a
branch or an `sdk-v*` run cannot get the PyPI OIDC token for `horae`. The `pypi` environment stays
limited to `sdk-v*` and `horae-sdk`.

## Verify a release

### Image signature (cosign)

The image digest is signed keyless by the `publish` job of `ci.yml` on `main` (release tags reuse
that digest):

```
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/horae-dev/horae/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-github-workflow-repository horae-dev/horae \
  --certificate-github-workflow-trigger push \
  ghcr.io/horae-dev/horae:X.Y.Z
```

Pin by digest (`ghcr.io/horae-dev/horae@sha256:...`) in production; the regexp is the exact
workflow identity, anchored. Add `--certificate-github-workflow-sha <commit>` to bind it to the
tagged commit.

### PyPI attestations

The release files carry a PEP 740 attestation made by the publish job (Sigstore, bound to
`horae-dev/horae`, workflow `release.yml`, environment `pypi-core`). On the
[project page](https://pypi.org/project/horae/) each file shows "Provenance". To check one:

```
uv run --with pypi-attestations pypi-attestations verify pypi \
  --repository https://github.com/horae-dev/horae \
  pypi:horae-X.Y.Z-py3-none-any.whl
```

Compare the wheel with the one attached to the GitHub Release (`sha256sum`).

## One-time set-up (repository owner)

- A ruleset "Release tags" restricts `v*` and `sdk-v*` tags to admins (done).
- **Environment `pypi-core`**: required reviewer, deployment tags restricted to `v*` (no branch
  rule), no secrets. The existing `pypi` environment (`sdk-v*`) is unchanged.
- **PyPI trusted publisher** for project `horae`: owner `horae-dev`, repository `horae`, workflow
  `release.yml`, environment `pypi-core` (a pending publisher if the project does not exist yet).
- The `ghcr.io/horae-dev/horae` package public ([#66](https://github.com/horae-dev/horae/issues/66)).
- Labels: create `security` (used by `.github/release.yml` for the Security section) and
  optionally `skip-changelog`. A pull request without a matching label lands in "Other changes".

Commits here carry no conventional prefix (`feat:`), so GitHub's notes are grouped by pull request
label only. Label pull requests before merging them.
