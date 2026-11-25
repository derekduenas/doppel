# ADR-0001: M0 recorders in Python asyncio; hot plane stays Rust

**Status:** accepted 2026-11-25

**Context:** The blueprint puts C1 Tape in Rust. The dev Mac has no Rust toolchain and 8.9 GB free.
Order books cannot be backfilled, so recording time is the scarce resource.

**Decision:** Ship M0 recorders as Python asyncio (websockets, aiohttp, orjson, zstd). The tape format
(zstd frames of JSON records with seq, two receive clocks, CRC32 over the raw payload, a hashed
manifest, and explicit control events) is the contract. The Rust hot plane (C3L/C6/C9/C10, M2+) reads
the same format. A Rust recorder can replace this one later without a format change.

**Evidence:** 120 s live run with 15 streams at ~2k msgs/s, no slow-consumer disconnects after sharding
the CLOB, 0.5 core, 76 MB RSS. Receive clocks carry asyncio scheduling jitter (ms-level). That is
acceptable for research labels; it is not acceptable for the hot plane.
