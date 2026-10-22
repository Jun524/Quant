"""Version fingerprints for the fixed Alpha #7 collection protocol."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path


PROTOCOL_FILES = (
    "scripts/collect_microstructure.py",
    "quant/data/orderbook.py",
    "quant/research/microstructure.py",
    "quant/research/protocol.py",
    "quant/execution/shadow_maker.py",
    "research/alpha007-preregistration.json",
)

LEGACY_PROTOCOL_FILE_SETS = (
    frozenset(
        {
            "scripts/collect_microstructure.py",
            "quant/data/orderbook.py",
            "quant/research/microstructure.py",
            "quant/execution/shadow_maker.py",
            "research/alpha007-preregistration.json",
        }
    ),
)


def protocol_digests(project_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in PROTOCOL_FILES:
        digest = sha256()
        with (project_root / relative).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[relative] = digest.hexdigest()
    return result
