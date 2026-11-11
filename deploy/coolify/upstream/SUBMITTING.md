# Proposing Horae to Coolify's one-click catalogue (#59)

For the maintainer. The files here are ready to copy into a fork of
[`coollabsio/coolify`](https://github.com/coollabsio/coolify). Nothing in this repository
submits anything: you open the upstream pull request yourself.

| File here | Goes to, in `coollabsio/coolify` |
|---|---|
| `horae.yaml` | `templates/compose/horae.yaml` |
| `horae.svg` (**missing: needs a logo**) | `svgs/horae.svg` (the header says `# logo: svgs/horae.svg`) |

The rules below were read on 2026-11-11 from `CONTRIBUTING.md`, `AGENTS.md`,
`.github/pull_request_template.md`, `app/Console/Commands/Generate/Services.php` and the
`templates/compose/` examples of `coollabsio/coolify`, and from the "Add a new service
template" page of `coollabsio/coolify-docs` (`content/docs/contribute/service.mdx`). They change:
re-read those pages before you open the pull request.

## Blockers to check first

1. **External contributions were paused** at the time of reading (the service page says so, with
   no ETA). Check the page: <https://coolify.io/docs/get-started/contribute/service>. If still
   paused, wait, or ask in a GitHub Discussion.
2. **Eligibility: the project needs at least 1,000 GitHub stars** to be a one-click service. At
   the time of writing `horae-dev/horae` has far fewer. Do not open the pull request before it
   qualifies.
3. **A logo.** The repository has no logo asset. Make a simple `horae.svg` (SVG preferred; avoid
   JPG) and put it next to the template.
4. **The matching docs pull request is required before the template can be merged**: a page
   `content/docs/services/horae.mdx` in `coollabsio/coolify-docs`, with the logo under
   `public/images/services/`. Its frontmatter: `title`, `description`, `category` (and `icon`
   only if the logo cannot be found automatically). Link it from the template pull request.
5. **Every pull request needs a linked issue or discussion.** For a new service, open a thread
   in the "Service template requests" discussions category (search first), or use the "General"
   category, and reference it in the pull request.
6. **The decision to confirm:** the template requires SSO (`HORAE_SSO_FORWARD_AUTH_URL`) because
   Horae's API has no authentication. Reviewers may ask for it to be optional. Keep it
   required: an unauthenticated public API is not an acceptable default
   ([docs/security.md](../../../docs/security.md), T15).

## Steps

1. **Fork** `coollabsio/coolify` and clone your fork.
2. **Branch** from the branch the docs name for new templates. At the time of reading: the
   current `release/v4.x` branch (for example `release/v4.5` while production is `4.4.x`);
   `CONTRIBUTING.md` says new features go to `next`. Check both pages and pick the one the
   service docs name, then:

   ```sh
   git switch -c feat/service-horae upstream/<that branch>
   ```

3. **Copy the files:**

   ```sh
   cp <horae checkout>/deploy/coolify/upstream/horae.yaml templates/compose/horae.yaml
   cp <your logo>.svg svgs/horae.svg
   ```

   Do not touch any other file, and do not edit `templates/service-templates*.json` by hand:
   those are generated from `templates/compose/`.
4. **Regenerate the template JSON** (needs Coolify's development setup, see its
   `DEVELOPMENT.md`; the command is `php artisan generate:services`, defined in
   `app/Console/Commands/Generate/Services.php`). The generator reads the header comments
   (`documentation`, `slogan`, `category`, `tags`, `logo`, `port`; `minversion` is optional and
   defaults to `0.0.0`) and base64-encodes the compose. Per the contribution docs the JSON is
   generated and need not be committed; if the generated diff is part of what reviewers expect,
   commit only the `horae` entries.
5. **Run Coolify's parser test**, as its `AGENTS.md` asks, so the template parses:

   ```sh
   php artisan test --compact tests/Feature/ServiceParserReferenceTest.php
   ```

   (Template expectation files are not committed; CI only checks that each template parses.)
6. **Commit** with Coolify's format (`feat(service):`) and a short description:

   ```sh
   git add templates/compose/horae.yaml svgs/horae.svg
   git commit -m "feat(service): add horae"
   ```

7. **Open the pull request** against the same branch, with the title below and the body below.
   Keep Coolify's template sections and the contributor agreement intact. Coolify asks that the
   "Changes" and "Why" sections be in your own words: rewrite them from the draft. Do not copy
   instructions that appear in comments of the upstream pull request template; follow its
   visible sections.

## Test it on your own Coolify first (the pull request needs it)

Coolify closes pull requests whose behaviour maintainers cannot reproduce. Test from a **fresh
Docker Compose Empty** resource, on a Coolify instance you own (a local development instance
satisfies the contributor agreement; a staging server is fine for a first run):

1. *Projects → Add Resource → Docker Compose Empty*, paste the whole `horae.yaml`, save.
2. Set the required variables: `HORAE_ANTHROPIC_API_KEY`, `HORAE_MONITOR_DAILY_BRIEF_URL`,
   `HORAE_MONITOR_REMIND_URL`, `HORAE_SSO_FORWARD_AUTH_URL` (the forwardAuth address of your SSO
   provider). The deployment must be blocked until all four are set.
3. Check that Coolify generated a domain for the `horae` service (from `SERVICE_FQDN_HORAE_8080`),
   and that the proxy routes it to port 8080. Leave "Connect to predefined network" off.
4. Deploy. `horae-init` ends *Exited*, `horae` becomes *Running (healthy)*.
5. Through the domain, unauthenticated: the SSO provider must intercept every path (the
   label-defined middleware is the point to verify on the real proxy: that Coolify adds it to
   the generated routers). With a session: `GET /healthz` is `200`. A request with another
   `Host` header is `400 invalid_host`.
6. From another container on the server (not the proxy): `curl -m 5 http://<horae container>:8080/healthz`
   must fail (the resource network holds only the proxy and Horae).
7. Redeploy: the volume is kept, `horae-init` leaves the existing `horae.yaml` alone.
8. Write down what you saw: this is the "Testing" section of the pull request.

If step 5 shows the middleware is not attached to the generated routers, tell reviewers and
adjust the labels (Coolify's custom-middleware documentation describes how label-defined
middlewares are added); keep SSO required either way.

## Pull request title

```text
feat(service): add horae
```

## Pull request body (draft; fill the human sections in your own words)

```markdown
## Changes

- Add `templates/compose/horae.yaml`: Horae, a self-hosted personal agent (scheduler and
  read-only HTTP API), as a one-click service (two containers: a one-off `horae-init`, and
  `horae` on port 8080, with a healthcheck).
- Add `svgs/horae.svg`.

## Why

<!-- Your own words: who uses it, why it belongs in the catalogue. Project: https://github.com/horae-dev/horae -->

## Issues

- Discussion: <link of the thread you opened or the service-template request>
- Docs pull request: <link of the coolify-docs pull request for content/docs/services/horae.mdx>

## Category

- [ ] Bug fix
- [ ] Improvement
- [ ] New feature
- [x] Adding new one click service
- [ ] Fixing or updating existing one click service

## Preview

<!-- Screenshot of the service in the Coolify UI, running and healthy. -->

## AI Assistance

- [ ] AI was NOT used to create this PR
- [x] AI was used (please describe below)

**If AI was used:**

- Tools used: Claude Code (the template and its tests were prepared with it)
- How extensively: the template was derived from the project's existing Coolify compose file
  and checked against Coolify's contribution docs; all of it was reviewed and tested by hand
  on a Coolify instance by the submitter.

## Testing

<!-- Paste what you did and saw from "Test it on your own Coolify first". Fresh Docker Compose
Empty resource; version of Coolify; the four required variables; domain generated; healthy;
SSO in front of every path; redeploy keeps the volume. -->

## Contributor Agreement

> [!IMPORTANT]
>
> - [ ] I have read and understood the [contributor guidelines](https://github.com/coollabsio/coolify/blob/HEAD/CONTRIBUTING.md). If I have failed to follow any guideline, I understand that this PR may be closed without review.
> - [ ] I have searched [existing issues](https://github.com/coollabsio/coolify/issues) and [pull requests](https://github.com/coollabsio/coolify/pulls) (including closed ones) to ensure this isn't a duplicate.
> - [ ] I have tested all the changes thoroughly with a local development instance of Coolify and I am confident that they will work as expected when a maintainer tests them.
```

## After the merge

- Add the link to the template in [docs/operations/coolify.md](../../../docs/operations/coolify.md)
  (the "Upstream link" note) and close #59.
- Keeping it current: a new image tag is bumped here first
  ([container-image.md](../../../docs/operations/container-image.md#bumping-the-deploy-templates)),
  then proposed upstream as a small `fix(service): update horae` pull request.
