"""Content-bound references for research inputs and reports."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def artifact_reference(path: Path | None) -> dict[str, str] | None:
    """Identify an optional artifact by both location and immutable content hash."""
    if path is None:
        return None
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest()}
