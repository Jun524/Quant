"""Append-only on-disk storage for point-in-time universe observations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
from typing import Iterable

from quant.data.universe import InstrumentSnapshot


def snapshot_path(directory: Path, observed_at: datetime) -> Path:
    stamp = observed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"universe-{stamp}.json"


def write_snapshot_batch(*, directory: Path, observed_at: datetime, instruments: list[dict], metadata: dict | None = None) -> Path:
    """Write one immutable observation batch; a duplicate timestamp is refused."""
    path = snapshot_path(directory, observed_at)
    if path.exists():
        raise FileExistsError(f"snapshot already exists: {path}")
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "metadata": metadata or {},
        "instruments": instruments,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_manifest_path(snapshot: Path) -> Path:
    return snapshot.parent / "manifests" / f"{snapshot.stem}.manifest.json"


def _latest_forward_manifest(
    directory: Path,
    *,
    protocol_sha256: dict[str, str],
) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    for path in (directory / "manifests").glob("universe-*.manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("evidence_role") != "daily_forward_universe":
                continue
            if payload.get("protocol_sha256") != protocol_sha256:
                continue
            observed = datetime.fromisoformat(str(payload["observed_at"])).astimezone(timezone.utc)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
            continue
        candidates.append((observed, path))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def seal_snapshot_batch(
    snapshot: Path,
    *,
    protocol_sha256: dict[str, str],
    evidence_role: str,
    scheduled_date: str | None,
    collection_started_at: datetime,
    collection_completed_at: datetime,
) -> Path:
    """Bind an immutable raw batch to protocol, timing, and the prior manifest."""
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    observed_at = datetime.fromisoformat(str(payload["observed_at"])).astimezone(timezone.utc)
    instruments = payload.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("universe snapshot has no instruments")
    symbols = [str(item.get("symbol") or "") for item in instruments if isinstance(item, dict)]
    if len(symbols) != len(instruments) or not all(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("universe snapshot symbols are missing or duplicated")
    if any(str(item.get("timestamp")) != payload["observed_at"] for item in instruments):
        raise ValueError("instrument timestamps do not exactly match batch observed_at")
    metadata = payload.get("metadata") or {}
    if evidence_role == "daily_forward_universe" and (
        metadata.get("all_usdt_perpetual") is not True or scheduled_date != observed_at.date().isoformat()
    ):
        raise ValueError("forward universe snapshot does not match its scheduled full-market day")
    manifest = snapshot_manifest_path(snapshot)
    if manifest.exists():
        raise FileExistsError(f"snapshot manifest already exists: {manifest}")
    prior = (
        _latest_forward_manifest(snapshot.parent, protocol_sha256=protocol_sha256)
        if evidence_role == "daily_forward_universe"
        else None
    )
    previous_path = None
    previous_digest = None
    if prior is not None:
        previous_path = str(prior.relative_to(snapshot.parent))
        previous_digest = file_sha256(prior)
    manifest_payload = {
        "schema_version": 1,
        "status": "SEALED",
        "evidence_role": evidence_role,
        "observed_at": observed_at.isoformat(),
        "scheduled_date": scheduled_date,
        "collection_started_at": collection_started_at.astimezone(timezone.utc).isoformat(),
        "collection_completed_at": collection_completed_at.astimezone(timezone.utc).isoformat(),
        "collection_duration_seconds": (
            collection_completed_at - collection_started_at
        ).total_seconds(),
        "snapshot_path": snapshot.name,
        "snapshot_sha256": file_sha256(snapshot),
        "instrument_count": len(instruments),
        "eligible_count": sum(bool(item.get("eligible")) for item in instruments),
        "depth_collected_count": sum(bool(item.get("depth_collected")) for item in instruments),
        "protocol_sha256": protocol_sha256,
        "previous_manifest_path": previous_path,
        "previous_manifest_sha256": previous_digest,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest)
    return manifest


def validate_sealed_snapshot(
    snapshot: Path,
    *,
    expected_protocol: dict[str, str],
    required_evidence_role: str = "daily_forward_universe",
) -> dict:
    manifest_path = snapshot_manifest_path(snapshot)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"sealed snapshot cannot be read: {error}") from error
    instruments = payload.get("instruments")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("status") != "SEALED"
        or manifest.get("evidence_role") != required_evidence_role
        or manifest.get("snapshot_path") != snapshot.name
        or manifest.get("snapshot_sha256") != file_sha256(snapshot)
        or manifest.get("protocol_sha256") != expected_protocol
        or not isinstance(instruments, list)
        or int(manifest.get("instrument_count") or 0) != len(instruments)
        or manifest.get("observed_at") != payload.get("observed_at")
    ):
        raise ValueError("sealed universe snapshot manifest mismatch")
    observed = datetime.fromisoformat(str(payload["observed_at"])).astimezone(timezone.utc)
    if manifest.get("scheduled_date") != observed.date().isoformat():
        raise ValueError("sealed universe snapshot scheduled date mismatch")
    symbols = [str(item.get("symbol") or "") for item in instruments if isinstance(item, dict)]
    if len(symbols) != len(instruments) or not all(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("sealed universe symbols are missing or duplicated")
    if any(str(item.get("timestamp")) != payload["observed_at"] for item in instruments):
        raise ValueError("sealed universe instrument timestamp mismatch")
    return {
        "manifest_path": manifest_path,
        "manifest": manifest,
        "payload": payload,
        "observed_at": observed,
    }


def load_instrument_snapshots(directory: Path) -> list[InstrumentSnapshot]:
    """Read only completed batches; malformed snapshots fail closed."""
    snapshots: list[InstrumentSnapshot] = []
    for path in sorted(directory.glob("universe-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError(f"unsupported universe snapshot schema: {path}")
        observed_at = datetime.fromisoformat(payload["observed_at"])
        for item in payload.get("instruments", []):
            snapshots.append(
                InstrumentSnapshot(
                    pair=item["symbol"],
                    observed_at=observed_at,
                    listed_at=datetime.fromisoformat(item["listed_at"]),
                    quote_volume_24h=float(item.get("quote_volume") or 0.0),
                    candle_count=int(item.get("candle_count") or 0),
                    data_continuous=bool(item.get("data_continuous")),
                    spread_bps=item.get("spread_bps"),
                    orderbook_depth=item.get("depth_10bps"),
                    active=bool(item.get("listing_status") == "active"),
                    data_fresh=bool(item.get("data_fresh", True)),
                )
            )
    return snapshots
