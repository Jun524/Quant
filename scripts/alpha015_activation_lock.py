#!/usr/bin/env python3
"""Create Alpha 015's write-once prior-day membership lock when eligible."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document, protocol_digests
from quant.research.alpha015_activation_lock import build_alpha015_activation_lock
from quant.research.candidate_activation import verify_methodology_artifacts


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    base_path = PROJECT_ROOT / "research" / "alpha015-activation-policy.json"
    amendment_path = PROJECT_ROOT / "research" / "alpha015-activation-amendment-001.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
    methodology = json.loads(
        (PROJECT_ROOT / "research" / "alpha015-methodology-audit.json").read_text(encoding="utf-8")
    )
    methodology = verify_methodology_artifacts(methodology, project_root=PROJECT_ROOT)
    if methodology["artifact_integrity"]["status"] != "PASS":
        raise SystemExit("Alpha 015 methodology artifact integrity failed")
    expected_base_digest = str((amendment.get("base_policy") or {}).get("sha256") or "")
    if sha256(base_path.read_bytes()).hexdigest() != expected_base_digest:
        raise SystemExit("Alpha 015 base activation policy differs from its amendment binding")

    universe_policy = policy_document()
    protocol = protocol_digests()
    coverage = assess_forward_universe_history(
        PROJECT_ROOT / "research" / "universe_snapshots",
        expected_protocol=protocol,
        policy=universe_policy,
    )
    required = int((base.get("activation_requirements") or {})["point_in_time_universe_consecutive_days"])
    ready = coverage["status"] == "READY" and int(coverage["consecutive_days_ending_latest"]) >= required
    receipt_path = PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
    if ready:
        receipt = build_alpha015_activation_lock(
            snapshot_directory=PROJECT_ROOT / "research" / "universe_snapshots",
            universe_policy=universe_policy,
            expected_protocol=protocol,
            candidate_policy=base,
            amendment=amendment,
        )
        encoded = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
        if receipt_path.exists():
            if receipt_path.read_bytes() != encoded:
                raise SystemExit("existing Alpha 015 activation lock differs from source replay")
        else:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        status = {
            "schema_version": 1,
            "status": "LOCKED_FOR_PREREGISTRATION_ONLY",
            "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
            "lock_sha256": receipt["lock_sha256"],
            "return_data_authorized": False,
            "live_order_authorization": "CLOSED",
        }
    else:
        if receipt_path.exists():
            raise SystemExit("activation lock exists before source readiness")
        status = {
            "schema_version": 1,
            "status": "WAITING_POINT_IN_TIME_UNIVERSE",
            "current_consecutive_universe_days": int(coverage["consecutive_days_ending_latest"]),
            "required_consecutive_universe_days": required,
            "days_remaining": max(0, required - int(coverage["consecutive_days_ending_latest"])),
            "receipt": None,
            "return_data_authorized": False,
            "live_order_authorization": "CLOSED",
        }
    output = PROJECT_ROOT / "reports" / "alpha015-activation-lock-status.json"
    atomic_write(output, (json.dumps(status, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
