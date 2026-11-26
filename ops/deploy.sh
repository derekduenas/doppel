#!/bin/sh
# Release: tests -> commit must be clean -> push GitHub + Apex mirror -> build /opt/doppel.new on
# Apex -> atomic swap. Does NOT restart doppel-tape unless --restart-tape (the recorder keeps running
# its loaded code; restart only when tape code changed).
set -eu
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "uncommitted tracked changes; commit first" >&2; exit 1; }  # release = git archive HEAD
# Release only main: the script pushes main but packages HEAD, so a pod or detached checkout would
# otherwise deploy unreviewed code while both remote main refs still look correct (Astra audit).
[ "$(git symbolic-ref -q --short HEAD || true)" = main ] || { echo "not on branch main; refusing to deploy" >&2; exit 1; }
uv run pytest -q
uv run pyright src
uv export --no-dev --no-hashes --no-emit-project -q > ops/requirements.lock
git diff --quiet ops/requirements.lock || { echo "requirements.lock changed; commit it" >&2; exit 1; }
git push -q origin main
git push -q apex main
SHA=$(git rev-parse HEAD)
git fetch -q origin main
git fetch -q apex main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && [ "$(git rev-parse HEAD)" = "$(git rev-parse apex/main)" ] \
  || { echo "HEAD differs from origin/main or apex/main after push; refusing to deploy" >&2; exit 1; }
mode=keep
if [ "${1:-}" = "--restart-tape" ]; then mode=restart; fi
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
git archive --format=tar --output="$archive" "$SHA"
installer=$(git show "$SHA:ops/install-release.sh")
# The remote command consumes only the completed archive, with the installer from the same commit.
ssh atlas-do "set -- /opt $SHA $mode /srv/git/doppel.git
$installer" < "$archive"
