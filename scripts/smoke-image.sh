#!/usr/bin/env bash
# Smoke test for the container image (#40). Usage: scripts/smoke-image.sh IMAGE
# Runs the image the way production does: read-only root filesystem, tmpfs /tmp, no network,
# all capabilities dropped, no privilege escalation.
set -euo pipefail

image="${1:?usage: smoke-image.sh IMAGE}"
run=(docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --network none
     --cap-drop ALL --security-opt no-new-privileges)

uid="$("${run[@]}" --entrypoint id "$image" -u)"
if [ "$uid" = "0" ]; then
  echo "FAIL: the image runs as root (uid 0)" >&2
  exit 1
fi
echo "ok: runs as uid ${uid}"

configured="$(docker image inspect --format '{{.Config.User}}' "$image")"
case "$configured" in
  ''|0|0:*|root|root:*) echo "FAIL: image USER is '${configured}'" >&2; exit 1 ;;
esac
echo "ok: image USER is ${configured}"

"${run[@]}" "$image" --version
"${run[@]}" "$image" --help > /dev/null
echo "ok: horae --version and --help work with a read-only root filesystem"

# A write to the root filesystem must fail (proves --read-only is in effect), /tmp must work.
if "${run[@]}" --entrypoint sh "$image" -c 'touch /usr/probe' 2>/dev/null; then
  echo "FAIL: the root filesystem is writable" >&2
  exit 1
fi
"${run[@]}" --entrypoint sh "$image" -c 'touch /tmp/probe'
echo "ok: root filesystem read-only, /tmp writable"

# No package manager or compiler in the final image.
for tool in pip uv gcc cc make; do
  if "${run[@]}" --entrypoint sh "$image" -c "command -v ${tool}" > /dev/null 2>&1; then
    echo "FAIL: ${tool} is present in the final image" >&2
    exit 1
  fi
done
echo "ok: no pip, uv or build tools"
