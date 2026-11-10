# Quick start

Three ways in, from the quickest to the one you would keep running:

1. [Try it from a checkout](#try-it-from-a-checkout) (no Docker, no secret, no network): a first
   pipeline, its dry run, undo and replay.
2. [Docker Compose](#docker-compose): the container image on a machine with Docker, API on
   loopback.
3. [Coolify](#coolify): the reference deployment, behind Traefik and SSO.

!!! warning "Provisional"
    Horae is pre-release and its decisions are provisional. The image tag in the deploy
    templates is bumped by hand, and anything that says "planned" is not on `main` yet.

## Try it from a checkout

You need `git` and [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs
Python 3.12 itself).

```sh
git clone https://github.com/horae-dev/horae.git
cd horae
uv sync
uv run horae init ~/horae-demo
uv run horae doctor -i ~/horae-demo
uv run horae run daily-fixture -i ~/horae-demo --dry-run
uv run horae run daily-fixture -i ~/horae-demo
uv run horae runs -i ~/horae-demo
```

`doctor` prints `OK: 1 pipeline, 2 plugins, 0 model aliases`. The dry run shows the note the
pipeline would write and writes nothing; the real run writes
`~/horae-demo/vault/Daily/<today>.assistant.md`. `horae runs` lists both runs, the dry run
marked `(dry run)`. Undo the real one, using its id from the first column:

```sh
uv run horae undo <run-id> -i ~/horae-demo
```

Undo reverses what the run did: here it removes the note, because the run created it (a note
that existed is restored byte for byte). This example has no LLM and reads synthetic data; the
[concepts](concepts.md) page explains what ran.

## Docker Compose

The template is [`deploy/compose/`](https://github.com/horae-dev/horae/tree/main/deploy/compose):
Horae alone, on `127.0.0.1`, with a read-only root filesystem, a named volume for the
instance and the built-in scheduler. You need Docker with the Compose plugin.

### 1. Check that the image can be pulled

 The templates pull `ghcr.io/horae-dev/horae`
without credentials, which works only when its package is public:

```sh
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://ghcr.io/token?scope=repository:horae-dev/horae:pull'
```

`200` means public: use step 2a. `401` means it is not (yet): use
step 2b.

### 2a. Pull the image

```sh
git clone https://github.com/horae-dev/horae.git
cd horae/deploy/compose
cp .env.example .env
mkdir -p secrets
docker compose up -d
```

### 2b. Build the image from the checkout

```sh
git clone https://github.com/horae-dev/horae.git
cd horae
docker build -t horae:local .
cd deploy/compose
cp .env.example .env
mkdir -p secrets
cat > local.yaml <<'EOF'
services:
  horae-init: {image: "horae:local"}
  horae: {image: "horae:local"}
EOF
export COMPOSE_FILE=docker-compose.yaml:local.yaml
docker compose up -d
```

### 3. Check it

```sh
docker compose ps -a
curl -s http://127.0.0.1:8080/healthz
docker compose exec horae horae doctor
docker compose exec horae horae run daily-fixture --dry-run
```

`horae` is `Up (healthy)` and `horae-init` is `Exited (0)` (it created the example instance
in the volume). `/healthz` answers `{"status": "ok", ...}`.

If `curl` on the host gets an empty reply (seen with OrbStack on macOS, whose port forwarding
differs from Docker on Linux), the container is still fine: check it from inside with
`docker compose exec horae horae doctor`.

Next:

- Put your own `horae.yaml`, `pipelines/` and `horae.lock` in the volume, and secrets as files
  in `secrets/` (one file per secret): [Docker Compose guide](operations/compose.md).
- **The API has no authentication of its own.** The template binds it to loopback on purpose;
  never publish it on another interface. For a public host use the Traefik and SSO override
  described in the guide.
- Pin the image by digest in production, and back up the volume:
  [container image](operations/container-image.md), [backups](operations/retention-and-backups.md).

## Coolify

[`deploy/coolify/docker-compose.yaml`](https://github.com/horae-dev/horae/blob/main/deploy/coolify/docker-compose.yaml)
is a Coolify **Docker Compose Empty** resource with its own Traefik routers, an SSO
forwardAuth middleware, and only `/act/` exempt from SSO. Before you start you need a Coolify
server with its Traefik proxy, an SSO provider (authentik is the reference) configured as
described in the [Coolify guide](operations/coolify.md#1-prerequisites-on-the-server), a DNS
name such as `horae.example.org` pointing at the server (or a Cloudflare Tunnel), an Anthropic
API key and two Uptime Kuma push URLs.

1. In your project: **+ New, Resource, Docker Compose Empty**. Paste the template and save.
2. **Edit Compose** and turn **off** "Escape special characters in labels".
3. Leave the **Domains** of both services empty (a Coolify domain would create routers without
   SSO).
4. In **Environment Variables**, set the four required ones. Coolify blocks the deployment
   until you do:

   | Variable | Example value |
   |---|---|
   | `HORAE_PUBLIC_HOST` | `horae.example.org` |
   | `HORAE_ANTHROPIC_API_KEY` | your key (a secret) |
   | `HORAE_MONITOR_DAILY_BRIEF_URL` | `https://kuma.example.org/api/push/<token>` |
   | `HORAE_MONITOR_REMIND_URL` | `https://kuma.example.org/api/push/<token>` |

   On Coolify's default proxy also set `HORAE_TRAEFIK_CERTRESOLVER=letsencrypt`.
5. Deploy. `horae` shows *Running (healthy)* and `horae-init` shows *Exited*.
6. Open `https://horae.example.org/healthz`: it goes through the SSO sign-in, then answers
   `{"status": "ok", ...}`.
7. Add your instance and the two **Scheduled Tasks** (`horae run daily-brief`,
   `horae run remind`), and run the checks of the guide.

The guide has the rest: the Cloudflare Tunnel mode, the exact routing and why, forwarded
headers, the checks to run on the server, updates and backups:
[Deploying Horae on Coolify](operations/coolify.md).
