"""Check answer-key inputs before simulating scripted wallets on dense tape."""

import argparse
import hashlib
from pathlib import Path

import orjson

from ..tape.segment import read_segment, segment_path
from ..tape.universe import ASSETS, HORIZONS, intl_slug
from ..twin.tape import NS, window_list
from .flow import _ts
from .ledger import TokenMap


def _digest(path: Path) -> str:
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def _book_assets(msg: dict):
    if msg.get("event_type") == "book":
        yield msg.get("asset_id")
    elif msg.get("event_type") == "price_change":
        for change in msg.get("price_changes", []):
            yield change.get("asset_id")


def preflight_inputs(data: Path, tokens: Path, stream: str, t_from: int, split: int, t_to: int) -> dict:
    """Require resolved complementary pairs and in-window book evidence in both halves.

    The certificate covers quoted token identities, sealed bytes and receipt-time window
    membership. Continuous books, actual subject fills and fidelity remain downstream checks.
    """
    parts = stream.split("_")
    if len(parts) != 3 or parts[0] != "clob" or parts[1] not in ASSETS or parts[2] not in HORIZONS:
        raise ValueError(f"unsupported answer-key book stream {stream!r}")
    _, asset, hz = parts
    length = HORIZONS[hz]
    if not t_from < split < t_to:
        raise ValueError("answer-key train and holdout must be nonempty and ordered")
    halves = {name: [s for s, _, _ in window_list([asset], [hz], lo, hi)]
              for name, lo, hi in [("train", t_from, split), ("holdout", split, t_to)]}
    if not all(halves.values()):
        raise ValueError("train and holdout must each contain a complete market window")
    keys = {intl_slug(asset, hz, s): s for starts in halves.values() for s in starts}
    pairs: dict[int, dict[int, dict]] = {s: {} for s in keys.values()}
    token_sha = _digest(tokens)
    for row in TokenMap(tokens).m.values():
        start = keys.get(row.get("slug", ""))
        if start is None:
            continue
        idx = row.get("idx")
        if type(idx) is not int or idx not in (0, 1) or idx in pairs[start]:
            raise ValueError(f"ambiguous token side for {row['slug']}")
        pairs[start][idx] = row
    mapped = {s for s, pair in pairs.items() if len(pair) == 2}
    resolved = set()
    for s in mapped:
        up, down = pairs[s][0], pairs[s][1]
        payouts = [up.get("payout"), down.get("payout")]
        if (up.get("condition") and up["condition"] == down.get("condition")
                and up.get("status") == down.get("status") == "resolved"
                and all(type(v) in (int, float) and v in (0, 1) for v in payouts)
                and payouts[0] != payouts[1]):
            resolved.add(s)
    report = {name: {"expected": len(starts), "mapped": len(mapped.intersection(starts)),
                     "resolved": len(resolved.intersection(starts))} for name, starts in halves.items()}
    if len(resolved) != len(pairs):
        raise ValueError(f"unusable answer-key token pairs: {report}; "
                         f"first missing or unresolved slug {intl_slug(asset, hz, min(pairs.keys() - resolved))}")

    manifest = data / stream / "manifest.jsonl"
    seals = {r["file"]: r for line in manifest.read_bytes().splitlines() if line and (r := orjson.loads(line))}
    # Keep each side pending until the real book shows its token AND matching condition in its own window.
    pending = {s: {r["token"] for r in pair.values()} for s, pair in pairs.items()}
    segments = {}
    for h in range(t_from // 3600 * 3600, t_to, 3600):
        path = segment_path(data, stream, h * NS)
        rel = path.relative_to(data / stream).as_posix()
        seal = seals.get(rel)
        if seal is None or not path.is_file():
            raise ValueError(f"missing sealed answer-key segment {stream}/{rel}")
        before = path.stat()
        if before.st_size != seal["bytes"] or _digest(path) != seal["sha256"]:
            raise ValueError(f"answer-key book differs from manifest: {stream}/{rel}")
        for rec in read_segment(path, verify=True, strict=True):
            start = rec["tw"] // (NS * length) * length
            missing = pending.get(start)
            if not missing or (rec.get("meta") or {}).get("ctl") or rec["raw"][:1] not in ("[", "{"):
                continue
            msgs = orjson.loads(rec["raw"])
            for msg in msgs if isinstance(msgs, list) else [msgs]:
                if isinstance(msg, dict) and msg.get("market") == pairs[start][0]["condition"]:
                    missing.difference_update(_book_assets(msg))
        after = path.stat()
        if (before.st_ino, before.st_ctime_ns) != (after.st_ino, after.st_ctime_ns):
            raise ValueError(f"answer-key book changed while checking {stream}/{rel}")
        segments[rel] = seal["sha256"]
    complete = {s for s, remaining in pending.items() if not remaining}
    for name, starts in halves.items():
        report[name]["book_pairs"] = len(complete.intersection(starts))
    if len(complete) != len(pairs):
        raise ValueError(f"answer-key book evidence missing: {report}; "
                         f"first incomplete slug {intl_slug(asset, hz, min(pairs.keys() - complete))}")
    if _digest(tokens) != token_sha:
        raise ValueError("answer-key token map changed during preflight")
    return {"range": [t_from, split, t_to], "stream": stream, **report,
            "tokens_sha256": token_sha, "segment_sha256": segments}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--tokens", type=Path, required=True)
    ap.add_argument("--stream", default="clob_btc_5m")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True)
    ap.add_argument("--split", type=_ts, required=True)
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    args = ap.parse_args()
    print(orjson.dumps(preflight_inputs(**vars(args)), option=orjson.OPT_INDENT_2).decode())
