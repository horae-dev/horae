#!/usr/bin/env bash
# Smoke test for the container image (#40, #37).
# Usage: scripts/smoke-image.sh IMAGE [PLATFORM]
#   PLATFORM (e.g. linux/arm64) runs the image for that platform (under QEMU when foreign).
#   EXPECTED_VERSION=X.Y.Z in the environment also checks that `horae --version` reports it.
# Runs the image the way production does: read-only root filesystem, tmpfs /tmp, no network,
# all capabilities dropped, no privilege escalation, a volume on /instance.
set -euo pipefail

image="${1:?usage: smoke-image.sh IMAGE [PLATFORM]}"
platform="${2:-}"
platform_args=()
if [ -n "$platform" ]; then
  platform_args=(--platform "$platform")
fi
base=(docker run --rm ${platform_args[@]+"${platform_args[@]}"} --tmpfs "/tmp:rw,noexec,nosuid,size=16m" --network none
      --cap-drop ALL --security-opt no-new-privileges)
run=("${base[@]}" --read-only)

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

volumes=()
containers=()
cleanup() {
  if [ "${#containers[@]}" -gt 0 ]; then
    docker rm -f "${containers[@]}" > /dev/null 2>&1 || true
  fi
  if [ "${#volumes[@]}" -gt 0 ]; then
    docker volume rm -f "${volumes[@]}" > /dev/null || true
  fi
}
trap cleanup EXIT
new_volume() {  # sets $volume (not run in a subshell, so cleanup sees every volume)
  volume="horae-smoke-$$-${#volumes[@]}"
  docker volume create "$volume" > /dev/null
  volumes+=("$volume")
}

# --- identity ------------------------------------------------------------------------------
uid="$("${run[@]}" --entrypoint id "$image" -u)"
[ "$uid" != "0" ] || fail "the image runs as root (uid 0)"
echo "ok: runs as uid ${uid}"

configured="$(docker image inspect --format '{{.Config.User}}' "$image")"
case "$configured" in
  ''|0|0:*|root|root:*) fail "image USER is '${configured}'" ;;
esac
echo "ok: image USER is ${configured}"

# --- PID 1 is tini -------------------------------------------------------------------------
entrypoint="$(docker image inspect --format '{{json .Config.Entrypoint}}' "$image")"
[ "$entrypoint" = '["tini","--","horae"]' ] || fail "entrypoint is ${entrypoint}"
# The image's own PID-1 program, started the way the entrypoint starts it, sees itself as PID 1.
pid1="$("${run[@]}" --entrypoint tini "$image" -- cat /proc/1/comm)"
[ "$pid1" = "tini" ] || fail "PID 1 is '${pid1}', not tini"
echo "ok: entrypoint ${entrypoint}, PID 1 is tini"

# --- CLI -----------------------------------------------------------------------------------
version="$("${run[@]}" "$image" --version)"
[[ "$version" =~ ^horae\ [0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] \
  || fail "unexpected --version output: '${version}'"
if [ -n "${EXPECTED_VERSION:-}" ] && [ "$version" != "horae ${EXPECTED_VERSION}" ]; then
  fail "--version reports '${version}', expected 'horae ${EXPECTED_VERSION}'"
fi
"${run[@]}" "$image" --help > /dev/null
echo "ok: ${version}; --help works with a read-only root filesystem"

# --- read-only root filesystem ---------------------------------------------------------------
# Probed as root (still with every capability dropped): root owns /usr, so the write can only
# fail because the filesystem is read-only. The same probe without --read-only must succeed,
# which proves the probe is able to fail.
probe='touch /usr/probe 2>&1'
if out="$("${run[@]}" --user 0:0 --entrypoint sh "$image" -c "$probe")"; then
  fail "the root filesystem is writable (even with --read-only)"
fi
case "$out" in
  *"Read-only file system"*) ;;
  *) fail "writing /usr failed for another reason than a read-only filesystem: ${out}" ;;
esac
"${base[@]}" --user 0:0 --entrypoint sh "$image" -c "$probe" > /dev/null \
  || fail "control probe: root cannot write /usr even without --read-only"
"${run[@]}" --entrypoint sh "$image" -c 'touch /tmp/probe'
echo "ok: root filesystem read-only (probe checked against a writable control), /tmp writable"

