import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


@pytest.mark.parametrize("failure", ["", "install", "live-smoke", "moved-main", "restart"])
def test_release_preserves_installed_tree_and_propagates_errors(tmp_path, failure):
    root, commands = tmp_path / "opt", tmp_path / "bin"
    live = root / "doppel"
    live.mkdir(parents=True)
    (live / "DEPLOYED_COMMIT").write_text("original")
    commands.mkdir()

    def executable(name, body):
        path = commands / name
        path.write_text(body)
        path.chmod(0o755)

    executable("flock", f"#!{sys.executable}\nimport fcntl\nfcntl.flock(9, fcntl.LOCK_EX)\n")
    executable("git", '#!/bin/sh\nif [ "$FAIL" = moved-main ]; then echo newer; else echo candidate; fi\n')
    executable("systemctl", '''#!/bin/sh
echo "$*" >> "$ROOT/services"
if [ "$FAIL" = restart ] && [ "$1" = restart ]; then
    [ "$(cat "$ROOT/doppel/DEPLOYED_COMMIT")" = original ]
fi
''')
    executable("python3", '''#!/bin/sh
set -eu
if [ "$1" != -m ]; then exec "$REAL_PYTHON" "$@"; fi
venv=$3
mkdir -p "$venv/bin"
printf '%s\n' "$venv" > "$venv/pyvenv.cfg"
ln -s "$REAL_PYTHON" "$venv/bin/python"
cat > "$venv/bin/pip" <<'MOCK'
#!/bin/sh
[ "$FAIL" != install ]
MOCK
printf '#!%s/bin/python\n' "$venv" > "$venv/bin/doppel-tape"
cat >> "$venv/bin/doppel-tape" <<'MOCK'
import os
import sys
from pathlib import Path
if os.environ["FAIL"] == "live-smoke" and Path(sys.argv[0]).parents[2].name == "doppel":
    sys.exit(1)
MOCK
chmod 755 "$venv/bin/pip" "$venv/bin/doppel-tape"
''')
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("ops/requirements.lock"))
        tar.addfile(tarfile.TarInfo("ops/deploy.sh"))
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "FAIL": failure, "REAL_PYTHON": sys.executable, "ROOT": str(root)}
    script = Path(__file__).resolve().parents[1] / "ops/install-release.sh"
    mode = "restart" if failure == "restart" else "keep"
    run = subprocess.run(["sh", str(script), str(root), "candidate", mode, str(tmp_path / "mirror")],
                         env=env, input=packed.getvalue(), capture_output=True)
    assert (run.returncode == 0) == (failure == ""), run.stderr.decode()
    assert (live / "DEPLOYED_COMMIT").read_text().strip() == ("original" if failure else "candidate")
    if not failure:
        assert (root / "doppel.old/DEPLOYED_COMMIT").read_text() == "original"
        assert (root / "services").read_text() == "is-active doppel-tape\n"
        assert (live / ".venv/pyvenv.cfg").read_text().strip() == str(live / ".venv")
    elif failure == "restart":
        assert (root / "services").read_text().splitlines() == ["restart doppel-tape"] * 2
    else:
        assert not (root / "services").exists()
