#!/bin/sh
# Called by deploy.sh over SSH with a complete git archive on stdin.
set -eu
root=$1
revision=$2
mode=$3
mirror=$4
live=$root/doppel
stage=$root/doppel.new
old=$root/doppel.old

exec 9>"$root/.doppel-release.lock"
flock -x 9
[ -d "$live" ]
rm -rf "$stage"
mkdir "$stage"
tar -xf - -C "$stage"
printf '%s\n' "$revision" > "$stage/DEPLOYED_COMMIT"
python3 -m venv "$stage/.venv"
"$stage/.venv/bin/pip" install -q -r "$stage/ops/requirements.lock"
"$stage/.venv/bin/pip" install -q --no-deps "$stage"
chmod 755 "$stage"/ops/*.sh
"$stage/.venv/bin/doppel-tape" --help >/dev/null

# Console launchers and activation scripts contain the absolute venv path. Relocate regular
# files before publication. Interpreter symlinks can point outside the release.
python3 - "$stage" "$live" <<'PY'
import os
import sys
from pathlib import Path

stage = Path(sys.argv[1])
old, new = map(os.fsencode, sys.argv[1:])
venv = stage / ".venv"
for path in [venv / "pyvenv.cfg", *(venv / "bin").iterdir()]:
    if not path.is_symlink() and path.is_file():
        raw = path.read_bytes()
        if old in raw:
            path.write_bytes(raw.replace(old, new))
PY

# Linux/GNU mv cannot atomically replace a nonempty directory. Exchange both names in one
# filesystem operation so that even a killed deploy leaves the installed path usable.
exchange() {
    python3 - "$stage" "$live" <<'PY'
import ctypes
import os
import sys

src, dst = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
if sys.platform == "darwin":
    rc = libc.renamex_np(src, dst, 2)  # RENAME_SWAP
else:
    rc = libc.renameat2(-100, src, -100, dst, 2)  # AT_FDCWD, RENAME_EXCHANGE
if rc:
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err))
PY
}

[ "$(git --git-dir="$mirror" rev-parse refs/heads/main)" = "$revision" ] || {
    echo "Apex main moved during release preparation" >&2
    exit 1
}
exchange
if "$live/.venv/bin/doppel-tape" --help >/dev/null &&
   { [ "$mode" = keep ] || systemctl restart doppel-tape; } &&
   systemctl is-active doppel-tape; then
    rm -rf "$old"
    mv "$stage" "$old"
    printf 'deployed %s\n' "$revision"
else
    exchange
    if [ "$mode" = restart ]; then systemctl restart doppel-tape; fi
    echo "release validation failed; restored previous release" >&2
    exit 1
fi
