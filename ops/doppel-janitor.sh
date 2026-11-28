#!/bin/sh
# The legacy unit can report disk pressure while stopping short of unverified deletion.
set -eu
exec python3 "$(dirname "$0")/tape_janitor.py" "${1:?tape root}" "${2:-12}"
