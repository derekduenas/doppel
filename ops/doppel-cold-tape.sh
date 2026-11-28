#!/bin/sh
# A single writer processes the primary and any staged Rust root in sequence.
set -eu
base=$(dirname "$0")
mode=$1
state=/var/lib/doppel-cold
run() {
  root=$1
  remote=$2
  case "$mode" in
    archive) /usr/bin/python3 "$base/archive_tape.py" --source "$root" --remote "$remote" --state "$state" --streams auto ;;
    prune) /usr/bin/python3 "$base/tape_janitor.py" "$root" 12 --remote "$remote" --state "$state" --retain-days "$DOPPEL_RETAIN_DAYS" ;;
    *) return 2 ;;
  esac
}
if [ -n "${DOPPEL_LEAD_ROOT:-}" ]; then
  : "${DOPPEL_LEAD_ARCHIVE_REMOTE:?A separate remote prefix is required for the Rust root}"
  [ "$DOPPEL_LEAD_ARCHIVE_REMOTE" != "$DOPPEL_ARCHIVE_REMOTE" ] || exit 2
fi
rc=0
run "$DOPPEL_TAPE_ROOT" "$DOPPEL_ARCHIVE_REMOTE" || rc=$?
if [ -n "${DOPPEL_LEAD_ROOT:-}" ]; then
  run "$DOPPEL_LEAD_ROOT" "$DOPPEL_LEAD_ARCHIVE_REMOTE" || rc=$?
fi
exit "$rc"
