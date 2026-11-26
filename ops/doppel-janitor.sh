#!/bin/sh
# Preserve the systemd entry point for the tape-volume free-space janitor.
set -eu
exec python3 "$(dirname "$0")/tape_janitor.py" "${1:?tape root}" "${2:-12}"
