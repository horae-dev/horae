# Deploying Horae on Coolify

- **Issue:** [#41](https://github.com/horae-dev/horae/issues/41); threat T15 in
  [docs/security.md](../security.md); [PLAN §7](../PLAN.md#7-reference-deployment-coolify).
- **Template:** [`deploy/coolify/docker-compose.yaml`](../../deploy/coolify/docker-compose.yaml),
  a Coolify **Docker Compose Empty** service. Plain Docker: [compose.md](compose.md).
- **Image:** `ghcr.io/horae-dev/horae` ([container-image.md](container-image.md)).

> **Check first that the image can be pulled without credentials.** The template pulls
> `ghcr.io/horae-dev/horae` anonymously, which works only once the GHCR package is **public**
> (a setting of the `horae-dev` organisation, not of this repository). Check from any machine:
>
> ```sh
> curl -s -o /dev/null -w '%{http_code}\n' \
>   'https://ghcr.io/token?scope=repository:horae-dev/horae:pull'
> ```
>
> `200`: public, nothing to do. `401`: not anonymously pullable, and the deployment fails at
> the pull (*denied* / *unauthorized*) until you give the Coolify server
> [registry credentials](#registry-credentials-private-image).

The reference deployment is one Coolify server with its Traefik proxy and these resources:

| Resource | What | SSO |
|---|---|---|
| `horae` | this template: `horae serve` (the read-only API; with Scheduled Tasks, no built-in scheduler) | yes, except `/act/` |
| SSO provider | for example authentik (Coolify one-click service) and its outpost | it *is* the SSO |
| `vault-sync` | the Coolify one-click "Obsidian LiveSync" service (CouchDB), E2EE and path obfuscation on, its own domain | **no** (the plugin cannot do SSO) |
| ntfy | self-hosted ntfy with an iOS upstream | ntfy's own auth |
| Uptime Kuma | the push monitors of the scheduled runs | Uptime Kuma's own auth |

Only `horae` is in this template; the others are separate resources from Coolify's catalogue.

## What the template does

- Two services. `horae-init` is a one-off (`restart: "no"`, so Coolify leaves it out of the
  stack's health): on a new, empty volume it runs `horae init /instance` (the example instance,
  the walking skeleton) and exits; when `horae.yaml` is already there it does nothing.
  `horae` starts once it has exited 0, with the image's own command (`horae serve`), user
  (`10001`), tini and `HEALTHCHECK` (`/healthz` over loopback): a fresh deployment is healthy.
- `/instance` is a named volume (`horae-instance`; Coolify prefixes it with the resource's UUID).
  Root filesystem read-only, a tmpfs on `/tmp`, every capability dropped, `no-new-privileges`,
  `stop_grace_period: 30s` (runs in progress are saved and their monitors pinged on SIGTERM;
  [decision 49](../../schemas/README.md#decisions) E2 asks for at least 20 s).
- **No published port** and **no `networks:`**: Coolify puts the stack on a network of its own
  (named after the resource's UUID) and connects its proxy to it. That network is shared only by
  this stack and the proxy, which is what [decision 51](../../schemas/README.md#decisions) B
  needs: the API trusts its network (a neighbour container could send any `Host` to port 8080).
  **Leave "Connect to predefined network" off** for this resource: it would put Horae on the
  shared `coolify` network, next to every other resource. It is **the one misconfiguration
  that does not fail closed**: everything keeps working, and any container on that network can
  call port 8080 directly, without SSO. Check it after every change to the resource.
- **Coolify writes every variable of the resource into one `.env` and injects it into every
  service** (`env_file: .env` on each, `serviceParser`), whatever the template's
  `environment:` lists. So `horae-init` receives the secrets too (the API key, the push URLs).
  It is the same image and user, runs only `horae init` on an empty volume and exits, so this
  does not widen what a compromise of Horae could read; but do not add services to this
  resource that should not see Horae's secrets.
- `HORAE_ALLOWED_HOSTS` is the domain Coolify routes to port 8080 (`SERVICE_URL_HORAE_8080`,
  read back as `${SERVICE_FQDN_HORAE}`): any other `Host` gets `400 invalid_host`.
- `HORAE_SCHEDULER=external` by default: Coolify's Scheduled Tasks run the pipelines.

### Routing: only `/act/` is exempt from SSO

- Coolify generates the routers of the domain (`https` and `http`, `Host(...) &&
  PathPrefix(/)`) and **adds to them every middleware that the service defines with
  `traefik.http.middlewares.<name>.*` labels** (Coolify docs, *Custom Middlewares*; the source,
  `fqdnLabelsForTraefik`). The template defines one, `horae-sso`, a `forwardAuth` to
  `HORAE_SSO_FORWARD_AUTH_URL`. So everything on the domain (`/`, `/runs`, `/runs/<id>`,
  `/healthz`, `/act` without the slash) goes through SSO.
- One router of the template's own, `horae-act`, has no middleware: `Host(<domain>) &&
  PathPrefix(/act/)` on the `https` entry point, priority 1000 (above the generated routers',
  whose priority is the length of their rule). The `/act/` links (#38) are signed, single-use
  and only confirmed by a `POST`: they are designed to work without SSO (T14, T16).
- Traefik routes on the **cleaned** path (`sanitizePath`, on by default: dot segments and
  repeated slashes removed after decoding), so `/act/../runs`, `/act/%2e%2e/runs`,
  `/act/..%2Fruns` and `//act/../runs` are `/runs` for the router and go through SSO. Horae
  itself never cleans a path: such a request reaching it raw is a `404`, never `/runs`
  (`tests/test_api.py`, `test_traversals_out_of_act_are_404`).
- `trustForwardHeader=false` on `horae-sso`: the SSO provider is sent the method, host and URI
  that Traefik itself received, never `X-Forwarded-*` values from the client. With `true`, if
  Traefik's entry point ever trusted a client's forwarded headers (an upstream proxy or CDN
  listed in `forwardedHeaders.trustedIPs`, or `insecure`), a client could send a spoofed
  `X-Forwarded-Uri` (`/act/...` while requesting `/runs`) and have the provider decide on a
  path other than the one routed. `false` costs nothing here: Traefik is the edge.
- `horae-act` has `tls=true` and **no `certresolver`**: Traefik serves a certificate by host
  name (SNI), from any resolver, and the router Coolify generates for the same domain obtains
  it with the server's resolver, whatever that is named (`letsencrypt` by default, but it can
  be changed per server). Naming one here would be a second, possibly wrong, request for the
  same certificate. To confirm on a real Coolify: `/act/x` is served with the domain's
  certificate.
- `tests/test_deploy_templates.py` renders the template with Coolify's generated labels and
  routes requests through it (the threat-model criterion of #41); CI also runs `docker compose
  config` on it.

If something is misconfigured, the template **fails closed**: with label escaping on (below),
both label values that use a variable stay literal: the forwardAuth address is not a URL
(Traefik answers an error, never the API) and the `/act/` router's host never matches (so
`/act/` falls back to the SSO router). A missing middleware makes Traefik refuse the router.
The exception is "Connect to predefined network" (above): turned on, it fails **open** for
containers on the shared network.

### One Horae per Coolify server

The template's own Traefik names are fixed: the middleware `horae-sso`, the router and service
`horae-act` (Coolify's generated routers carry the resource's UUID and never clash). Coolify
substitutes no per-resource value into labels: `COOLIFY_RESOURCE_UUID` is added to the
containers' environment only, not to the `.env` that Compose interpolates labels with, and the
`SERVICE_NAME_*` variables are the compose service names, the same in every copy (Coolify
source, `serviceParser` and `Service::saveComposeConfigs`). So **deploy at most one Horae per
Coolify server.**

A second one fails closed, never open: the two `horae-act` routers differ (their hosts), and
Traefik drops a router defined differently by two containers, so `/act/` on both domains falls
back to the SSO routers; if the two SSO addresses differ, `horae-sso` is dropped too and every
router that uses it is refused (nothing is served). Either way nothing is reachable without
SSO (`tests/test_deploy_templates.py`, `test_coolify_a_second_horae_on_the_server_fails_closed`),
but `/act/` stops working. **Follow-up:** a per-copy suffix (an operator-set required variable,
or a Coolify magic value if one becomes available in labels) once a second instance is needed.

## Install

### 1. The SSO provider

Install authentik (or another provider with a Traefik `forwardAuth` endpoint) as its own
resource. For authentik: create a **Proxy Provider** in *Forward auth* mode for the Horae URL
(single application, or domain level for the whole domain), an Application using it, and add it
to the embedded outpost. The forwardAuth address Traefik calls is the outpost's endpoint on the
internal network, for example
`http://authentik-server:9000/outpost.goauthentik.io/auth/traefik`: use authentik's container
name as Coolify shows it (authentik's docs, *Traefik* forward auth; Coolify's guide
*Authentik Forward Authentication Middleware*). In single-application mode authentik also needs
`/outpost.goauthentik.io/` on the Horae host routed to authentik: add that router to the
authentik resource (it reaches authentik, not Horae), or use domain-level mode.

### 2. The resource

1. In the project: **+ New → Resource → Docker Compose Empty**, paste
   `deploy/coolify/docker-compose.yaml`, save.
2. **Edit Compose → turn off "Escape special characters in labels"** ("Turn this off to use
   environment variables inside labels", Coolify's help text). The template's labels use
   `${HORAE_SSO_FORWARD_AUTH_URL}` and `${SERVICE_FQDN_HORAE}`; escaped, they stay literal and
   nothing works (fail closed, above).
3. The `horae` service → **Domains**: one `https://` URL with **no path**, e.g.
   `https://horae.example.org` (one domain: `HORAE_ALLOWED_HOSTS` takes the first one only, and
   the `/act/` router names that host). Leave `horae-init` without a domain.
4. **Environment Variables**: Coolify lists the required ones (`${VAR:?}`) and **blocks the
   deployment until each is set**:

   | Variable | Value |
   |---|---|
   | `HORAE_SSO_FORWARD_AUTH_URL` | the forwardAuth address of step 1 (not a secret) |
   | `HORAE_ANTHROPIC_API_KEY` | the Anthropic API key (secret) |
   | `HORAE_MONITOR_DAILY_BRIEF_URL` | the Uptime Kuma push URL of `daily-brief` (secret) |
   | `HORAE_MONITOR_REMIND_URL` | the Uptime Kuma push URL of `remind` (secret) |

   Optional: `HORAE_SCHEDULER` (`external`, the default here, or `builtin`) and
   `HORAE_IMAGE_TAG` (see [Updates](#updates)). Every variable, these included, is injected
   into both services (above). The template writes required variables as a
   bare `${VAR:?}`: Coolify stores any text after `:?` as the variable's value, which would
   unblock the deployment with that text as the secret.
   Using another backend (`ollama` with a `base_url`, no key): delete the
   `HORAE_ANTHROPIC_API_KEY` line from the compose, rather than setting a dummy value.
5. Keep **Connect to predefined network** off. **Deploy.** Both containers start; `horae` turns
   healthy (Coolify shows *Running (healthy)*); `horae-init` shows *Exited*.

Check from a browser: `https://horae.example.org/healthz` asks you to sign in, then answers
`{"status": "ok", ...}`; `https://horae.example.org/act/x` answers without sign-in (a `404`
until #38 ships `/act/`).

### 3. The instance (`horae.yaml`)

The first deployment holds the example instance. Put your own configuration (from your instance
repository: `horae.yaml`, `pipelines/`, and the `horae.lock` of your plugins) into the volume,
then restart the resource. The image has no shell tools beyond `sh`; from the Coolify server:

```sh
docker ps --filter name=horae-     # the container is horae-<resource uuid>
docker cp horae.yaml horae-<uuid>:/instance/horae.yaml
docker cp pipelines/. horae-<uuid>:/instance/pipelines/
docker exec horae-<uuid> horae doctor     # must pass before you restart
```

(`docker cp` writes the files as root with their source permissions: keep them world-readable,
`0644`, which is fine since they hold no secret; Horae only reads its configuration.) The secrets are environment variables, so the instance names them with
explicit references ([decision 25](../../schemas/README.md#decisions); without one, a secret is
read from `/run/secrets/<NAME>`):

```yaml
secrets:
  refs:
    ANTHROPIC_API_KEY: {env: HORAE_ANTHROPIC_API_KEY}
    MONITOR_DAILY_BRIEF: {env: HORAE_MONITOR_DAILY_BRIEF_URL}
    MONITOR_REMIND: {env: HORAE_MONITOR_REMIND_URL}
models:
  main: {backend: anthropic, model: <a Claude model id>, secret: ANTHROPIC_API_KEY}
scheduler:
  mode: external          # HORAE_SCHEDULER=external overrides it anyway
  monitors:               # only for pipelines that exist (an unknown one is a loader error)
    daily-brief: {secret: MONITOR_DAILY_BRIEF}
    remind: {secret: MONITOR_REMIND}
```

Run a one-off command in the running container with Coolify's **Terminal** (container `horae`)
or `docker exec horae-<uuid> horae <command>`: `horae doctor`, `horae runs`, `horae run
<pipeline> --dry-run`. To start again from an empty instance, delete the volume (Coolify:
*Persistent Storage*) and redeploy: `horae-init` creates a new one.

### 4. Scheduled Tasks

With `HORAE_SCHEDULER=external`, `horae serve` fires nothing and Coolify runs the pipelines. In
the resource: **Scheduled Tasks → + Add**, one per pipeline:

| Name | Command | Frequency | Container | Timeout |
|---|---|---|---|---|
| `daily-brief` | `horae run daily-brief` | `0 6,18 * * *` (twice a day) | `horae` | `300` (default) or more |
| `remind` | `horae run remind` | `*/5 * * * *` | `horae` | `300` (default) |

- **Set the container to `horae`** (recommended). Coolify picks among the resource's
  **running** containers: `horae-init` has exited, so today `horae` is the only candidate and a
  task without a container name runs there too; but if a second container is ever running
  (another service added, or `horae-init` caught during a redeploy) a task without a name fails
  with "More than one container exists but no container name was provided". Coolify runs
  `sh -c '<command>'` with `docker exec` in that container (as uid 10001, with the container's
  environment, so the secrets resolve).
- **Timeout: 300 s by default** (the task's *Timeout* field). Past it Coolify stops waiting:
  it kills its own SSH command and records the task as **failed**, but the `horae run` inside
  the container is not necessarily stopped and may finish (and ping its monitor) later. While
  it runs it holds the pipeline's lock, so the next occurrence exits `5` (locked) instead of
  running twice; Coolify also drops an occurrence of the same task while its previous one is
  still being run (its overlap lock). Give a pipeline that can take longer (several LLM calls) a timeout
  above its longest run, and keep it below the interval between two runs.
- The frequency is a cron expression in the **server's time zone** (Coolify server settings),
  not the instance's `timezone`.
- A task's exit code is Horae's ([README](../../README.md#getting-started), decision 37): `0`
  ok, `1` failed, `5` locked (the previous run of the pipeline still holds its lock; it is not
  run twice). Coolify shows failed tasks; the push monitors are the alarm.
- Missed runs (the server was down) are not caught up, by Coolify or by Horae.

`HORAE_SCHEDULER=builtin` instead uses the pipelines' own `trigger: {cron: ...}` inside
`horae serve`, without Scheduled Tasks; choose one, not both, or each run happens twice (the
second is skipped as `locked` only if the first is still running).

### 5. Push monitors (Uptime Kuma)

Horae pings each pipeline's push URL after every run, scheduled or manual (`up` for `ok`,
`noop` and `degraded`, `down` otherwise; [decision 49](../../schemas/README.md#decisions) F).
A run that never happens pings nothing: Uptime Kuma notices the missing heartbeat.

1. In Uptime Kuma: **Add New Monitor → Push**, one per pipeline. Heartbeat interval a little
   longer than the schedule: `remind` every 5 min → 600 s; `daily-brief` twice a day → about
   13 h (46800 s). Add your ntfy (or other) notification.
2. Copy each **push URL** (it carries the monitor's token: it is a secret) into
   `HORAE_MONITOR_DAILY_BRIEF_URL` / `HORAE_MONITOR_REMIND_URL`, redeploy. Horae adds the
   `status`, `msg` and `ping` parameters itself; the default query string Uptime Kuma shows may
   stay or go.
3. The URL must be `https://` (any host) or `http://` to a LAN or single-label host, e.g.
   Uptime Kuma's `https://` domain. `horae doctor` checks it without printing it.

`/healthz` is behind SSO: Uptime Kuma cannot poll it from outside without a session. Do not add
an SSO-free route for it (its body lists the pipelines and their schedules). Rely on the push
monitors for the runs, and on Coolify's health status (the image's `HEALTHCHECK`) for the
process.

## Updates

The image is pinned. The template writes it **once**, in `x-horae-image` at its top:
`ghcr.io/horae-dev/horae:${HORAE_IMAGE_TAG:-<default>}`, the default being a `sha-<short>`
tag of `main` (which never moves), bumped as a release step
([container-image.md](container-image.md#bumping-the-deploy-templates)). Coolify stores that
default as the value of `HORAE_IMAGE_TAG` when it first reads the template, so a deployed
resource keeps its tag until you change the variable, even if you paste a newer template.

To update: verify the new image's signature
([container-image.md](container-image.md#verify-before-you-run)), set `HORAE_IMAGE_TAG` to its
tag, **redeploy**. **In production, pin the digest as well** (a tag can be re-pointed; a digest
cannot): `HORAE_IMAGE_TAG=<tag>@sha256:<digest>`, the digest you verified; Docker then pulls
exactly that digest. Release tags (`X.Y.Z`) replace `sha-*` once there are releases; there is
no `latest`. Read the release notes for built-in plugin changes
([container-image.md](container-image.md#stable-paths-and-upgrading)).

### Registry credentials (private image)

While the GHCR package is not public (the check at the top answers `401`), log the Coolify
server's Docker in to GHCR; Coolify deploys with the Docker of that server, as the user it
connects with (root by default), and uses its stored login for the pull:

1. On GitHub, create a token that can only read packages: a classic personal access token
   with the single scope `read:packages` (an account that can read the `horae-dev` package),
   with an expiry.
2. On the Coolify server, as that user, without putting the token in the shell history:

   ```sh
   read -rs GHCR_TOKEN && printf '%s' "$GHCR_TOKEN" \
     | docker login ghcr.io -u <github user> --password-stdin; unset GHCR_TOKEN
   ```

3. Redeploy. The login is stored in `~/.docker/config.json` of that user, unencrypted unless a
   credential helper is set up: anyone root on the server can read it. Renew it before the
   token expires (the pull of the next deployment fails, the running container keeps going).

## Backups

Coolify's scheduled backups cover its databases, not this volume. Back up the volume
(`/var/lib/docker/volumes/<uuid>_horae-instance/_data` on the server: configuration, lock,
store, vault) with the server's own backup tool, **with an encrypted offsite copy**, and follow
[retention-and-backups.md](retention-and-backups.md) for the SQLite store (online backup API, or
a copy with the container stopped; never a plain file copy while it runs).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Deploy button blocked, variables highlighted | a required variable is empty: set it |
| Every request: `400 {"kind": "invalid_host", ...}` | the request's `Host` is not `HORAE_ALLOWED_HOSTS`: the domain changed without a redeploy, or the domain has several entries (only the first is allowed). The log names each refused host once |
| Every request fails (`500`) or is never redirected to sign-in | label escaping still on (the forwardAuth address is literal `${...}`), or the SSO outpost is unreachable from Traefik at `HORAE_SSO_FORWARD_AUTH_URL` |
| `/act/...` asks for sign-in | label escaping on (the `/act/` router's host is literal), or the domain is not the URL of step 2.3 |
| `horae` never starts, `horae-init` exited 3 | the volume is not empty but holds no `horae.yaml`: `horae init` refuses it |
| `horae` restarts, log says a configuration error | `horae.yaml` is invalid (`docker exec ... horae doctor`), or `HORAE_SCHEDULER` is not `builtin`/`external` |
| Unhealthy, `/healthz` `503` | the store cannot be read, or the built-in scheduler stopped (see the log) |
| Deployment fails at the pull: *denied* / *unauthorized* | the GHCR package is not public: [registry credentials](#registry-credentials-private-image) |
| `/act/...` asks for sign-in, or nothing on the domain answers, after adding a second Horae | two Horae on one server clash on the Traefik names: [one per server](#one-horae-per-coolify-server) |
| Scheduled task: "More than one container exists" | set the task's container to `horae` |
| Scheduled task failed after 300 s, the run finished later | the task's timeout is shorter than the run: raise it (Scheduled Tasks) |
| Monitor down although the run worked | the ping failed (Horae logs a warning, without the URL): check the URL with `horae doctor` |

## What was verified, and what was not

Verified without Coolify: the rendered template's routing and settings
(`tests/test_deploy_templates.py`), `docker compose config` (CI job `deploy`), and the template
run by plain Docker Compose against an image built from the tree (healthy; `horae run` through
`docker exec`). **Not verified here**, to check on a real Coolify: the deployment and its health
status, the blocking of required variables in the UI, the generated routers carrying
`horae-sso` (Coolify's label generation as of its source on 2026-10-29), the escape toggle,
Scheduled Tasks in a two-container stack (and their timeout), the `horae-act` router served
with the domain's certificate without a `certresolver`, `sanitizePath` on Coolify's Traefik
entry points, the YAML anchor (`x-horae-image`) read by Coolify's parser, the two-instance
clash failing closed, and the authentik forward auth end to end.

## Sources

- Coolify docs: [Docker Compose](https://coolify.io/docs/knowledge-base/docker/compose)
  (magic variables `SERVICE_URL_<NAME>_<PORT>` / `SERVICE_FQDN_<NAME>`, `${VAR:?}` blocks the
  deployment, resource network, `restart: "no"` one-offs),
  [Adding a service template](https://coolify.io/docs/get-started/contribute/service) (header
  comments, pinned images, no `container_name`),
  [Custom middlewares](https://coolify.io/docs/knowledge-base/proxy/traefik/custom-middlewares)
  (label-defined middlewares are added to every generated router; custom routers and priority),
  [Authentik forward auth](https://coolify.io/docs/knowledge-base/proxy/traefik/protect-services-with-authentik).
- Coolify source (`coollabsio/coolify`, `main` at 2026-10-29): `bootstrap/helpers/docker.php`
  (`fqdnLabelsForTraefik`: router names, rules, middlewares from labels),
  `bootstrap/helpers/parsers.php` (`serviceParser`: the text after `:?` becomes the stored value;
  label escaping; the resource network; `env_file: .env` on every service; `COOLIFY_RESOURCE_UUID`
  in the containers' environment only), `app/Models/Service.php` (`saveComposeConfigs`: what
  the `.env` holds), `app/Jobs/ScheduledTaskJob.php` (`docker exec ... sh -c` in a running
  container, a name required when several are running, the 300 s default timeout), the *Escape
  special characters in labels* toggle (`edit-compose.blade.php`).
- Templates in Coolify's catalogue (`templates/compose/uptime-kuma.yaml`, `ntfy.yaml`) for the
  current conventions.
- authentik docs: [Traefik forward auth](https://docs.goauthentik.io/add-secure-apps/providers/proxy/server_traefik/).
