"""Run one deterministic file shard with isolated fixture paths."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def test_files() -> list[Path]:
    return sorted(set(Path("tests").rglob("test_*.py")) | set(Path("tests").rglob("*_test.py")))


def shard(files: list[Path], index: int, count: int) -> list[str]:
    # The answer key and fitter suites run complete replay fixtures. Separating
    # them avoids leaving one runner with both of the longest test modules.
    replay = {
        "test_answerkey.py",
        "test_rulefit.py",
        "test_simulator.py",
        "test_reconstruct.py",
        "test_fidelity.py",
    }
    groups: list[list[str]] = [[] for _ in range(count)]
    for number, path in enumerate(sorted(files, key=lambda p: (p.name not in replay, p))):
        groups[number % count].append(str(path))
    return groups[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.index < args.count:
        parser.error("index must be in [0, count)")
    selected = shard(test_files(), args.index, args.count)
    if not selected:
        parser.error("shard has no tests")
    args.work.mkdir(parents=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--durations=10", "--junitxml", str(args.report),
        "--basetemp", str(args.work / "pytest"), *selected,
    ], env={**os.environ, "TMPDIR": str(args.work)})


if __name__ == "__main__":
    raise SystemExit(main())
