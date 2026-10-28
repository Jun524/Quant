#!/usr/bin/env python3
"""Plan or acquire publisher-verified Alpha 016 aggregate-trade archives."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.spot_perp_flow_data import (
    MAX_COMPRESSED_BYTES,
    SpotPerpFlowDataError,
    archive_spec,
    inspect_archive,
    parse_checksum,
    verify_adjacent_day_chains,
)
from quant.research.spot_perp_flow_protocol import (
    SpotPerpFlowProtocolError,
    build_phase_plan,
)


def _fetch_small(url: str, *, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-alpha016-acquisition/1"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(4097)
    if len(payload) > 4096:
        raise SpotPerpFlowDataError("publisher checksum response is unexpectedly large")
    return payload


def _download(url: str, destination: Path, *, timeout: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            request = Request(url, headers={"User-Agent": "quant-alpha016-acquisition/1"})
            with urlopen(request, timeout=timeout) as response:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            if temporary.stat().st_size <= 0 or temporary.stat().st_size > MAX_COMPRESSED_BYTES:
                raise SpotPerpFlowDataError("downloaded archive has invalid size")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _targets(plan: dict) -> list[dict]:
    result = []
    for market in plan["calendar"]["source_markets"]:
        for label in plan["calendar"]["source_days"]:
            spec = archive_spec(market=market, symbol="BTCUSDT", day=date.fromisoformat(label))
            result.append({**spec, "checksum_url": f"{spec['url']}.CHECKSUM"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("development", "locked_oos"), default="development")
    parser.add_argument("--download", action="store_true", help="download and seal source archives after authorization")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    preregistration = json.loads((PROJECT_ROOT / "research/alpha016-preregistration.json").read_text(encoding="utf-8"))
    development_report = None
    if args.phase == "locked_oos":
        report_path = PROJECT_ROOT / "reports/alpha016-development.json"
        if not report_path.is_file():
            raise SystemExit("locked OOS remains unopened: development report is absent")
        development_report = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        plan = build_phase_plan(preregistration, phase=args.phase, development_report=development_report)
    except SpotPerpFlowProtocolError as error:
        raise SystemExit(str(error)) from error
    targets = _targets(plan)
    if not args.download:
        print(json.dumps({**plan, "targets": targets, "downloaded": False}, indent=2))
        return

    root = PROJECT_ROOT / "research" / "alpha016" / "raw" / args.phase
    verified = []
    for target in targets:
        checksum_payload = _fetch_small(target["checksum_url"], timeout=args.timeout)
        expected = parse_checksum(checksum_payload, expected_name=target["archive_name"])
        directory = root / target["market"] / target["date"]
        archive_path = directory / target["archive_name"]
        checksum_path = directory / f"{target['archive_name']}.CHECKSUM"
        if checksum_path.exists() and checksum_path.read_bytes() != checksum_payload:
            raise SystemExit(f"publisher checksum changed after local sealing: {target['market']} {target['date']}")
        if not archive_path.exists():
            _download(target["url"], archive_path, timeout=args.timeout)
        inspected = inspect_archive(
            archive_path,
            market=target["market"],
            symbol="BTCUSDT",
            day=date.fromisoformat(target["date"]),
            expected_digest=expected,
        )
        if not checksum_path.exists():
            _atomic_bytes(checksum_path, checksum_payload)
        verified.append({**inspected, "checksum": str(checksum_path), "source_url": target["url"]})
        print(f"verified {target['market']} {target['date']} rows={inspected['rows']}", flush=True)
    chain = verify_adjacent_day_chains(verified)
    manifest = {
        "schema_version": 1,
        "candidate": plan["candidate"],
        "phase": args.phase,
        "status": "PUBLISHER_VERIFIED_SOURCE_BYTES_SEALED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": plan["plan_sha256"],
        "chain": chain,
        "files": verified,
        "orders_submitted": False,
    }
    _atomic_bytes(root / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    print(json.dumps({"status": manifest["status"], "manifest": str(root / "manifest.json"), "files": len(verified)}, indent=2))


if __name__ == "__main__":
    main()