# --- no package manager or compiler ---------------------------------------------------------
for tool in pip pip3 uv gcc cc make; do
  if "${run[@]}" --entrypoint sh "$image" -c "command -v ${tool}" > /dev/null 2>&1; then
    fail "${tool} is present in the final image"
  fi
done
if "${run[@]}" --entrypoint python "$image" -c 'import ensurepip' > /dev/null 2>&1; then
  fail "ensurepip is present in the final image"
fi
if "${run[@]}" --entrypoint python "$image" -m pip --version > /dev/null 2>&1; then
  fail "python -m pip works in the final image"
fi
echo "ok: no pip, ensurepip, uv or build tools"

# --- an instance on a volume: init, doctor, run ----------------------------------------------
# A new named volume takes the image's /instance ownership (10001, mode 0700).
new_volume
owner="$("${run[@]}" -v "${volume}:/instance" --entrypoint stat "$image" -c '%u:%g' /instance)"
[ "$owner" = "10001:10001" ] || fail "a new volume on /instance is owned by ${owner}"
inst=("${run[@]}" -v "${volume}:/instance")
"${inst[@]}" "$image" init /instance/smoke > /dev/null || fail "horae init /instance/smoke"
"${inst[@]}" "$image" doctor -i /instance/smoke > /dev/null || fail "horae doctor"
"${inst[@]}" "$image" run daily-fixture -i /instance/smoke > /dev/null \
  || fail "horae run daily-fixture"
notes="$("${inst[@]}" --entrypoint find "$image" /instance/smoke/vault/Daily -type f -name '*.md')"
[ -n "$notes" ] || fail "horae run daily-fixture wrote no note in vault/Daily/"
echo "ok: init, doctor and run daily-fixture on a volume, no network (${notes##*/})"

# The volume's root itself, an existing empty directory (the usual `-v horae:/instance` case).
new_volume
"${run[@]}" -v "${volume}:/instance" "$image" init /instance > /dev/null \
  || fail "horae init /instance on an empty volume"
"${run[@]}" -v "${volume}:/instance" "$image" doctor > /dev/null \
  || fail "horae doctor on the volume's root (HORAE_INSTANCE=/instance)"
echo "ok: init and doctor on the root of an empty volume"

# --- the default command: horae serve, its HTTP API and the HEALTHCHECK (#37) ------------------
# Detached, without --rm, so that its exit code can be read after the stop. The image's own
# HEALTHCHECK command runs (only its interval is shortened); no network, loopback only.
cmd="$(docker image inspect --format '{{json .Config.Cmd}}' "$image")"
[ "$cmd" = '["serve"]' ] || fail "default command is ${cmd}, not [\"serve\"]"
server="horae-smoke-serve-$$"
containers+=("$server")
docker run -d --name "$server" ${platform_args[@]+"${platform_args[@]}"} \
  --tmpfs "/tmp:rw,noexec,nosuid,size=16m" --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only --health-interval 1s --health-start-period 60s \
  -v "${volume}:/instance" "$image" > /dev/null
health=""
for _ in $(seq 1 60); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$server")"
  [ "$health" = "healthy" ] && break
  [ "$(docker inspect --format '{{.State.Running}}' "$server")" = "true" ] \
    || { docker logs "$server" >&2; fail "horae serve exited"; }
  sleep 1
done
[ "$health" = "healthy" ] || { docker logs "$server" >&2; fail "horae serve is '${health}'"; }
runs_probe='import urllib.request as u, json
r = u.build_opener(u.ProxyHandler({})).open("http://127.0.0.1:8080/runs?limit=1", timeout=5)
assert r.headers["Cache-Control"] == "no-store", r.headers
print(json.load(r)["schema_version"])'
[ "$(docker exec "$server" python -c "$runs_probe")" = "1" ] || fail "GET /runs"
docker stop --time 20 "$server" > /dev/null
code="$(docker inspect --format '{{.State.ExitCode}}' "$server")"
[ "$code" = "0" ] || { docker logs "$server" >&2; fail "horae serve exited ${code} on SIGTERM"; }
docker logs "$server" 2>&1 | grep -q '^horae serve: stopped$' \
  || { docker logs "$server" >&2; fail "horae serve did not report its stop"; }
echo "ok: horae serve is healthy (HEALTHCHECK on /healthz), serves /runs, stops with exit 0"
