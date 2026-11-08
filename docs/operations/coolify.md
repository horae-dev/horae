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
resources, all reached from the Internet through **one Cloudflare Tunnel** (the template's
opt-in tunnel mode; its default is Traefik's `https` entry point):

| Resource | What | SSO |
|---|---|---|
| `horae` | this template: `horae serve` (the read-only API; with Scheduled Tasks, no built-in scheduler) | yes, except `/act/` |
| `cloudflared` | the Cloudflare Tunnel connector: every public host name of the tunnel points at Traefik's `http` entry point | (transport) |
| authentik | the SSO provider and its embedded outpost; Traefik's file configuration defines the shared forwardAuth middleware and the outpost service | it *is* the SSO |
| `vault-sync` | the Coolify one-click "Obsidian LiveSync" service (CouchDB), E2EE and path obfuscation on, its own host name | **no** (the plugin cannot do SSO) |
| ntfy | self-hosted ntfy with an iOS upstream | ntfy's own auth |
| Uptime Kuma | the push monitors of the scheduled runs | Uptime Kuma's own auth |

Only `horae` is in this template; the others are separate resources.

## The layout: hand-written routers, file-provider SSO, the resource's own network

```text
default:  browser ──https──▶ Traefik, entry point `https` (:443, TLS)
tunnel:   browser ──https──▶ Cloudflare (TLS ends) ──tunnel──▶ cloudflared ──http──▶ Traefik `http` (:80)

  Traefik ──(the resource's own network: the proxy and this resource only)──▶
     ├─ <name>          Host(h)                                           → SSO middleware → Horae :8080
     ├─ <name>-act      Host(h) && PathPrefix(/act/)                      → Horae :8080 (no SSO)
     └─ <name>-outpost  Host(h) && PathPrefix(/outpost.goauthentik.io/)   → sso@file (authentik)
```

- **Coolify generates no router for Horae.** The service has **no domain**: the routers are the
  template's own labels (`traefik.enable=true`, three routers and one service), the way the
  other apps of the reference server are deployed (but not on their shared network:
  [The network](#the-network)).
- **Two transport modes.**
  - **Default, secure:** the routers are on Traefik's `https` entry point with `tls=true`
    (`HORAE_TRAEFIK_ENTRYPOINT=https`, `HORAE_TRAEFIK_TLS=true`). The certificate for
    `HORAE_PUBLIC_HOST` must come from the proxy: Coolify obtains certificates only for the
    domains it manages, and this service has none. The common case on Coolify: the proxy has a
    `letsencrypt` certificate resolver (Coolify's default proxy configuration); set
    `HORAE_TRAEFIK_CERTRESOLVER=letsencrypt` and every router of the template asks it for the
    certificate (`tls.certresolver`; DNS for the host must point at the server and port 80 or
    443 be reachable for the challenge, depending on the resolver). Otherwise: an
    entry-point-level resolver, a certificate in the file configuration, or a Cloudflare
    origin certificate (with Cloudflare proxying in *Full (strict)* mode).
    `HORAE_TRAEFIK_CERTRESOLVER` stays empty in the tunnel mode.
  - **Tunnel, opt-in (the reference server):** a Cloudflare Tunnel delivers the traffic, TLS
    ends at Cloudflare, and `cloudflared` reaches Traefik over plain HTTP. Set
    `HORAE_TRAEFIK_ENTRYPOINT=http` and `HORAE_TRAEFIK_TLS=false`, **only while port 80 is not
    reachable from the Internet**. Close it in the server's or the provider's firewall, so that
    only `cloudflared` reaches the `http` entry point; check from outside (`curl -m 5
    http://<server public address>/` must time out). Docker's published ports bypass `ufw`, so
    use the provider's firewall or the `DOCKER-USER` chain. Turn on Cloudflare's *Always Use
    HTTPS* for the zone, so that a client's `http://` request is redirected before the tunnel.
    Any router that serves the public on plain `http` (no tunnel in front) needs a
    redirect-to-https middleware instead of being served.
- **SSO is defined in Traefik's dynamic file configuration**, and the template only names it
  (`HORAE_SSO_MIDDLEWARE`, `HORAE_SSO_OUTPOST_SERVICE`). On Coolify the files live in
  `/data/coolify/proxy/dynamic/` (UI: *Servers → Proxy → Dynamic Configurations*; Coolify docs,
  *Traefik dynamic configuration*). **Preferred: a middleware for Horae alone, with
  `trustForwardHeader: false`**, for example `/data/coolify/proxy/dynamic/horae.yaml`
  (your own authentik container name and headers):

  ```yaml
  http:
    middlewares:
      horae-authentik:
        forwardAuth:
          address: http://authentik-server:9000/outpost.goauthentik.io/auth/traefik
          trustForwardHeader: false
          authResponseHeaders:
            - X-authentik-username
            - X-authentik-groups
            - X-authentik-email
  ```

  Then set `HORAE_SSO_MIDDLEWARE=horae-authentik@file`. authentik still learns the host,
  method and URI: Traefik sends the ones it received. The reference server's shared
  `authentik@file` (the default name) has `trustForwardHeader: true`. It may be used only under
  the condition in [Forwarded headers](#forwarded-headers). The outpost service is shared
  either way:

  ```yaml
  http:
    services:
      sso:
        loadBalancer:
          servers:
            - url: http://authentik-server:9000/outpost.goauthentik.io
  ```

  `HORAE_SSO_MIDDLEWARE` is never empty: an empty value falls back to the default (`:-`). A
  name the file provider does not define makes Traefik refuse the main router, so nothing but
  `/act/` and the outpost path is served (fails closed).
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
- **No published port, no network of its own.** Both services stay on the resource's own
  network, which Coolify creates and connects the proxy to ([The network](#the-network)); no
  `traefik.docker.network` label.
- **Coolify writes every variable of the resource into one `.env` and injects it into every
  service** (`env_file: .env` on each, `serviceParser`), whatever the template's
  `environment:` lists. So `horae-init` receives the secrets too (the API key, the push URLs).
  It is the same image and user, runs only `horae init` on an empty volume and exits, so this
  does not widen what a compromise of Horae could read; but do not add services to this
  resource that should not see Horae's secrets.
- `HORAE_ALLOWED_HOSTS` is `HORAE_PUBLIC_HOST`, the public host name users reach (required).
  **One host only**: the routers' `Host()` takes one name, so a second name (an alias, a
  `www.`) is not served by these routers, and Horae answers it `400 invalid_host` if it reaches
  Horae another way. Write a bare DNS name: no scheme, port, path, or a second name. A value
  with a port makes `horae serve` refuse to start (a usage error) and the router match
  nothing; two names make the router's rule invalid: either way nothing is served (fails
  closed). At request time, any `Host` other than that name (another
  name, a malformed or repeated header) gets `400 invalid_host` (fails closed). Upper case and
  a port in the request's `Host` are **not** refused: Traefik's `Host()` matcher and Horae
  both compare the name case-insensitively, without the port, so `HORAE.example.org:443` is
  served as `horae.example.org`.
- `HORAE_SCHEDULER=external` by default: Coolify's Scheduled Tasks run the pipelines.

### The network

Any container on a network with Horae can call port 8080 directly, **without SSO**, and send any
`Host` (the API trusts its network: [decision 51](../../schemas/README.md#decisions) B; the
Host check stops browsers, not neighbours). So Horae must be on no network that another app
joins.

**Horae stays on its resource's own network, and nothing else.** Coolify creates one network
per resource (named after the resource's UUID) and **connects its proxy, `coolify-proxy`, to
every resource's network** (read on the reference server: the proxy is attached to each
resource's UUID network, plus `coolify`, `traefik-network` and the tunnel's network). So
Horae's resource network already holds exactly the proxy and Horae's own containers (`horae`,
and `horae-init` while it runs): Traefik reaches port 8080 there, and no other app does. The
template therefore declares no network and no `traefik.docker.network`: Traefik uses the only
network it shares with the container. If a provider-level default network is set
(`--providers.docker.network`) and Horae is not on it, Traefik logs a warning and still uses
the container's network.

This is simpler than a hand-made network: no `docker network create`, no edit of the proxy's
compose file (`/data/coolify/proxy/docker-compose.yml`, service `traefik`), nothing lost when
Coolify recreates the proxy, and no variable to get wrong. Coolify maintains the connection,
as it does for every resource with a domain.

**Not a shared proxy network.** A server-wide `traefik-network` that every label-routed app
joins (as on the reference server: authentik, ntfy, Uptime Kuma, media and download
containers, ...) is **not acceptable for Horae**: every one of those containers could read
`/runs` and `/healthz` without signing in, so a compromise of the least trusted app on the
server (a torrent client, a media server parsing untrusted files) would reach Horae's API with
the SSO never consulted. The other apps may stay on that network; **Horae does not join it**
(no `networks:` entry, no `traefik.docker.network=traefik-network`).

Keep **"Connect to predefined network"** off for this resource: it would also put Horae on the
shared `coolify` network, next to every other resource (and, with two networks, Traefik could
pick either one). These network settings fail **open** for neighbours (everything keeps
working, and the neighbours reach port 8080 without SSO): check them after every change
([Verify on the server](#verify-on-the-server)).

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
- `tests/test_deploy_templates.py` renders the template, routes requests through its routers
  with the file provider's middleware and service, and checks the criterion of #41; CI also
  runs `docker compose config` on it.

If something is misconfigured, the template **fails closed**: with label escaping on (below),
the labels' variables stay literal (`Host(${HORAE_PUBLIC_HOST})` never matches, the entry point
`${...}` does not exist): nothing is served. A middleware name the file provider does not
define makes Traefik refuse the main router. The exceptions are the network settings
([The network](#the-network)) and a domain set on the service: they fail **open**.

### Forwarded headers

**Requirement:** no entry point that carries Horae's routers trusts forwarded headers. Neither
`http` nor `https` may set `forwardedHeaders.trustedIPs` or `forwardedHeaders.insecure` in
Traefik's static configuration. Traefik's documented behaviour for an untrusted peer is then to
drop the `X-Forwarded-*` headers the peer sends (`cloudflared` in the tunnel mode, the client
otherwise) and set its own from the request it actually received, before any middleware runs.
Do not rely on that description alone: the spoofed `X-Forwarded-Uri` check of
[Verify on the server](#verify-on-the-server) is **mandatory**. The reference server meets the
requirement: the proxy's command line (`coolify-proxy`) sets no `forwardedHeaders` option, nor
`encodedCharacters` or `sanitizePath`, so Traefik 3.7's defaults apply.

- **Preferred: a Horae-only middleware with `trustForwardHeader: false`**
  ([the YAML above](#the-layout-hand-written-routers-file-provider-sso-the-resources-own-network)).
  authentik is then told the method, host and URI Traefik received, whatever the entry points
  trust. Our guidance for a forwardAuth the templates define by label is the same: the
  [alternative](#alternative-a-coolify-domain-and-a-label-defined-forwardauth) and
  [compose.md](compose.md) use `trustForwardHeader=false`.
- **The shared `authentik@file` with `trustForwardHeader: true`** passes the request's
  `X-Forwarded-*` (`-Uri`, `-Host`, `-Method`, ...) on to authentik, which decides on them.
  That is safe only under the requirement above. **It stops being safe as soon as an entry
  point serving Horae trusts its peer**: `trustedIPs` including `cloudflared`'s address or its
  Docker network (tempting, to get the client IP in `X-Forwarded-For`), or `insecure: true`.
  Cloudflare and `cloudflared` pass a client's own `X-Forwarded-Uri` / `X-Forwarded-Host`
  through. A client could then request `/runs` with `X-Forwarded-Uri: /act/x` (or another
  app's host), and authentik would decide on that path instead. An unauthenticated-path rule,
  or another app's policy, would then let the request through to Horae.

**Tunnel mode: the scheme authentik sees (to verify).** Behind the tunnel, Traefik receives
plain `http` from `cloudflared`. A Horae-only middleware with `trustForwardHeader: false` tells
authentik `X-Forwarded-Proto: http`, the scheme Traefik received, and authentik may then build
its sign-in redirect (the `rd` back to Horae) as `http://...`, or reject a URL that does not
match the provider's `https://` external host: sign-in loops or lands on `http://` (Cloudflare's
*Always Use HTTPS* then redirects it, if on). In the tunnel mode, either:

- keep the shared `authentik@file` with `trustForwardHeader: true`, **only** under the
  requirement above (no `trustedIPs`, no `insecure`: verified on the reference server, whose
  proxy sets no `forwardedHeaders` option), the setup the reference server's other apps sign
  in with today; or
- use the Horae-only middleware and make authentik build its URLs from the provider's
  **external host** (`https://<public host>`, set in the Proxy Provider), not from the
  forwarded scheme.

Which of the two the reference server needs is **to verify** at the first deployment: sign in
through the tunnel and check that the browser ends on `https://<public host>/...`. In the
default `https` mode Traefik received TLS itself and the question does not arise.

Check the requirement on the server ([Verify on the server](#verify-on-the-server)) after every
change to Traefik's static configuration.

### Several Horae on one server

Every Traefik name of the template carries `HORAE_TRAEFIK_NAME` (`<name>`, `<name>-act`,
`<name>-outpost`, the service `<name>`), so give each copy its own name and public host. Two
copies with the **same** name clash: the routers differ (their hosts) and Traefik drops a
router that two containers define differently, so neither host is served (fails closed,
`test_coolify_two_horae_with_the_same_name_fail_closed`).

### Alternative: a Coolify domain and a label-defined forwardAuth

When you want Coolify to issue the certificate (Traefik is the edge, no tunnel), the earlier
variant of this template still works and is not shipped as a file: give the `horae` service a domain
(`SERVICE_URL_HORAE_8080` in its environment, `HORAE_ALLOWED_HOSTS=${SERVICE_FQDN_HORAE}`),
remove the three routers above, and add a forwardAuth middleware by label
(`traefik.http.middlewares.<name>-sso.forwardauth.address=...`,
`...trustForwardHeader=false`), which Coolify adds to every router it generates for the domain,
plus one `<name>-act` router (`Host(${SERVICE_FQDN_HORAE}) && PathPrefix(/act/)`, entry point
`https`, `tls=true`, priority 1000). The network is the same: the resource's own, which
Coolify connects to the proxy. The routing tests cover the shipped template only; [compose.md](compose.md)'s `traefik.yaml` is the tested label-defined forwardAuth.

## Install

### 1. Prerequisites on the server

1. **authentik**: a **Proxy Provider** in *Forward auth (single application)* mode for
   `https://<public host>`, an Application using it, added to the embedded outpost (authentik's
   docs, *Traefik* forward auth). Domain-level mode works too.
2. **The SSO middleware and outpost service** in Traefik's dynamic file configuration
   (`/data/coolify/proxy/dynamic/`, the YAML
   [above](#the-layout-hand-written-routers-file-provider-sso-the-resources-own-network)):
   preferably `horae-authentik` with `trustForwardHeader: false`. The address is authentik's
   container on a network Traefik shares with it.
3. **Transport**, one of:
   - **Default, `https`:** a certificate for the public host served by Traefik's `https` entry
     point (`HORAE_TRAEFIK_CERTRESOLVER=letsencrypt` with Coolify's default resolver, an
     entry-point certificate resolver, a file-provided certificate, or a Cloudflare origin
     certificate), and DNS for the host pointing at the server.
   - **Tunnel (opt-in):** in Cloudflare Zero Trust, *Networks → Tunnels → your tunnel → Public
     Hostname → Add*: the subdomain and domain of `HORAE_PUBLIC_HOST` (`horae.example.org`),
     *Service* **`HTTP`** and **URL `coolify-proxy:80`**. That is Traefik's `http` entry point,
     reached by its container name on a network `cloudflared` shares with the proxy, or the
     server's address and port 80 if `cloudflared` runs on the host network. Use neither
     `HTTPS` nor `:443`: the routers are then on `http`. Leave *HTTP Host Header* empty, so
     Traefik sees the public host name; Cloudflare creates the DNS record. **Port 80 must not be
     reachable from the Internet** (firewall it; only `cloudflared` reaches it), and turn on
     *Always Use HTTPS* in Cloudflare.

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
   | `HORAE_PUBLIC_HOST` | the public host name, e.g. `horae.example.org` (one name; it is also `HORAE_ALLOWED_HOSTS`) |
   | `HORAE_ANTHROPIC_API_KEY` | the Anthropic API key (secret) |
   | `HORAE_MONITOR_DAILY_BRIEF_URL` | the Uptime Kuma push URL of `daily-brief` (secret) |
   | `HORAE_MONITOR_REMIND_URL` | the Uptime Kuma push URL of `remind` (secret) |

   Optional, with their defaults:

   | Variable | Default | Tunnel mode | |
   |---|---|---|---|
   | `HORAE_TRAEFIK_ENTRYPOINT` | `https` | `http` | the entry point the routers are on |
   | `HORAE_TRAEFIK_TLS` | `true` | `false` | Cloudflare terminates TLS in the tunnel mode |
   | `HORAE_TRAEFIK_CERTRESOLVER` | empty | empty | `https` mode: the proxy's certificate resolver (`letsencrypt` on Coolify's default proxy); set, every router gets `tls.certresolver` |
   | `HORAE_SSO_MIDDLEWARE` | `authentik@file` | | prefer `horae-authentik@file` (`trustForwardHeader: false`); empty means the default |
   | `HORAE_SSO_OUTPOST_SERVICE` | `sso@file` | | the authentik outpost service |
   | `HORAE_TRAEFIK_NAME` | `horae` | | prefix of the router and service names; unique per Horae on the server |

   An optional variable set to an empty value is the same as unset: `HORAE_TRAEFIK_ENTRYPOINT`,
   `HORAE_TRAEFIK_TLS`, `HORAE_TRAEFIK_NAME`, `HORAE_SSO_MIDDLEWARE` and
   `HORAE_SSO_OUTPOST_SERVICE` fall back to their defaults (`:-`), and an empty
   `HORAE_TRAEFIK_CERTRESOLVER` adds no `tls.certresolver` (the line then renders as a
   `<name>.certresolver` label that Traefik ignores; an empty `tls.certresolver` would turn TLS
   on and break the tunnel mode). The template writes that line with Compose's `${VAR:+...}`:
   that Coolify hands it to Compose unchanged (with label escaping off) is **to verify**; if
   it does not, delete the three `certresolver` lines.
   | `HORAE_SCHEDULER` | `external` | | or `builtin` |
   | `HORAE_IMAGE_TAG` | the template's | | see [Updates](#updates) |

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
- Traversals out of `/act/` ask for sign-in, each sent with `curl --path-as-is` (without
  following redirects, `-o /dev/null -w '%{http_code} %{redirect_url}\n'`): `/act/../runs`,
  `/act/%2e%2e/runs`, `/act/..%2Fruns` and `//act/../runs`, on `https://horae.example.org`.
  Each must answer the redirect to authentik, never Horae's `/runs` body.
- **Mandatory:** a request for `/runs` with `-H 'X-Forwarded-Uri: /act/x'` (and one with
  `-H 'X-Forwarded-Host: <another app's host>'`) still asks for sign-in, through the public
  path (the tunnel in the tunnel mode).
- **No other router above priority 1000 matches the host:** in Traefik's dashboard or API
  (`/api/http/routers`), no router other than `<name>-act` and `<name>-outpost` has a priority
  of 1000 or more and a rule that matches `HORAE_PUBLIC_HOST` (a catch-all `HostRegexp`, a
  `PathPrefix` without `Host`, another app's router for a wildcard). Such a router would win
  over Horae's and decide what the host serves.
- **Forwarded headers:** Traefik's static configuration has no `forwardedHeaders.trustedIPs`
  and no `forwardedHeaders.insecure` on any entry point carrying Horae's routers (the
  `traefik` command or `traefik.yaml` in the proxy configuration), and `sanitizePath` is not
  set to `false`. Better, `HORAE_SSO_MIDDLEWARE` names a Horae-only middleware with
  `trustForwardHeader: false` ([Forwarded headers](#forwarded-headers)).
- **The network:** `docker inspect` of the `horae` container lists **one** network, the
  resource's (named after its UUID); not `coolify`, not `traefik-network`.
  `docker network inspect <that network> --format '{{range .Containers}}{{.Name}} {{end}}'`
  lists only `coolify-proxy` and this resource's containers. From any other container (a
  neighbour on `traefik-network`, say), `curl -m 5 http://<horae container>:8080/healthz` must
  fail. If the container has more than one network, Traefik may pick the wrong one (a `502`,
  fails closed) and a neighbour may share it: fix the network, do not add a label.
- **Tunnel mode only:** from outside the server, `curl -m 5 http://<server public address>/`
  times out (port 80 is closed to the Internet), and `http://horae.example.org/` is redirected
  to `https://` by Cloudflare.
- `docker inspect` of the `horae` container: the labels above, `traefik.enable=true`, **no**
  `http-0-...` / `https-0-...` routers (no Coolify domain), no `traefik.docker.network`.
- Traefik's dashboard or API: the three routers `<name>@docker`, `<name>-act@docker`,
  `<name>-outpost@docker` on the chosen entry point, with no error; `<name>` uses the SSO
  middleware.

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
| Cloudflare `404` / Traefik `404` for the host | label escaping still on (no router matches), `HORAE_TRAEFIK_ENTRYPOINT` differs from the entry point the traffic reaches (tunnel mode: the tunnel's service is `HTTP` to port 80, and the variables are `http` / `false`), or the proxy is not on the resource's network (Traefik logs it cannot reach the container: a `502`) |
| Certificate warning on the default `https` mode | no certificate for the public host on Traefik's `https` entry point: Coolify issues none for a service without a domain; set `HORAE_TRAEFIK_CERTRESOLVER` (`letsencrypt`) ([the layout](#the-layout-hand-written-routers-file-provider-sso-the-resources-own-network)) |
| `/act/...` works, everything else `404` | Traefik refused the main router: `HORAE_SSO_MIDDLEWARE` names a middleware its file configuration does not define (Traefik's log says so) |
| Every request fails (`500`) or is never redirected to sign-in | authentik is unreachable from Traefik at the shared middleware's address |
| Sign-in loops, or the callback is `404` | the outpost router is missing or `HORAE_SSO_OUTPOST_SERVICE` is wrong: `/outpost.goauthentik.io/` must reach authentik |
| Tunnel mode: sign-in returns to `http://...`, or loops | authentik built the URL from the forwarded scheme (`http`, what Traefik received from `cloudflared`): [Forwarded headers](#forwarded-headers), the tunnel-mode note |
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

Also read from it (read-only `docker inspect`): the proxy container `coolify-proxy` is the
compose service `traefik` of `/data/coolify/proxy/docker-compose.yml` (project
`coolify-proxy`); its command line sets no `encodedCharacters`, `sanitizePath` or
`forwardedHeaders` options (Traefik 3.7 defaults); and Coolify connects it to every resource's
own network (besides `coolify`, `traefik-network` and the tunnel's network).

The template keeps two things stricter than that server: Horae stays on its resource's own
network and does not join `traefik-network`, and the transport defaults to `https` with TLS,
the tunnel being an opt-in.

Verified without that server: the rendered template's routing and settings in both modes with
that file configuration (`tests/test_deploy_templates.py`), `docker compose config` (CI job `deploy`),
and the template's services run by plain Docker Compose against an image built from the tree
(healthy; `horae run` through `docker exec`). **Not verified yet**, for the maintainer on the
real server ([the checklist](#verify-on-the-server)): the deployment and its health status,
the blocking of required variables in the UI, the escape toggle, that Coolify adds no router of
its own and keeps `traefik.enable=true` for a service without a domain, that the `horae`
container is on its resource's network alone and Traefik reaches it there without
`traefik.docker.network`, the scheme authentik sees in the tunnel mode (the shared
`trustForwardHeader: true` middleware or the Horae-only one with the provider's external host),
port 80 closed to the Internet in the tunnel mode, the authentik forward auth and its callback
through the outpost router end to end (and the redirect back to `https://`), the encoded
traversals and a spoofed `X-Forwarded-Uri` through the tunnel, no other router above priority
1000 on the host, `HORAE_TRAEFIK_CERTRESOLVER` (`${VAR:+...}`) passed through by Coolify,
Scheduled Tasks in a two-container stack (and their timeout), and the YAML anchor
(`x-horae-image`) read by Coolify's parser.

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
