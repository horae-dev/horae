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

The reference deployment is one Coolify server (Coolify 4.6, its Traefik v3 proxy) with these
resources, all reached from the Internet through **one Cloudflare Tunnel**:

| Resource | What | SSO |
|---|---|---|
| `horae` | this template: `horae serve` (the read-only API; with Scheduled Tasks, no built-in scheduler) | yes, except `/act/` |
| `cloudflared` | the Cloudflare Tunnel connector: every public host name of the tunnel points at Traefik's `http` entry point | (transport) |
| authentik | the SSO provider and its embedded outpost; Traefik's file configuration defines the shared forwardAuth middleware and the outpost service | it *is* the SSO |
| `vault-sync` | the Coolify one-click "Obsidian LiveSync" service (CouchDB), E2EE and path obfuscation on, its own host name | **no** (the plugin cannot do SSO) |
| ntfy | self-hosted ntfy with an iOS upstream | ntfy's own auth |
| Uptime Kuma | the push monitors of the scheduled runs | Uptime Kuma's own auth |

Only `horae` is in this template; the others are separate resources.

## The layout: tunnel, file-provider SSO, hand-written routers

```text
browser ──https──▶ Cloudflare (TLS ends here) ──tunnel──▶ cloudflared
   ──http──▶ Traefik, entry point `http` (:80)
               ├─ <name>          Host(h)                                  → authentik@file → Horae :8080
               ├─ <name>-act      Host(h) && PathPrefix(/act/)             → Horae :8080 (no SSO)
               └─ <name>-outpost  Host(h) && PathPrefix(/outpost.goauthentik.io/) → sso@file (authentik)
```

- **TLS ends at Cloudflare.** `cloudflared` reaches Traefik over plain HTTP on its `http` entry
  point, so the routers are on `http` with `tls=false` (`HORAE_TRAEFIK_ENTRYPOINT`,
  `HORAE_TRAEFIK_TLS`). Port 443 of the server is not on that path.
- **Coolify generates no router for Horae.** The service has **no domain**: the routers are the
  template's own labels (`traefik.enable=true`, `traefik.docker.network`, three routers and one
  service), the way the other apps of the reference server are deployed.
- **SSO is shared, defined once in Traefik's dynamic file configuration**, and the template only
  names it. The prerequisite on the server (Coolify: *Servers → Proxy → Dynamic
  Configurations*), with your own container name and headers:

  ```yaml
  http:
    middlewares:
      authentik:
        forwardAuth:
          address: http://authentik-server:9000/outpost.goauthentik.io/auth/traefik
          trustForwardHeader: true
          authResponseHeaders:
            - X-authentik-username
            - X-authentik-groups
            - X-authentik-email
    services:
      sso:
        loadBalancer:
          servers:
            - url: http://authentik-server:9000/outpost.goauthentik.io
  ```

  The routers refer to them as `authentik@file` (`HORAE_SSO_MIDDLEWARE`) and `sso@file`
  (`HORAE_SSO_OUTPOST_SERVICE`). `HORAE_SSO_MIDDLEWARE` is never empty: an empty value falls
  back to the default (`:-`), and a name the file provider does not define makes Traefik
  refuse the main router (nothing but `/act/` and the outpost path is served: fails closed).
- **The outpost router** sends `/outpost.goauthentik.io/` on Horae's host to authentik, never
  to Horae: authentik's sign-in start and callback in single-application forward auth mode.

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
- **No published port.** `horae` joins its resource's own network and the external network
  Traefik reaches it on (`HORAE_TRAEFIK_NETWORK`, default `traefik-network`, also named in
  `traefik.docker.network`); `horae-init` joins only the resource's network.
- **Coolify writes every variable of the resource into one `.env` and injects it into every
  service** (`env_file: .env` on each, `serviceParser`), whatever the template's
  `environment:` lists. So `horae-init` receives the secrets too (the API key, the push URLs).
  It is the same image and user, runs only `horae init` on an empty volume and exits, so this
  does not widen what a compromise of Horae could read; but do not add services to this
  resource that should not see Horae's secrets.
- `HORAE_ALLOWED_HOSTS` is `HORAE_PUBLIC_HOST`, the public host name users reach through the
  tunnel (required; one name, the routers match it): any other `Host` gets `400 invalid_host`.
