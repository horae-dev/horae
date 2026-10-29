# Running Horae with plain Docker Compose

- **Issue:** [#41](https://github.com/horae-dev/horae/issues/41); threat T15 in
  [docs/security.md](../security.md). Coolify: [coolify.md](coolify.md).
- **Files:** [`deploy/compose/`](../../deploy/compose/): `docker-compose.yaml` (Horae alone, on
  loopback), `traefik.yaml` (optional override: behind Traefik and an SSO forwardAuth) and
  `.env.example`.

> **Check first that the image can be pulled without credentials.** The files pull
> `ghcr.io/horae-dev/horae` anonymously, which works only once the GHCR package is **public**
> (a setting of the `horae-dev` organisation). Check:
>
> ```sh
> curl -s -o /dev/null -w '%{http_code}\n' \
>   'https://ghcr.io/token?scope=repository:horae-dev/horae:pull'
> ```
>
> `200`: public. `401`: `docker compose up` fails at the pull (*denied* / *unauthorized*);
> log in first ([registry credentials](#registry-credentials-private-image)), or build the
> image yourself and point the services at it ([Testing locally](#testing-locally)).

The contract is the Coolify template's: the image's command (`horae serve`), user `10001`,
tini and `HEALTHCHECK`; a `horae-init` one-off that runs `horae init /instance` on a new, empty
volume; a named volume on `/instance`; read-only root filesystem, a 64 MiB tmpfs on `/tmp`
(`noexec,nosuid,nodev`: Horae never executes anything from it), `pids_limit: 256`, every
capability dropped, `no-new-privileges`, `stop_grace_period: 30s`. Differences: the API is published on
**127.0.0.1 only**, secrets are **files** (Horae's default), and the built-in scheduler is the
default.

## Install

```sh
cd deploy/compose
cp .env.example .env          # then edit it
mkdir -p secrets               # secret files go here (below)
docker compose up -d
docker compose ps             # horae: Up (healthy); horae-init: Exited (0)
curl -s http://127.0.0.1:8080/healthz
```

- `HORAE_SECRETS_DIR` (**required**: Compose refuses to start without it) is a directory of
  secret files mounted read-only on `/run/secrets`, Horae's default secrets directory: one file
  per secret, named after it (`secrets/ANTHROPIC_API_KEY`, `secrets/MONITOR_REMIND`, ...), with
  no reference needed in `horae.yaml` ([decision 25](../../schemas/README.md#decisions)). The
  directory and files must be readable by uid 10001, and never in the instance repository
  (`deploy/compose/secrets/` is git-ignored). An empty directory is fine for the example
  instance. After adding the files:

  ```sh
  sudo chown -R 10001:10001 secrets
  sudo chmod 0700 secrets
  sudo find secrets -type f -exec chmod 0400 {} +   # also fine on an empty directory
  ```
- `HORAE_IMAGE_TAG`: empty uses the default written once in `docker-compose.yaml`
  (`x-horae-image`, a `sha-<short>` of `main`, bumped as a release step:
  [container-image.md](container-image.md#bumping-the-deploy-templates)); or a pinned tag
  (`sha-<short>`, later `X.Y.Z`). **In production pin the digest too**:
  `<tag>@sha256:<digest>`, the digest whose signature you verified. To update: verify the
  signature, change the tag, `docker compose up -d`.
- Memory: no limit is set. To cap it, add `mem_limit: 512m` (for example) to the `horae`
  service in an override file; a run that exceeds it is killed by the kernel and recorded as
  failed, so size it above your largest run.
- `HORAE_LOCAL_PORT`: the loopback port (`127.0.0.1:8080` by default). The bind address is
  fixed to `127.0.0.1` in the file on purpose: **the API has no authentication of its own**;
  never publish it on another interface. Every local user of the host can read it.

Your instance: `docker compose cp horae.yaml horae:/instance/horae.yaml` (and `pipelines/`,
`horae.lock`), then `docker compose exec horae horae doctor` and `docker compose restart horae`.
A model secret as a file, for example:

```yaml
models:
  main: {backend: anthropic, model: <a Claude model id>, secret: ANTHROPIC_API_KEY}
scheduler:
  monitors:
    remind: {secret: MONITOR_REMIND}   # secrets/MONITOR_REMIND holds the push URL
```

## Scheduling: built-in or host cron

- **Built-in** (`HORAE_SCHEDULER=builtin`, the default here): `horae serve` fires each
  pipeline's `trigger: {cron: ...}` in the instance's time zone. Nothing else to set up.
- **External** (`HORAE_SCHEDULER=external`): `horae serve` fires nothing; the host's cron or a
  systemd timer runs the pipelines in the running container:

  ```cron
  # crontab of a user in the docker group; the times are the host's time zone
  0 6,18 * * *  cd /srv/horae/deploy/compose && docker compose exec -T horae horae run daily-brief
  */5 * * * *   cd /srv/horae/deploy/compose && docker compose exec -T horae horae run remind
  ```

  Exit codes are Horae's (`0` ok, `1` failed, `5` locked: the previous run still holds the
  pipeline's lock). Use one mode, not both.

Either way, `scheduler.monitors.<pipeline>` pings an Uptime Kuma (or Healthchecks) push URL
after each run; create a **Push** monitor per pipeline with a heartbeat interval a little longer
than its schedule, and store the push URL as a secret file. Details:
[coolify.md](coolify.md#5-push-monitors-uptime-kuma).

## Behind Traefik and SSO (`traefik.yaml`)

```sh
docker network create --internal horae-proxy      # shared ONLY by Horae and Traefik
docker network connect horae-proxy traefik        # your Traefik container
docker compose -f docker-compose.yaml -f traefik.yaml up -d
```

- **Required** with the override: `HORAE_PUBLIC_HOST` (the public host name; it also becomes
  `HORAE_ALLOWED_HOSTS`) and `HORAE_SSO_FORWARD_AUTH_URL` (the SSO provider's forwardAuth
  address, e.g. authentik's outpost
  `http://authentik-server:9000/outpost.goauthentik.io/auth/traefik`, reachable from Traefik).
  Optional: `HORAE_PROXY_NETWORK`, `HORAE_TRAEFIK_ENTRYPOINT` (`websecure`),
  `HORAE_TRAEFIK_CERTRESOLVER` (`letsencrypt`); match your Traefik's names.
- Two routers: `horae` (the whole host: `/runs`, `/healthz`, everything) with the `horae-sso`
  forwardAuth middleware, and `horae-act` (`PathPrefix(/act/)`, priority 1000) without it. Only
  `/act/` is exempt from SSO (`tests/test_deploy_templates.py`). Keep the entry point's
  `sanitizePath` on (Traefik's default), so that `/act/../runs` is routed as `/runs`.
- `trustForwardHeader=false` on `horae-sso`: the SSO provider decides on what Traefik received,
  never on `X-Forwarded-*` headers a client sent (a spoofed `X-Forwarded-Uri` could make it
  decide on another path than the one routed), even if your entry point trusts an upstream
  proxy's forwarded headers ([coolify.md](coolify.md#routing-only-act-is-exempt-from-sso)).
- **Network**: Horae joins `horae-proxy`, which must hold only Horae and Traefik. Any container
  on a network with Horae can reach port 8080 and send any `Host` (the Host check stops
  browsers, not neighbours; [decision 51](../../schemas/README.md#decisions) B). `--internal`
  keeps that network off the outside world; Horae's own egress (LLM API, push monitors) goes
  through its `default` network. Do not attach Horae to a Traefik network that other apps share.
- The loopback port stays published, for local checks.

## Registry credentials (private image)

While the GHCR package is not public, log the host's Docker in with a GitHub token that can
only read packages (a classic personal access token with the single scope `read:packages`,
with an expiry), as the user that runs `docker compose`, without the token in the history:

```sh
read -rs GHCR_TOKEN && printf '%s' "$GHCR_TOKEN" \
  | docker login ghcr.io -u <github user> --password-stdin; unset GHCR_TOKEN
```

The login is stored in `~/.docker/config.json`, unencrypted unless a credential helper is set
up.

## Testing locally

Against an image built from the tree, without pulling (an override file, not an edit of
`docker-compose.yaml`):

```sh
docker build -t horae:local .                      # at the repository root
cd deploy/compose
cat > local.yaml <<'EOF'
services:
  horae-init: {image: "horae:local"}
  horae: {image: "horae:local"}
EOF
docker compose -f docker-compose.yaml -f local.yaml up -d
```

## Backups

Back up the `horae_horae-instance` volume (configuration, lock, store, vault) and the secrets
directory, with an encrypted offsite copy; for the SQLite store follow
[retention-and-backups.md](retention-and-backups.md).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `required variable HORAE_SECRETS_DIR is missing a value` | set it in `.env` (or `HORAE_PUBLIC_HOST`, `HORAE_SSO_FORWARD_AUTH_URL` with `traefik.yaml`) |
| `400 {"kind": "invalid_host", ...}` through the proxy | the proxied `Host` is not in `HORAE_ALLOWED_HOSTS` (`HORAE_PUBLIC_HOST` with the override); loopback always works. The log names each refused host once |
| A secret "not found" or unreadable | the file is missing in `HORAE_SECRETS_DIR`, or not readable by uid 10001 |
| `horae-init` exited 3, `horae` never starts | the volume is not empty and has no `horae.yaml` |
| `docker compose up`: *denied* / *unauthorized* pulling the image | the GHCR package is not public: [registry credentials](#registry-credentials-private-image) |
| Traefik: `404` for the host | Traefik is not on `HORAE_PROXY_NETWORK`, or the entry point / resolver names differ from yours |

Verified locally: `docker compose config` with `.env.example` (and refused without
`HORAE_SECRETS_DIR`), and `docker compose up` against an image built from the tree: healthy,
`/runs` answers with an allowed `Host`, `400 invalid_host` with another, `docker compose exec
horae horae run daily-fixture` works (with the `noexec` tmpfs and `pids_limit`), `docker compose
stop` exits cleanly, and a redeploy keeps the instance. The Traefik override
was rendered and its routing tested, not run against a live Traefik.
