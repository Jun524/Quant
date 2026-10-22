"""Current protocol fingerprint for forward-only universe observations."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "research" / "universe-forward-policy.json"
PROTOCOL_FILES = (
    "research/universe-forward-policy.json",
    "scripts/collect_universe_snapshot.py",
    "scripts/run_universe_scheduler.py",
    "quant/data/universe.py",
    "quant/data/snapshots.py",
    "quant/data/universe_history.py",
    "quant/data/universe_protocol.py",
)


def protocol_digests(project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    return {
        relative: sha256((project_root / relative).read_bytes()).hexdigest()
        for relative in PROTOCOL_FILES
    }


def policy_document(path: Path = POLICY_PATH) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("unsupported universe forward policy schema")
    return value
