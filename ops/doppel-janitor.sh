#!/bin/sh
# Keeps the tape volume from filling: while free space is below FLOOR_GB, delete the oldest closed
# UTC day of the HIGH-VOLUME live streams only (clob_*, rtds_*, spot_*, pmus_books, pmus_ws, kalshi_books).
# Never touches paid or irreplaceable streams (goldsky_activity, ref_*). Never deletes today.
# Every deletion is logged. Stopgap until segments ship to object storage.
set -eu
ROOT="${1:?tape root}"
FLOOR_GB="${2:-12}"
LOG="$ROOT/janitor.log"
TODAY=$(date -u +%Y%m%d)
STREAMS=$(find "$ROOT" -mindepth 1 -maxdepth 1 -type d \( -name 'clob_*' -o -name 'rtds_*' -o -name 'spot_*' -o -name pmus_books -o -name pmus_ws -o -name kalshi_books \))
while :; do
  FREE_GB=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc 0-9)
  [ "$FREE_GB" -ge "$FLOOR_GB" ] && exit 0
  OLDEST=$(for s in $STREAMS; do find "$s" -mindepth 1 -maxdepth 1 -type d -regex '.*/[0-9]\{8\}' -printf '%f\n'; done | sort -u | head -1)
  if [ -z "$OLDEST" ] || [ "$OLDEST" = "$TODAY" ]; then
    echo "$(date -u +%FT%TZ) free=${FREE_GB}G below floor; nothing deletable" >> "$LOG"; exit 1
  fi
  echo "$(date -u +%FT%TZ) free=${FREE_GB}G < ${FLOOR_GB}G: deleting live-stream day $OLDEST" >> "$LOG"
  for s in $STREAMS; do rm -rf "$s/$OLDEST"; done
done
