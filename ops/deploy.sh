#!/bin/sh
# Release: tests -> commit must be clean -> push GitHub + Apex mirror -> build /opt/doppel.new on
# Apex -> atomic swap. Does NOT restart doppel-tape unless --restart-tape (the recorder keeps running
# its loaded code; restart only when tape code changed).
set -eu
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "uncommitted tracked changes; commit first" >&2; exit 1; }  # release = git archive HEAD
uv run pytest -q
uv run pyright src
uv export --no-dev --no-hashes --no-emit-project -q > ops/requirements.lock
git diff --quiet ops/requirements.lock || { echo "requirements.lock changed; commit it" >&2; exit 1; }
git push -q origin main
git push -q apex main
SHA=$(git rev-parse --short HEAD)
git archive HEAD | ssh atlas-do "set -e; rm -rf /opt/doppel.new && mkdir -p /opt/doppel.new && tar -x -C /opt/doppel.new && echo $SHA > /opt/doppel.new/DEPLOYED_COMMIT && python3 -m venv /opt/doppel.new/.venv && /opt/doppel.new/.venv/bin/pip install -q -r /opt/doppel.new/ops/requirements.lock && /opt/doppel.new/.venv/bin/pip install -q --no-deps /opt/doppel.new && rm -rf /opt/doppel.old && mv /opt/doppel /opt/doppel.old && mv /opt/doppel.new /opt/doppel && grep -rl /opt/doppel.new /opt/doppel/.venv/bin /opt/doppel/.venv/pyvenv.cfg 2>/dev/null | xargs -r sed -i 's#/opt/doppel.new#/opt/doppel#g'; chmod 755 /opt/doppel/ops/*.sh"
if [ "${1:-}" = "--restart-tape" ]; then ssh atlas-do systemctl restart doppel-tape; fi
ssh atlas-do "echo deployed \$(cat /opt/doppel/DEPLOYED_COMMIT); systemctl is-active doppel-tape"
