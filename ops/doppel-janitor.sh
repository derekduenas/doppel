#!/bin/sh
# Keeps the tape volume from filling: while free space is below FLOOR_GB, delete the oldest
# closed UTC day across all streams (never today). Every deletion is logged. This is a stopgap
# until segments ship to object storage; deleted days are gone.
set -eu
ROOT="${1:?tape root}"
FLOOR_GB="${2:-12}"
LOG="$ROOT/janitor.log"
TODAY=$(date -u +%Y%m%d)
while :; do
  FREE_GB=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc 0-9)
  [ "$FREE_GB" -ge "$FLOOR_GB" ] && exit 0
  OLDEST=$(find "$ROOT" -mindepth 2 -maxdepth 2 -type d -regex '.*/[0-9]\{8\}' -printf '%f\n' | sort -u | head -1)
  if [ -z "$OLDEST" ] || [ "$OLDEST" = "$TODAY" ]; then
    echo "$(date -u +%FT%TZ) free=${FREE_GB}G below floor but only today's data remains" >> "$LOG"; exit 1
  fi
  echo "$(date -u +%FT%TZ) free=${FREE_GB}G < ${FLOOR_GB}G: deleting day $OLDEST" >> "$LOG"
  find "$ROOT" -mindepth 2 -maxdepth 2 -type d -name "$OLDEST" -exec rm -rf {} +
done
