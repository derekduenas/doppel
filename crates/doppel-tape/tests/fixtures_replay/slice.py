"""Copy the head of each spot_l2_* stream (records with tw < cutoff), lines byte-identical, into
one zstd frame per segment at level 19. Usage: slice_fixture.py SRC DST CUTOFF_NS"""
import sys
from pathlib import Path

import orjson
import zstandard

src, dst, cutoff = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
for seg in sorted(src.glob("spot_l2_*/*/*.dtape.zst")):
    data = zstandard.ZstdDecompressor().stream_reader(seg.open("rb")).read()
    keep = [ln for ln in data.split(b"\n") if ln and orjson.loads(ln)["tw"] < cutoff]
    out = dst / seg.relative_to(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(zstandard.ZstdCompressor(level=19).compress(b"\n".join(keep) + b"\n"))
    print(out.relative_to(dst), len(keep), out.stat().st_size)
