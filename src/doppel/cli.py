"""DOPPEL commands exposed under the shared executable."""

import sys

from .lab import promotion


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "promote":
        return promotion.main(args[1:])
    print("usage: doppel promote bundle [options]", file=sys.stdout if args in ([], ["-h"], ["--help"]) else sys.stderr)
    return 0 if args in ([], ["-h"], ["--help"]) else 2
