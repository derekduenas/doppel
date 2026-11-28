"""Bind the paper runtime to the lead's separate shadow approval configuration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from ..strategy.genome import Genome
from ..strategy.shadow_admission import AdmissionRejected, AdmissionTrust, admit_shadow, simulation_digest
from ..twin.engine import SimConfig

APPROVAL = Path("/etc/doppel-shadow/approval.json")
RELEASE_ROOT = Path("/opt/doppel")
_REQUIRED = {"schema", "public_key_hex", "plan_sha256", "active_genomes", "data_root",
             "journal_root", "reference_root"}


def _owner_file(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or st.st_size > 32768:
            raise ValueError("shadow approval files must be small root-owned regular files")
        data = handle.read(32769)
        if len(data) > 32768:
            raise ValueError("shadow approval file grew past the size limit")
        return data


@dataclass(frozen=True)
class ShadowSession:
    receipt_sha256: str
    valid_until_ns: int
    approval_sha256: str

    def journal_binding(self) -> dict:
        return {"state": "SHADOW_ACTIVE", "receipt_sha256": self.receipt_sha256,
                "valid_until_ns": self.valid_until_ns, "approval_sha256": self.approval_sha256}

    def stop_reason(self, now_ns: int) -> str | None:
        if now_ns >= self.valid_until_ns:
            return "shadow_admission_expired"
        try:
            current = hashlib.sha256(_owner_file(APPROVAL)).hexdigest()
        except (OSError, ValueError):
            return "shadow_approval_unavailable"
        return "shadow_approval_changed" if current != self.approval_sha256 else None


def authorize(genome: Genome, bundle: Path, tape: Path, journal: Path,
              reference: str | None, cfg: SimConfig) -> ShadowSession:
    try:
        raw = _owner_file(APPROVAL)
        doc = json.loads(raw)
        if set(doc) != _REQUIRED or doc["schema"] != "doppel-shadow-operator/1":
            raise ValueError("unexpected shadow operator configuration")
        tape_root = Path(doc["data_root"])
        journal_root = Path(doc["journal_root"])
        approved_reference = None if doc["reference_root"] is None else Path(doc["reference_root"])
        roots = [tape_root, journal_root] + ([approved_reference] if approved_reference is not None else [])
        if any(not p.is_absolute() for p in roots):
            raise ValueError("shadow source and journal roots must be absolute")
        actual_reference = None if reference is None else Path(reference).resolve()
        if (tape.resolve() != tape_root.resolve() or journal.resolve() != journal_root.resolve() / genome.id
                or actual_reference != (None if approved_reference is None else approved_reference.resolve())):
            raise ValueError("shadow tape, reference or journal is outside the approved scope")
        active = doc["active_genomes"]
        if not isinstance(active, list) or not all(type(k) is str for k in active):
            raise ValueError("invalid operator active set")
        trust = AdmissionTrust(
            public_key=bytes.fromhex(doc["public_key_hex"]),
            registry_root=RELEASE_ROOT,
            commit_sha=_owner_file(RELEASE_ROOT / "DEPLOYED_COMMIT").decode().strip(),
            plan_sha256=doc["plan_sha256"],
            active_genomes=tuple(active),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AdmissionRejected("REJECT_UNTRUSTED_OPERATOR", "shadow configuration is unavailable or invalid") from exc
    receipt = admit_shadow(genome, bundle, trust, journal / "admissions",
                           simulation_sha256=simulation_digest(cfg))
    return ShadowSession(receipt["receipt_sha256"], receipt["valid_until_ns"], hashlib.sha256(raw).hexdigest())
