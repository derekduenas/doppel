"""doppel-brti-relay: copy PERELMAN's raw BRTI stream lines into a directory paper can read.

PERELMAN's stream directory is private to its DynamicUser (0700/0600). The relay runs with only
CAP_DAC_READ_SEARCH and sees nothing else under /var/lib/private (ops/systemd/doppel-brti-relay.service).

    doppel-brti-relay --src /var/lib/private/perelman/stream --out /mnt/.../doppel/perelman-relay

Every ``interval`` (<= 100 ms) it reads the new COMPLETE lines of each ``*.jsonl`` in ``src`` (the
hour file being appended; a new hour file is picked up when it appears) and appends each to the
file of the same name in ``out``, as the same JSON object with one added field ``relay_tw`` (ns,
wall clock at copy, strictly increasing). A partial last line waits until its newline arrives.

Exactly-once: the output file is the record of progress. Its k-th complete line is the source's
k-th complete line, so on start the relay counts the complete lines of each output file (cutting a
torn last line from a crash) and resumes the source after that many lines. ``relay-state.json``
mirrors the offsets after every batch for inspection; it is never needed for correctness. A batch is
one ``write`` and an ``fsync`` per file. Nothing else is read or written; no network.

Limit: rotated ``*.jsonl.gz`` files are not read. If the relay is down across PERELMAN's hourly
rotation, that hour's tail is missing from the relay (paper then has only ``ref_brti_live``).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import orjson

STATE = "relay-state.json"


class RelayError(ValueError):
    pass


def _complete_lines(data: bytes) -> list[bytes]:
    cut = data.rfind(b"\n") + 1
    return data[:cut].splitlines(keepends=True)


class Relay:
    def __init__(self, src: Path, out: Path, clock=time.time_ns):
        self.src, self.out, self.clock = Path(src), Path(out), clock
        self.out.mkdir(parents=True, exist_ok=True)
        self.offsets: dict[str, int] = {}
        self.last_tw = 0
        self._resume()

    def _resume(self) -> None:
        """Offsets from the output itself (k output lines = k source lines)."""
        for o in sorted(self.out.glob("*.jsonl")):
            data = o.read_bytes()
            lines = _complete_lines(data)
            keep = sum(len(x) for x in lines)
            if keep != len(data):                       # torn last line of a crash: never complete
                with open(o, "r+b") as fh:
                    fh.truncate(keep)
                    fh.flush()
                    os.fsync(fh.fileno())
            for x in lines:
                self.last_tw = max(self.last_tw, int(orjson.loads(x).get("relay_tw", 0)))
            src = self.src / o.name
            if not src.exists():
                self.offsets[o.name] = -1               # source rotated away: this hour is closed
                continue
            off = 0
            with open(src, "rb") as fh:
                for k in range(len(lines)):
                    line = fh.readline()
                    if not line.endswith(b"\n"):
                        raise RelayError(f"{o.name}: output has more lines than the source")
                    off += len(line)
            self.offsets[o.name] = off

    def step(self) -> int:
        """Copy every new complete line once. Returns the number of lines copied."""
        n = 0
        for src in sorted(self.src.glob("*.jsonl")):
            name = src.name
            start = self.offsets.get(name, 0)
            if start < 0:
                continue
            with open(src, "rb") as fh:
                fh.seek(start)
                lines = _complete_lines(fh.read())
            if not lines:
                continue
            buf = bytearray()
            for line in lines:
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError as exc:
                    raise RelayError(f"malformed upstream line in {name}") from exc
                if not isinstance(rec, dict):
                    raise RelayError(f"upstream line in {name} is not an object")
                self.last_tw = max(self.clock(), self.last_tw + 1)
                buf += orjson.dumps({**rec, "relay_tw": self.last_tw}) + b"\n"
            with open(self.out / name, "ab") as fh:
                fh.write(bytes(buf))
                fh.flush()
                os.fsync(fh.fileno())
            self.offsets[name] = start + sum(len(x) for x in lines)
            n += len(lines)
        if n:
            tmp = self.out / (STATE + ".tmp")
            tmp.write_bytes(orjson.dumps({"offsets": self.offsets, "relay_tw": self.last_tw}))
            os.replace(tmp, self.out / STATE)
        return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-brti-relay")
    ap.add_argument("--src", type=Path, required=True, help="PERELMAN stream directory (read only)")
    ap.add_argument("--out", type=Path, required=True, help="relay output directory")
    ap.add_argument("--interval", type=float, default=0.1, help="seconds between polls (<= 0.1)")
    a = ap.parse_args(argv)
    if not 0 < a.interval <= 0.1:
        print("--interval must be in (0, 0.1]", file=sys.stderr)
        return 2
    relay = Relay(a.src, a.out)
    stop = {"x": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("x", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("x", True))
    while not stop["x"]:
        relay.step()
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
