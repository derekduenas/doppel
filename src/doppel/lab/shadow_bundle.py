"""Assemble promotion evidence for offline approval and subsequent Shadow admission."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys

from ..strategy.genome import Genome, from_dict
from ..strategy.registry import Registry
from ..strategy.shadow_admission import (
    OFFSHORE_ROLES, SIGNATURE_DOMAIN, AdmissionRejected, AdmissionTrust, _canon,
    _decode_evidence, _read, _sha, _verify_approval_metadata, _verified_evidence,
)
from ..strategy.shadow_archetypes import archetype
from ..strategy.shadow_probability import MAX_BYTES as MAX_MODEL_BYTES
from ..twin.cli import code_sha256

COMPUTED_GATES = ("dsr", "pbo", "fdr", "hac", "bootstrap", "promo_edge")


@dataclass(frozen=True)
class ShadowBundle:
    approval: bytes
    files: tuple[tuple[str, bytes], ...]
    public_key: bytes

    @property
    def sha256(self) -> str:
        return _sha(self.approval)

    def write(self, destination: Path, signature: bytes | None = None) -> None:
        """Publish evidence without replacing earlier approval. Write the signature last."""
        if signature is not None:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            try:
                Ed25519PublicKey.from_public_bytes(self.public_key).verify(signature, SIGNATURE_DOMAIN + self.approval)
            except InvalidSignature as exc:
                raise ValueError("signature does not verify for the reconstructed approval") from exc
        destination.mkdir(mode=0o700)
        records = (*self.files, ("approval.json", self.approval))
        if signature is not None:
            records = (*records, ("approval.sig", signature))
        written = []
        try:
            for name, data in records:
                path = destination / name
                with path.open("xb") as output:
                    written.append(path)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            fd = os.open(destination, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            for path in written:
                path.unlink()
            destination.rmdir()
            raise


def _fill_gates(genome, raw_gates, files, evidence):
    for field, value in (("genome_id", genome.id), ("archetype", archetype(genome))):
        if field in raw_gates and raw_gates[field] != value:
            raise ValueError(f"gate results refer to a different {field}")
    gates = dict(raw_gates["gates"])

    def populate(name, row):
        current = gates.setdefault(name, {})
        if not isinstance(current, dict) or any(k in current and current[k] != v for k, v in row.items()):
            raise ValueError(f"gate results contradict supplied evidence: {name}")
        gates[name] = {**row, **current}

    passed, na = "MEASURED_PASS", "NOT_APPLICABLE_ARCHETYPE"
    for name in COMPUTED_GATES:
        populate(name, {"status": passed})
    offshore = {role: _sha(files[role + ".json"]) for role in OFFSHORE_ROLES if role + ".json" in files}
    if offshore:
        populate("offshore_receipts", {"status": passed, "sources_sha256": offshore})
    else:
        populate("offshore_receipts", {"status": na})
    if genome.mode == "taker":
        if "taker-measurements.json" in files:
            name = "taker-measurements.json"
            digest = _sha(files[name])
            for gate, measurement in evidence[name]["measurements"].items():
                populate(gate, {"status": passed, **measurement, "source_sha256": digest})
        if "layer5b.json" in files:
            populate("taker_timing", {"status": passed, "source_sha256": _sha(files["layer5b.json"]),
                                      "trigger_source": genome.evidence["taker_trigger_source"]})
        for name in ("maker_queue_fill", "adverse_selection_stress"):
            populate(name, {"status": na})
    else:
        populate("taker_timing", {"status": na})
        if "maker.json" in files:
            digest = _sha(files["maker.json"])
            for name in ("maker_queue_fill", "adverse_selection_stress"):
                populate(name, {"status": passed, "source_sha256": digest})
            for horizon in ("2s", "10s"):
                measurement = evidence["maker.json"]["markouts"][horizon]
                populate("markout_" + horizon, {"status": passed, **{
                    k: measurement[k] for k in ("value", "observations", "source_sha256")}})
    return {"schema": "doppel.shadow-gates/v2", "genome_id": genome.id, "archetype": archetype(genome), "gates": gates}


def build_shadow_bundle(promotion_dir: Path, replay_artifact: Path, trial_ledger: Path, gate_results: Path, *,
                        genome: Genome, trust: AdmissionTrust, validity_ns: tuple[int, int]) -> ShadowBundle:
    """Verify a promotion report and its measured proofs, then assemble deterministic approval bytes.

    The promotion directory contains promotion.json and the separately pinned plan.json.
    Siblings of gate_results supply maker.json or layer5b.json and any native receipt
    and probability attestations. Trust and validity are provided by the approver.
    """
    paths = {"promotion.json": promotion_dir / "promotion.json", "plan.json": promotion_dir / "plan.json",
             "replay.json": replay_artifact, "trials.json": trial_ledger}
    specific = ("layer5b.json", "taker-measurements.json") if genome.mode == "taker" else ("maker.json",)
    for name in (*specific, "binance.json", "hyperliquid.json", "probability.json"):
        source = gate_results.parent / name
        if source.exists():
            paths[name] = source
    files = {name: _read(path, MAX_MODEL_BYTES) if name == "probability.json" else _read(path)
             for name, path in paths.items()}
    evidence = {name: _decode_evidence(name, raw) for name, raw in files.items()}
    gates = _fill_gates(genome, _decode_evidence("gates.json", _read(gate_results)), files, evidence)
    evidence["gates.json"], files["gates.json"] = gates, _canon(gates)
    replay = evidence["replay.json"]
    ledger = evidence["trials.json"]
    trials = ledger.get("trials")
    if not isinstance(trials, list) or len(trials) < 2 or not all(isinstance(row, dict) for row in trials):
        raise AdmissionRejected("REJECT_INCOMPLETE_LEDGER", "canonical ledger has too few complete trials")
    matches = [row for row in trials if row.get("genome_id") == genome.id
               and ("trial_id" not in replay or row.get("trial_id") == replay["trial_id"])]
    if len(matches) != 1:
        raise AdmissionRejected("REJECT_INCOMPLETE_LEDGER", "ledger does not uniquely identify the candidate trial")
    result = next((row for row in Registry(trust.registry_root).records()
                   if row.get("kind") == "result" and row.get("genome_id") == genome.id and row.get("binding") is True), None)
    if result is None:
        raise AdmissionRejected("REJECT_NONCANONICAL_REPLAY", "missing canonical registry result")
    start, end = validity_ns
    approval = {"schema": "doppel.shadow-approval/v2", "genome_id": genome.id, "archetype": archetype(genome),
                "commit_sha": trust.commit_sha, "scoring_sha256": code_sha256(), "registry_result_sha256": result["hash"],
                "valid_from_ns": start, "valid_until_ns": end, "n_trials": len(trials),
                "candidate_trial_id": matches[0]["trial_id"], "trial_head_sha256": trials[-1]["sha256"],
                "files": {name: _sha(raw) for name, raw in files.items()}}
    _verify_approval_metadata(approval, trust, genome, require_current=False)
    simulation = replay["verdict"]["evaluation"]["simulation"]
    _, _, _, stats = _verified_evidence(genome, trust, approval, evidence, _sha(_canon(simulation)))
    approval["statistics"] = stats
    return ShadowBundle(_canon(approval), tuple(sorted(files.items())), trust.public_key)


def _local_signature(bundle: ShadowBundle, path: Path) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size != 32:
            raise ValueError("signing key must be a root-owned, owner-only raw 32-byte Ed25519 seed")
        key = Ed25519PrivateKey.from_private_bytes(source.read(33))
    if key.public_key().public_bytes_raw() != bundle.public_key:
        raise ValueError("signing key does not match the independently configured public key")
    return key.sign(SIGNATURE_DOMAIN + bundle.approval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doppel promote bundle", description=__doc__)
    for name in ("promotion-dir", "replay-artifact", "trial-ledger", "gate-results", "genome", "registry-root", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("commit-sha", "plan-sha256", "public-key-hex"):
        parser.add_argument("--" + name, required=True)
    for name in ("valid-from-ns", "valid-until-ns"):
        parser.add_argument("--" + name, type=int, required=True)
    parser.add_argument("--active-genome", action="append", default=[])
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument("--signing-key", type=Path, help="root-owned raw Ed25519 seed in an owner-only file")
    signing.add_argument("--signature", type=Path, help="offline binary signature for the deterministically rebuilt approval")
    args = parser.parse_args(argv)
    try:
        key = bytes.fromhex(args.public_key_hex)
        if len(key) != 32:
            raise ValueError("expected a 32-byte operator public key")
        import orjson
        genome = from_dict(orjson.loads(_read(args.genome)))
        trust = AdmissionTrust(key, args.registry_root, args.commit_sha, args.plan_sha256, tuple(args.active_genome))
        bundle = build_shadow_bundle(args.promotion_dir, args.replay_artifact, args.trial_ledger, args.gate_results,
                                     genome=genome, trust=trust, validity_ns=(args.valid_from_ns, args.valid_until_ns))
        signature = None
        if args.signing_key:
            signature = _local_signature(bundle, args.signing_key)
        elif args.signature:
            signature = _read(args.signature, 64)
        bundle.write(args.out, signature)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 2
    print(bundle.sha256)
    return 0
