"""DOPPEL commands exposed under the shared executable."""

import sys

from .lab import promotion


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:2] == ["promote", "replay-export"]:
        from .lab.shadow_replay_export import main as export_main

        return export_main(args[2:])
    if args and args[0] == "promote":
        return promotion.main(args[1:])
    print("usage: doppel promote {bundle,replay-export} [options]",
          file=sys.stdout if args in ([], ["-h"], ["--help"]) else sys.stderr)
    return 0 if args in ([], ["-h"], ["--help"]) else 2