- `HORAE_SCHEDULER=external` by default: Coolify's Scheduled Tasks run the pipelines.

### The proxy network

Any container on a network with Horae can call port 8080 directly, **without SSO**, and send any
`Host` (the API trusts its network: [decision 51](../../schemas/README.md#decisions) B; the
Host check stops browsers, not neighbours). The reference server's `traefik-network` (the
default) is shared by Traefik **and every app routed by labels** (authentik, ntfy, Uptime Kuma,
...): on it, each of those containers can read `/runs` and `/healthz` without signing in. That
is the reference layout's accepted trade-off, recorded in [T15](../security.md); a compromise
of one of those apps reaches Horae's read-only API.

**Recommended:** a network shared only by Horae and the proxy. Create it on the server, add it
to the proxy's own compose file in Coolify (*Servers → Proxy → Configuration*, under the
`coolify-proxy` service's `networks:` and the top-level `networks:` as `external: true`, so
that it survives a proxy restart), restart the proxy, then set `HORAE_TRAEFIK_NETWORK` to it:

```sh
docker network create horae-proxy
```

Keep **"Connect to predefined network"** off for this resource: it would also put Horae on
the shared `coolify` network, next to every other resource. Both settings fail **open** for
neighbours (everything keeps working, and the neighbours reach port 8080 without SSO): check
them after every change.

### Routing: only `/act/` is exempt from SSO

- `<name>` (`HORAE_TRAEFIK_NAME`, default `horae`): `Host(<public host>)`, the whole host
  (`/`, `/runs`, `/runs/<id>`, `/healthz`, `/act` without the slash), through the shared SSO
  middleware; priority 900.
