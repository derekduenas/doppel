#!/bin/sh
# Keep the existing protected-day local policy until cold retention is enabled.
set -eu
exec python3 "$(dirname "$0")/tape_janitor.py" --local-floor "${1:?tape root}" "${2:-12}"
