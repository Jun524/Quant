"""Append-only experiment registry with atomic local writes."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _empty_registry() -> dict[str, Any]:
    return {"schema_version": 1, "experiments": [], "events": []}


class ExperimentRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_registry()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("experiments"), list):
            raise ValueError(f"invalid experiment registry: {self.path}")
        data.setdefault("events", [])
        if not isinstance(data["events"], list):
            raise ValueError(f"invalid experiment events: {self.path}")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=".registry-", delete=False
        ) as temporary:
            json.dump(data, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, self.path)

    @contextmanager
    def _exclusive_write(self):
        """Serialize registry read-modify-write cycles across local processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def register(
        self,
        *,
        strategy: str,
        hypothesis: str,
        reason: str,
        result: dict[str, Any],
        parent_experiment: int | None = None,
        reason_for_change: str | None = None,
        trial_accounting: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._exclusive_write():
            data = self._load()
            experiment_id = max(
                (item["experiment_id"] for item in data["experiments"]), default=0
            ) + 1
            record = {
                "experiment_id": experiment_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy,
                "hypothesis": hypothesis,
                "reason": reason,
                "parent_experiment": parent_experiment,
                "reason_for_change": reason_for_change,
                "trial_accounting": trial_accounting,
                "result": result,
            }
            data["experiments"].append(record)
            self._write(data)
        return record

    def register_unique_strategy(
        self,
        *,
        strategy: str,
        hypothesis: str,
        reason: str,
        result: dict[str, Any],
        parent_experiment: int | None = None,
        reason_for_change: str | None = None,
        trial_accounting: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically return the sole strategy record or create it once."""
        with self._exclusive_write():
            data = self._load()
            existing = [
                item for item in data["experiments"] if item.get("strategy") == strategy
            ]
            if len(existing) > 1:
                raise ValueError(f"duplicate strategy records: {strategy}")
            if existing:
                return existing[0], False
            experiment_id = max(
                (item["experiment_id"] for item in data["experiments"]), default=0
            ) + 1
            record = {
                "experiment_id": experiment_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy,
                "hypothesis": hypothesis,
                "reason": reason,
                "parent_experiment": parent_experiment,
                "reason_for_change": reason_for_change,
                "trial_accounting": trial_accounting,
                "result": result,
            }
            data["experiments"].append(record)
            self._write(data)
        return record, True

    def append_event(self, *, experiment_id: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an immutable status/result event for an existing experiment."""
        with self._exclusive_write():
            data = self._load()
            if not any(
                item.get("experiment_id") == experiment_id
                for item in data["experiments"]
            ):
                raise ValueError(f"unknown experiment_id: {experiment_id}")
            event_id = max(
                (int(item["event_id"]) for item in data["events"]), default=0
            ) + 1
            event = {
                "event_id": event_id,
                "experiment_id": experiment_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "payload": payload,
            }
            data["events"].append(event)
            self._write(data)
        return event
