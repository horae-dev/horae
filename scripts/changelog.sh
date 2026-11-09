#!/usr/bin/env bash
# Prepend the section of the next release to CHANGELOG.md, from the pull requests merged since the
# previous release tag, grouped by label as in .github/release.yml.
#
#   scripts/changelog.sh X.Y.Z [main]
#
# Run it before tagging, on the release branch (or on main), commit the result in the version-bump
# pull request, review it. Needs `gh` (logged in; read access to the repository is enough for the
# generated notes of an existing tag range) and `jq`. The tag does not need to exist yet.
set -euo pipefail

repo="horae-dev/horae"
version="${1:-}"
target="${2:-main}"
if [[ ! "${version}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "usage: $0 X.Y.Z [target-branch]   (plain semver, no leading v)" >&2
  exit 2
fi
cd "$(dirname "$0")/.."

if grep -q "^## v${version} " CHANGELOG.md; then
  echo "CHANGELOG.md already has a section for v${version}" >&2
  exit 1
fi

# The previous release: the highest plain-semver vX.Y.Z tag other than this one.
previous="$(gh api "repos/${repo}/tags" --paginate --jq '.[].name' \
  | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
  | grep -vx "v${version}" | sort -V | tail -n 1 || true)"

args=(-f "tag_name=v${version}" -f "target_commitish=${target}")
if [ -n "${previous}" ]; then
  args+=(-f "previous_tag_name=${previous}")
fi
body="$(gh api -X POST "repos/${repo}/releases/generate-notes" "${args[@]}" --jq .body \
  | grep -vx "## What's Changed")"

section="## v${version} - $(date -u +%Y-%m-%d)

${body}
"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
# Keep the intro (everything before the first "## v"), then the new section, then the older ones.
first="$(grep -n -m 1 '^## v' CHANGELOG.md | cut -d: -f1 || true)"
if [ -n "${first}" ]; then
  { head -n $((first - 1)) CHANGELOG.md; printf '%s\n' "${section}"; tail -n +"${first}" CHANGELOG.md; } > "${tmp}"
else
  { cat CHANGELOG.md; printf '\n%s\n' "${section}"; } > "${tmp}"
fi
cat "${tmp}" > CHANGELOG.md
echo "added v${version} (since ${previous:-the beginning}) to CHANGELOG.md; review and edit it"
