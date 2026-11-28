#!/bin/sh
# Release only the exact main commit verified by hosted GitHub Actions. The lead runs
# this script from a checkout. The recorder restarts only with --restart-tape.
set -eu
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "uncommitted tracked changes; commit first" >&2; exit 1; }  # release = git archive HEAD
# Release only main: the script pushes main but packages HEAD, so a pod or detached checkout would
# otherwise deploy unreviewed code while both remote main refs still look correct (Astra audit).
[ "$(git symbolic-ref -q --short HEAD || true)" = main ] || { echo "not on branch main; refusing to deploy" >&2; exit 1; }
SHA=$(git rev-parse HEAD)
git fetch -q origin main
[ "$SHA" = "$(git rev-parse origin/main)" ] || { echo "HEAD differs from GitHub main" >&2; exit 1; }
ci_head=$(gh run list --workflow ci.yml --branch main --event push --commit "$SHA" --status success \
    --limit 1 --json headSha --jq '.[0].headSha // ""')
[ "$ci_head" = "$SHA" ] || { echo "no successful hosted CI run for $SHA on main" >&2; exit 1; }
git push -q apex main
git fetch -q apex main
[ "$SHA" = "$(git rev-parse apex/main)" ] || { echo "HEAD differs from Apex main" >&2; exit 1; }
mode=keep
if [ "${1:-}" = "--restart-tape" ]; then mode=restart; fi
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
git archive --format=tar --output="$archive" "$SHA"
installer=$(git show "$SHA:ops/install-release.sh")
# The remote command consumes only the completed archive, with the installer from the same commit.
ssh atlas-do "set -- /opt $SHA $mode /srv/git/doppel.git
$installer" < "$archive"
