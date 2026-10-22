#!/usr/bin/env python3
"""Prepare and incrementally seal authorized Alpha 015 development inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_acquisition import (
    Alpha015AcquisitionError,
    assess_storage_budget,
    build_phase_acquisition_plan,
    build_development_acquisition_plan,
    build_progress_receipt,
    build_size_inventory_receipt,
    validate_phase_acquisition_plan,
    validate_development_acquisition_plan,
    verify_progress_receipt,
    verify_size_inventory_receipt,
)
from quant.research.alpha015_data_guard import (
    MAX_COMPRESSED_BYTES,
    Alpha015DataError,
    archive_spec,
    download_and_seal_funding,
    seal_downloaded_archive_file,
    validate_phase_archive_bundle,
    verify_publisher_archive_file,
)
from scripts.alpha015_readiness import build_report
from scripts.open_alpha015_validation import validate_validation_unlock


DATA_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / "development"
PLAN_PATH = DATA_ROOT / "acquisition-plan.json"
SIZE_PATH = DATA_ROOT / "size-inventory.json"
PROGRESS_PATH = DATA_ROOT / "progress.json"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
LOCK_PATH = DATA_ROOT / ".acquisition.lock"


class Alpha015NetworkError(RuntimeError):
    pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_once_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise Alpha015AcquisitionError(
                f"existing write-once artifact differs: {path.name}"
            )
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _request(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 30.0,
    attempts: int = 4,
):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                url,
                method=method,
                headers={"User-Agent": "quant-alpha015-source-client/1"},
            )
            return urlopen(request, timeout=timeout)
        except HTTPError as error:
            if error.code in {418, 429}:
                raise Alpha015NetworkError(
                    f"publisher returned HTTP {error.code}; retry is disabled"
                ) from error
            last_error = error
            if error.code not in {500, 502, 503, 504} or attempt + 1 == attempts:
                break
        except (URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 == attempts:
                break
        time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise Alpha015NetworkError(
        f"publisher request failed ({type(last_error).__name__}: {last_error})"
    ) from last_error


def _fetch_small(url: str, *, maximum_bytes: int) -> bytes:
    with _request(url) as response:
        payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise Alpha015NetworkError("publisher response exceeded the fixed byte limit")
    return payload


def _head_content_length(url: str) -> int:
    with _request(url, method="HEAD") as response:
        value = response.headers.get("Content-Length")
    try:
        length = int(value or 0)
    except ValueError as error:
        raise Alpha015NetworkError("publisher Content-Length is invalid") from error
    if length <= 0 or length > MAX_COMPRESSED_BYTES:
        raise Alpha015NetworkError("publisher Content-Length is outside limits")
    return length


def _download_temporary(
    url: str,
    *,
    directory: Path,
    expected_bytes: int,
    reserve_bytes: int,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < expected_bytes + reserve_bytes:
        raise Alpha015AcquisitionError(
            "free disk space would cross the acquisition reserve"
        )
    descriptor, raw_path = tempfile.mkstemp(prefix=".download-", dir=directory)
    temporary = Path(raw_path)
    received = 0
    try:
        with os.fdopen(descriptor, "wb") as handle, _request(url) as response:
            header = response.headers.get("Content-Length")
            if header is None or int(header) != expected_bytes:
                raise Alpha015NetworkError(
                    "archive Content-Length changed after the sealed size inventory"
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_bytes or received > MAX_COMPRESSED_BYTES:
                    raise Alpha015NetworkError("archive exceeded its sealed size")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if received != expected_bytes:
            raise Alpha015NetworkError("archive download ended before Content-Length")
        return temporary
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise Alpha015AcquisitionError(
            f"required Alpha 015 artifact is unavailable: {path.name}"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha015AcquisitionError(f"invalid JSON artifact: {path.name}") from error
    if not isinstance(payload, dict):
        raise Alpha015AcquisitionError(f"JSON artifact is not an object: {path.name}")
    return payload


def _load_authorized_plan(
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, str | None]:
    preregistration = _load_json(
        PROJECT_ROOT / "research" / "alpha015-preregistration.json"
    )
    activation_lock = _load_json(
        PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
    )
    if phase == "development":
        readiness, _ = build_report()
        if not (
            readiness.get("status") == "READY_FOR_DATA_ACQUISITION"
            and readiness.get("return_data_authorized") is True
        ):
            raise Alpha015AcquisitionError(
                "source-bound development acquisition is not authorized"
            )
        plan = build_development_acquisition_plan(
            readiness=readiness,
            preregistration=preregistration,
            activation_lock=activation_lock,
        )
        return plan, preregistration, activation_lock, None, None
    if phase != "locked_validation":
        raise Alpha015AcquisitionError("phase must be development or locked_validation")
    report_path = PROJECT_ROOT / "research" / "alpha015_development" / "report.json"
    unlock_path = PROJECT_ROOT / "research" / "alpha015_development" / "validation-unlock.json"
    report_bytes = report_path.read_bytes()
    report = _load_json(report_path)
    unlock = _load_json(unlock_path)
    report_sha256 = sha256(report_bytes).hexdigest()
    if not validate_validation_unlock(
        unlock,
        report=report,
        preregistration=preregistration,
        activation_lock=activation_lock,
        report_sha256=report_sha256,
    ):
        raise Alpha015AcquisitionError("locked validation unlock receipt is invalid")
    plan = build_phase_acquisition_plan(
        readiness={},
        preregistration=preregistration,
        activation_lock=activation_lock,
        phase=phase,
        validation_unlock=unlock,
        expected_development_report_sha256=report_sha256,
    )
    return plan, preregistration, activation_lock, unlock, report_sha256


def _relative_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(receipt)
    for key in ("archive", "checksum", "file"):
        if key in normalized:
            normalized[key] = str(Path(str(normalized[key])).resolve().relative_to(DATA_ROOT.resolve()))
    return normalized


def _load_or_empty_size_inventory(plan: dict[str, Any]) -> dict[str, Any]:
    if not SIZE_PATH.exists():
        return build_size_inventory_receipt(plan, [])
    receipt = _load_json(SIZE_PATH)
    if not verify_size_inventory_receipt(receipt, plan):
        raise Alpha015AcquisitionError("size inventory receipt failed replay")
    return receipt


def _probe_sizes(plan: dict[str, Any], *, limit: int) -> dict[str, Any]:
    receipt = _load_or_empty_size_inventory(plan)
    completed = {
        (item["dataset"], item["symbol"], item["date"])
        for item in receipt["measurements"]
    }
    pending = [
        item
        for item in plan["files"]
        if item["dataset"] != "funding"
        and (item["dataset"], item["symbol"], item["date"]) not in completed
    ]
    measurements = list(receipt["measurements"])
    for item in pending[:limit]:
        measurements.append(
            {
                "dataset": item["dataset"],
                "symbol": item["symbol"],
                "date": item["date"],
                "content_length": _head_content_length(item["source_url"]),
            }
        )
        receipt = build_size_inventory_receipt(plan, measurements)
        _atomic_json(SIZE_PATH, receipt)
    return receipt


def _size_map_and_budget(
    plan: dict[str, Any], *, reserve_bytes: int
) -> tuple[dict[tuple[str, str, str], int], dict[str, Any]]:
    receipt = _load_or_empty_size_inventory(plan)
    if receipt["status"] != "COMPLETE":
        raise Alpha015AcquisitionError(
            "size inventory must be complete before downloading return bytes"
        )
    sizes = {
        (item["dataset"], item["symbol"], item["date"]): int(
            item["content_length"]
        )
        for item in receipt["measurements"]
    }
    budget = assess_storage_budget(
        plan,
        archive_content_lengths=sizes,
        free_bytes=shutil.disk_usage(DATA_ROOT).free,
        reserve_bytes=reserve_bytes,
    )
    if budget["status"] != "PASS":
        raise Alpha015AcquisitionError(
            f"insufficient storage: short by {budget['bytes_short']} bytes"
        )
    return sizes, budget


def _load_or_empty_progress(plan: dict[str, Any]) -> dict[str, Any]:
    if not PROGRESS_PATH.exists():
        return build_progress_receipt(plan, [])
    progress = _load_json(PROGRESS_PATH)
    if not verify_progress_receipt(progress, plan):
        raise Alpha015AcquisitionError("acquisition progress receipt failed replay")
    return progress


def _seal_one(
    item: dict[str, Any],
    *,
    sizes: dict[tuple[str, str, str], int],
    reserve_bytes: int,
) -> dict[str, Any]:
    dataset, symbol, day_text = item["dataset"], item["symbol"], item["date"]
    day = date.fromisoformat(day_text)
    output_dir = DATA_ROOT / dataset / symbol / day_text
    if dataset == "funding":
        receipt = download_and_seal_funding(
            symbol=symbol,
            day=day,
            output_dir=output_dir,
            fetch=lambda url: _fetch_small(url, maximum_bytes=2_000_000),
        )
        return _relative_receipt(receipt)

    spec = archive_spec(dataset, symbol, day)
    archive_path = output_dir / spec["archive_name"]
    checksum_path = output_dir / f"{spec['archive_name']}.CHECKSUM"
    if archive_path.is_file() and checksum_path.is_file():
        verified = verify_publisher_archive_file(
            archive_path,
            checksum_path.read_bytes(),
            expected_name=spec["archive_name"],
            expected_csv_name=spec["csv_name"],
            dataset=dataset,
            day=day,
        )
        return _relative_receipt(
            {
                **spec,
                **verified,
                "archive": str(archive_path),
                "checksum": str(checksum_path),
                "status": "VERIFIED_AND_SEALED",
            }
        )
    checksum_payload = _fetch_small(item["checksum_url"], maximum_bytes=4096)
    expected_bytes = sizes[(dataset, symbol, day_text)]
    temporary = _download_temporary(
        item["source_url"],
        directory=output_dir,
        expected_bytes=expected_bytes,
        reserve_bytes=reserve_bytes,
    )
    try:
        receipt = seal_downloaded_archive_file(
            temporary,
            checksum_payload,
            dataset=dataset,
            symbol=symbol,
            day=day,
            output_dir=output_dir,
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _relative_receipt(receipt)


def _download_batch(
    plan: dict[str, Any],
    activation_lock: dict[str, Any],
    *,
    validation_unlock: dict[str, Any] | None,
    development_report_sha256: str | None,
    limit: int,
    reserve_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sizes, budget = _size_map_and_budget(plan, reserve_bytes=reserve_bytes)
    progress = _load_or_empty_progress(plan)
    completed = {
        (item["dataset"], item["symbol"], item["date"])
        for item in progress["files"]
    }
    pending = [
        item
        for item in plan["files"]
        if (item["dataset"], item["symbol"], item["date"]) not in completed
    ]
    files = list(progress["files"])
    for item in pending[:limit]:
        files.append(_seal_one(item, sizes=sizes, reserve_bytes=reserve_bytes))
        progress = build_progress_receipt(plan, files)
        _atomic_json(PROGRESS_PATH, progress)

    if progress["status"] == "COMPLETE":
        manifest = {
            "schema_version": 1,
            "candidate": plan["candidate"],
            "phase": plan["phase"],
            "status": "PASS",
            "plan_sha256": plan["plan_sha256"],
            "preregistration_sha256": plan["preregistration_sha256"],
            "activation_lock_sha256": plan["activation_lock_sha256"],
            "size_inventory_sha256": _load_json(SIZE_PATH)[
                "size_inventory_sha256"
            ],
            "files": progress["files"],
            "locked_validation_open": plan["locked_validation_open"],
            "live_order_authorization": "CLOSED",
        }
        manifest["validation"] = validate_phase_archive_bundle(
            manifest,
            activation_lock,
            activation_lock,
            archive_root=DATA_ROOT,
            phase=plan["phase"],
            validation_unlock=validation_unlock,
            expected_preregistration_sha256=plan["preregistration_sha256"],
            expected_development_report_sha256=development_report_sha256,
        )
        _write_once_json(MANIFEST_PATH, manifest)
    return progress, budget


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe-sizes",
        type=int,
        default=0,
        metavar="N",
        help="HEAD-probe at most N publisher archives after authorization",
    )
    parser.add_argument(
        "--download-limit",
        type=int,
        default=0,
        metavar="N",
        help="download and seal at most N planned files after size preflight",
    )
    parser.add_argument("--reserve-gib", type=int, default=25)
    parser.add_argument(
        "--phase",
        choices=("development", "locked_validation"),
        default="development",
        help="locked validation requires the immutable development PASS receipt",
    )
    args = parser.parse_args()
    if not 0 <= args.probe_sizes <= 500 or not 0 <= args.download_limit <= 100:
        raise SystemExit("probe/download limits are outside safe bounds")
    if args.reserve_gib < 10:
        raise SystemExit("disk reserve must be at least 10 GiB")

    # Authorization happens before creating the data directory, lock, plan, or
    # making any network request.
    global DATA_ROOT, PLAN_PATH, SIZE_PATH, PROGRESS_PATH, MANIFEST_PATH, LOCK_PATH
    phase = args.phase
    if phase == "locked_validation":
        DATA_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / phase
        PLAN_PATH = DATA_ROOT / "acquisition-plan.json"
        SIZE_PATH = DATA_ROOT / "size-inventory.json"
        PROGRESS_PATH = DATA_ROOT / "progress.json"
        MANIFEST_PATH = DATA_ROOT / "manifest.json"
        LOCK_PATH = DATA_ROOT / ".acquisition.lock"
    plan, preregistration, activation_lock, validation_unlock, report_sha256 = _load_authorized_plan(phase)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("another Alpha 015 acquisition process is active") from error
        if PLAN_PATH.exists():
            stored_plan = _load_json(PLAN_PATH)
            readiness = build_report()[0] if phase == "development" else {}
            if not validate_phase_acquisition_plan(
                stored_plan,
                readiness=readiness,
                preregistration=preregistration,
                activation_lock=activation_lock,
                phase=phase,
                validation_unlock=validation_unlock,
                expected_development_report_sha256=report_sha256,
            ):
                raise SystemExit("stored Alpha 015 acquisition plan failed replay")
        else:
            _write_once_json(PLAN_PATH, plan)

        size_receipt = _load_or_empty_size_inventory(plan)
        if args.probe_sizes:
            size_receipt = _probe_sizes(plan, limit=args.probe_sizes)
        output: dict[str, Any] = {
            "status": "AUTHORIZED_PLAN_ONLY",
            "plan_sha256": plan["plan_sha256"],
            "planned_files": plan["file_count"],
            "size_inventory_status": size_receipt["status"],
            "measured_archives": size_receipt["measured_archives"],
            "required_archives": size_receipt["required_archives"],
            "return_bytes_downloaded_this_run": False,
            "locked_validation_open": plan["locked_validation_open"],
            "live_order_authorization": "CLOSED",
        }
        if args.download_limit:
            progress, budget = _download_batch(
                plan,
                activation_lock,
                validation_unlock=validation_unlock,
                development_report_sha256=report_sha256,
                limit=args.download_limit,
                reserve_bytes=args.reserve_gib * 1024**3,
            )
            output.update(
                {
                    "status": progress["status"],
                    "completed_files": progress["completed_files"],
                    "required_files": progress["required_files"],
                    "storage_budget": budget,
                    "return_bytes_downloaded_this_run": True,
                }
            )
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Alpha015AcquisitionError, Alpha015DataError, Alpha015NetworkError) as error:
        raise SystemExit(f"Alpha 015 acquisition blocked: {error}") from error