- `<name>-act`: `Host(<public host>) && PathPrefix(/act/)`, **no middleware**, priority 1000.
  The `/act/` links (#38) are signed, single-use and only confirmed by a `POST`: they are
  designed to work without SSO (T14, T16).
- `<name>-outpost`: `Host(<public host>) && PathPrefix(/outpost.goauthentik.io/)` to the
  outpost service (`sso@file`), priority 1000. It never reaches Horae.
- The priorities are explicit: if a domain is ever set on the service by mistake, the routers
  Coolify then generates (`Host() && PathPrefix(/)`, priority = the rule's length, well under
  900) lose to these on the public host. On **any other host** they would serve Horae without
  SSO: **leave the Domains empty** (the other fail-open setting).
- Traefik routes on the **cleaned** path (`sanitizePath`, on by default, not overridden on the
  reference server: dot segments and repeated slashes removed after decoding), so
  `/act/../runs`, `/act/%2e%2e/runs`, `/act/..%2Fruns`, `//act/../runs` and
  `/outpost.goauthentik.io/../runs` are `/runs` for the router and go through SSO. Horae itself
  never cleans a path: such a request reaching it raw is a `404`, never `/runs`
  (`tests/test_api.py`, `test_traversals_out_of_act_are_404`).
- **Forwarded headers.** The shared middleware has `trustForwardHeader: true`: it passes
  `X-Forwarded-*` (`-Uri`, `-Host`, `-Method`, ...) of the request on to authentik, which
  decides on them. That is safe on the reference server because **no entry point trusts its
  peer's forwarded headers** (no `forwardedHeaders.trustedIPs`, no `insecure`): `cloudflared`
  is an untrusted peer, so Traefik's entry point **removes** every `X-Forwarded-*` header it
  receives and sets its own from the request it actually got, before forwardAuth runs.
  authentik then sees the routed URI. **It stops being safe as soon as an entry point that
  serves Horae trusts `cloudflared`** (`forwardedHeaders.trustedIPs` including `cloudflared`'s
  address or its Docker network, or `forwardedHeaders.insecure: true`): Cloudflare and
  `cloudflared` pass a client's own `X-Forwarded-Uri` / `X-Forwarded-Host` through, so a
  client could request `/runs` with `X-Forwarded-Uri: /act/x` (or another app's host) and have
  authentik decide on that instead (an unauthenticated-path rule, or another app's policy,
  would then let it through). If you ever need trusted forwarded headers (the client IP in
  `X-Forwarded-For`), set `trustForwardHeader: false` on the middleware Horae uses (a second,
  Horae-only file middleware is fine). Our own guidance is unchanged: a forwardAuth that the
  template defines by label (the [alternative](#alternative-a-coolify-domain-and-a-label-defined-forwardauth),
  [compose.md](compose.md)) has `trustForwardHeader=false`.
- `tests/test_deploy_templates.py` renders the template, routes requests through its routers
  with the file provider's middleware and service, and checks the criterion of #41; CI also
  runs `docker compose config` on it.

If something is misconfigured, the template **fails closed**: with label escaping on (below),
the labels' variables stay literal (`Host(${HORAE_PUBLIC_HOST})` never matches, the entry point
`${...}` does not exist): nothing is served. A middleware name the file provider does not
define makes Traefik refuse the main router. The exceptions are the network settings (above)
and a domain set on the service: they fail **open**.

### Several Horae on one server

Every Traefik name of the template carries `HORAE_TRAEFIK_NAME` (`<name>`, `<name>-act`,
`<name>-outpost`, the service `<name>`), so give each copy its own name and public host. Two
copies with the **same** name clash: the routers differ (their hosts) and Traefik drops a
router that two containers define differently, so neither host is served (fails closed,
`test_coolify_two_horae_with_the_same_name_fail_closed`).

### Alternative: a Coolify domain and a label-defined forwardAuth

Without a tunnel (Traefik is the edge, with Coolify's certificates), the earlier variant of this
template still works and is not shipped as a file: give the `horae` service a domain
(`SERVICE_URL_HORAE_8080` in its environment, `HORAE_ALLOWED_HOSTS=${SERVICE_FQDN_HORAE}`),
remove the `networks:` and the three routers above, and add a forwardAuth middleware by label
(`traefik.http.middlewares.<name>-sso.forwardauth.address=...`,
`...trustForwardHeader=false`), which Coolify adds to every router it generates for the domain,
plus one `<name>-act` router (`Host(${SERVICE_FQDN_HORAE}) && PathPrefix(/act/)`, entry point
`https`, `tls=true`, priority 1000). The routing tests of this file cover the tunnel layout
only; [compose.md](compose.md)'s `traefik.yaml` is the tested label-defined forwardAuth.

## Install

### 1. Prerequisites on the server

1. **authentik**: a **Proxy Provider** in *Forward auth (single application)* mode for
   `https://<public host>`, an Application using it, added to the embedded outpost (authentik's
   docs, *Traefik* forward auth). Domain-level mode works too.
2. **The shared middleware and outpost service** in Traefik's dynamic file configuration (the
   YAML [above](#the-layout-tunnel-file-provider-sso-hand-written-routers)), the address being
   authentik's container on a network Traefik shares with it. Already there if other apps use
   them.
3. **The Cloudflare Tunnel public host name.** In Cloudflare Zero Trust: *Networks → Tunnels →
   your tunnel → Public Hostname → Add*: the subdomain and domain of `HORAE_PUBLIC_HOST`
   (`horae.example.org`), *Service* **`HTTP`** and **URL `coolify-proxy:80`** (Traefik's `http`
   entry point, reached by its container name on a network `cloudflared` shares with the
   proxy; or the server's address and port 80 if `cloudflared` runs on the host network). Not
   `HTTPS` / `:443`: the routers are on `http`. Leave *HTTP Host Header* empty, so Traefik sees
   the public host name. Cloudflare creates the DNS record.
4. Optional, recommended: [a network for Horae and the proxy only](#the-proxy-network).

### 2. The resource

1. In the project: **+ New → Resource → Docker Compose Empty**, paste
   `deploy/coolify/docker-compose.yaml`, save.
2. **Edit Compose → turn off "Escape special characters in labels"** ("Turn this off to use
   environment variables inside labels", Coolify's help text). Escaped, the labels' variables
   stay literal and nothing is served (fails closed).
3. **Leave the Domains of both services empty.** A domain makes Coolify generate routers
   without SSO ([above](#routing-only-act-is-exempt-from-sso)).
4. **Environment Variables**: Coolify lists the required ones (`${VAR:?}`) and **blocks the
   deployment until each is set**:

   | Variable | Value |
   |---|---|
   | `HORAE_PUBLIC_HOST` | the tunnel's public host name, e.g. `horae.example.org` (one name; it is also `HORAE_ALLOWED_HOSTS`) |
   | `HORAE_ANTHROPIC_API_KEY` | the Anthropic API key (secret) |
   | `HORAE_MONITOR_DAILY_BRIEF_URL` | the Uptime Kuma push URL of `daily-brief` (secret) |
   | `HORAE_MONITOR_REMIND_URL` | the Uptime Kuma push URL of `remind` (secret) |

   Optional, with their defaults (the reference server's names):

   | Variable | Default | |
   |---|---|---|
   | `HORAE_TRAEFIK_NAME` | `horae` | prefix of the router and service names; unique per Horae on the server |
   | `HORAE_TRAEFIK_ENTRYPOINT` | `http` | the entry point `cloudflared` reaches |
   | `HORAE_TRAEFIK_TLS` | `false` | Cloudflare terminates TLS |
   | `HORAE_TRAEFIK_NETWORK` | `traefik-network` | the network Traefik reaches Horae on ([prefer your own](#the-proxy-network)) |
   | `HORAE_SSO_MIDDLEWARE` | `authentik@file` | the shared forwardAuth middleware; empty means the default |
   | `HORAE_SSO_OUTPOST_SERVICE` | `sso@file` | the authentik outpost service |
   | `HORAE_SCHEDULER` | `external` | or `builtin` |
   | `HORAE_IMAGE_TAG` | the template's | see [Updates](#updates) |

   Every variable, these included, is injected into both services (above). The template writes
   required variables as a bare `${VAR:?}`: Coolify stores any text after `:?` as the
   variable's value, which would unblock the deployment with that text as the secret.
   Using another backend (`ollama` with a `base_url`, no key): delete the
   `HORAE_ANTHROPIC_API_KEY` line from the compose, rather than setting a dummy value.
5. Keep **Connect to predefined network** off. **Deploy.** Both containers start; `horae` turns
   healthy (Coolify shows *Running (healthy)*); `horae-init` shows *Exited*.

### Verify on the server

From a browser and the server, after the first deployment (and after any change to the proxy,
the tunnel or the resource):

- `https://horae.example.org/healthz` redirects to authentik's sign-in, then answers
  `{"status": "ok", ...}` and returns to an `https://` URL (not `http://`).
- `https://horae.example.org/act/x` answers **without** sign-in (a `404` until #38 ships `/act/`).
- `https://horae.example.org/act/../runs` (send it with `curl --path-as-is`) asks for sign-in.
- A request for `/runs` with `-H 'X-Forwarded-Uri: /act/x'` still asks for sign-in (the entry
  point drops the client's forwarded headers).
- `docker inspect` of the `horae` container: the labels above, `traefik.enable=true`, **no**
  `http-0-...` / `https-0-...` routers (no Coolify domain), the networks (the resource's and
  `HORAE_TRAEFIK_NETWORK`, not `coolify`).
- Traefik's dashboard or API: the three routers `<name>@docker`, `<name>-act@docker`,
  `<name>-outpost@docker` on `http`, with no error; `<name>` uses `authentik@file`.
- Traefik's static configuration still has no `forwardedHeaders.trustedIPs` / `insecure` on the
  `http` entry point, and `sanitizePath` is not set to `false`.
- From another container on `HORAE_TRAEFIK_NETWORK`: `curl http://<horae container>:8080/healthz`
  answers (the neighbour exposure described [above](#the-proxy-network)); with a dedicated
  network, only the proxy is on it.

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
| Every request: `400 {"kind": "invalid_host", ...}` | the request's `Host` is not `HORAE_PUBLIC_HOST`: the tunnel's public host name differs, or the tunnel rewrites the *HTTP Host Header*. The log names each refused host once |
| Cloudflare `404` / Traefik `404` for the host | label escaping still on (no router matches), the tunnel's service is not Traefik's `http` entry point (`HTTPS` / `:443` instead), `HORAE_TRAEFIK_ENTRYPOINT` differs from the entry point it reaches, or Traefik is not on `HORAE_TRAEFIK_NETWORK` |
| `/act/...` works, everything else `404` | Traefik refused the main router: `HORAE_SSO_MIDDLEWARE` names a middleware its file configuration does not define (Traefik's log says so) |
| Every request fails (`500`) or is never redirected to sign-in | authentik is unreachable from Traefik at the shared middleware's address |
| Sign-in loops, or the callback is `404` | the outpost router is missing or `HORAE_SSO_OUTPOST_SERVICE` is wrong: `/outpost.goauthentik.io/` must reach authentik |
| `horae` never starts, `horae-init` exited 3 | the volume is not empty but holds no `horae.yaml`: `horae init` refuses it |
| `horae` restarts, log says a configuration error | `horae.yaml` is invalid (`docker exec ... horae doctor`), or `HORAE_SCHEDULER` is not `builtin`/`external` |
| Unhealthy, `/healthz` `503` | the store cannot be read, or the built-in scheduler stopped (see the log) |
| Deployment fails at the pull: *denied* / *unauthorized* | the GHCR package is not public: [registry credentials](#registry-credentials-private-image) |
| Nothing on either host answers after adding a second Horae | both use the same `HORAE_TRAEFIK_NAME`: [give each its own](#several-horae-on-one-server) |
| Scheduled task: "More than one container exists" | set the task's container to `horae` |
| Scheduled task failed after 300 s, the run finished later | the task's timeout is shorter than the run: raise it (Scheduled Tasks) |
| Monitor down although the run worked | the ping failed (Horae logs a warning, without the URL): check the URL with `horae doctor` |

## What was verified, and what was not

The layout is the reference server's, as read (read-only) from it: Coolify 4.6.0, Traefik v3.7
(`coolify-proxy`) with the `http` (:80) and `https` (:443) entry points, the default
`sanitizePath`, no `forwardedHeaders.trustedIPs` / `insecure`; `cloudflared` reaching the `http`
entry point; apps routed by hand-written labels (`traefik.enable=true`,
`traefik.docker.network=traefik-network`, routers on `http` with `tls=false`, the main one with
`authentik@file`, an outpost router to `sso@file`); authentik, ntfy and Uptime Kuma as other
resources.

Verified without that server: the rendered template's routing and settings with that file
configuration (`tests/test_deploy_templates.py`), `docker compose config` (CI job `deploy`),
and the template's services run by plain Docker Compose against an image built from the tree
(healthy; `horae run` through `docker exec`). **Not verified yet**, for the maintainer on the
real server ([the checklist](#verify-on-the-server)): the deployment and its health status,
the blocking of required variables in the UI, the escape toggle, that Coolify adds no router of
its own and keeps `traefik.enable=true` for a service without a domain, the external network
joined next to the resource's own, the authentik forward auth and its callback through the
outpost router end to end (and the redirect back to `https://`), a spoofed `X-Forwarded-Uri`
through the tunnel, Scheduled Tasks in a two-container stack (and their timeout), and the YAML
anchor (`x-horae-image`) read by Coolify's parser.

## Sources

- Coolify docs: [Docker Compose](https://coolify.io/docs/knowledge-base/docker/compose)
  (magic variables `SERVICE_URL_<NAME>_<PORT>` / `SERVICE_FQDN_<NAME>` of the alternative,
  `${VAR:?}` blocks the deployment, resource network, `restart: "no"` one-offs),
  [Adding a service template](https://coolify.io/docs/get-started/contribute/service) (header
  comments, pinned images, no `container_name`),
  [Custom middlewares](https://coolify.io/docs/knowledge-base/proxy/traefik/custom-middlewares)
  (label-defined middlewares are added to every generated router; custom routers and priority),
  [Authentik forward auth](https://coolify.io/docs/knowledge-base/proxy/traefik/protect-services-with-authentik),
  [Cloudflare Tunnels](https://coolify.io/docs/knowledge-base/cloudflare/tunnels/overview).
- Traefik docs: [EntryPoints](https://doc.traefik.io/traefik/routing/entrypoints/)
  (`forwardedHeaders.trustedIPs` / `insecure`, `http.sanitizePath`),
  [ForwardAuth](https://doc.traefik.io/traefik/middlewares/http/forwardauth/)
  (`trustForwardHeader`), [providers and `@file` names](https://doc.traefik.io/traefik/providers/overview/),
  [router priority](https://doc.traefik.io/traefik/routing/routers/#priority).
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
