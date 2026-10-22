#!/usr/bin/env python3
"""Start, inspect, or stop the credential-free Alpha #1 forward dry-run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.record_mtm_equity import fetch_balance
from quant.monitoring.sleep_guard import inspect_caffeinate_guard


BASE_CONFIG = PROJECT_ROOT / "user_data" / "config.futures.json"
OVERLAY_CONFIG = PROJECT_ROOT / "user_data" / "config.futures.forward-dryrun.json"
STATE_PATH = PROJECT_ROOT / "research" / "alpha001-forward-dryrun-state.json"
DB_PATH = PROJECT_ROOT / "user_data" / "tradesv3.alpha001-forward-dryrun.sqlite"
BOT_LOG = PROJECT_ROOT / "user_data" / "logs" / "alpha001-forward-dryrun.log"
EQUITY_ROOT = PROJECT_ROOT / "research" / "equity" / "raw"
API_URL = "http://127.0.0.1:18081"
COHORT_PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha001-forward-cohort-20261019.json"
PROTOCOL_FILES = (
    "scripts/manage_alpha001_forward_dryrun.py",
    "user_data/config.futures.json",
    "user_data/config.futures.forward-dryrun.json",
    "user_data/config.futures.bias-audit.json",
    "user_data/strategies/FuturesBtcRegimeStrategy.py",
    "user_data/strategies/FuturesSlowTrendStrategy.py",
    "quant/regime/classifier.py",
    "scripts/record_mtm_equity.py",
    "scripts/alpha001_forward_report.py",
    "quant/monitoring/equity.py",
    "quant/monitoring/sleep_guard.py",
    "quant/research/forward_dryrun.py",
    "quant/research/monthly_target.py",
    "quant/research/risk.py",
    "research/promotion-policy.json",
    "quant/execution/maker_quality.py",
    "scripts/maker_execution_report.py",
    "scripts/live_preflight.py",
    "scripts/audit_futures_order_feasibility.py",
    "scripts/audit_binance_api_permissions.py",
    "scripts/audit_binance_futures_account.py",
    "quant/execution/bounded_notional.py",
    "quant/execution/shadow_maker.py",
    "scripts/shadow_maker_probe.py",
    "scripts/manage_shadow_entry_campaign.py",
    "scripts/record_alpha001_entry_execution.py",
    "scripts/run_alpha001_bias_audit.py",
    "research/alpha001-forward-cohort-20261019.json",
)
SENSITIVE_ENV_NAMES = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_FUTURES_API_KEY",
    "BINANCE_FUTURES_API_SECRET",
    "FREQTRADE__EXCHANGE__API_KEY",
    "FREQTRADE__EXCHANGE__SECRET",
    "FREQTRADE__EXCHANGE__KEY",
)
HEARTBEAT_STALE_SECONDS = 180
STARTUP_PROBE_TIMEOUT_SECONDS = 120
LOG_TAIL_BYTES = 2 * 1024 * 1024


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_state() -> dict | None:
    if not STATE_PATH.is_file():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def process_running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    command = completed.stdout
    return completed.returncode == 0 and "freqtrade" in command and " trade " in f" {command} "


def recorder_process_running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "record_mtm_equity.py" in completed.stdout


def entry_recorder_process_running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "record_alpha001_entry_execution.py" in completed.stdout


def digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def protocol_digests() -> dict[str, str]:
    return {relative: digest(PROJECT_ROOT / relative) for relative in PROTOCOL_FILES}


def load_forward_cohort() -> dict:
    """Load the immutable cohort record that separates sealed forward samples.

    A new cohort is permitted only to repair evidence continuity.  It is not a
    vehicle for changing the strategy, risk settings, or live-order permissions.
    The record itself is protocol-bound, so any later edit invalidates the cohort.
    """
    try:
        cohort = json.loads(COHORT_PREREGISTRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Alpha #1 cohort preregistration is unreadable: {error}") from error
    if not isinstance(cohort, dict):
        raise RuntimeError("Alpha #1 cohort preregistration must be an object")
    required = {
        "schema_version": 1,
        "status": "IMMUTABLE_PREREGISTRATION",
        "candidate": "FuturesBtcRegimeRiskBudgetLong",
        "strategy_or_risk_settings_changed": False,
        "live_order_authorization": "CLOSED",
    }
    if any(cohort.get(key) != value for key, value in required.items()):
        raise RuntimeError("Alpha #1 cohort preregistration does not preserve the locked safeguards")
    cohort_id = cohort.get("cohort_id")
    if not isinstance(cohort_id, str) or not cohort_id:
        raise RuntimeError("Alpha #1 cohort preregistration has no cohort id")
    return cohort


def equity_manifest_path(output: Path) -> Path:
    return output.with_suffix(".manifest.json")


def write_active_equity_manifest(state: dict, output: Path) -> Path:
    manifest_path = equity_manifest_path(output)
    payload = {
        "schema_version": 1,
        "status": "ACTIVE",
        "evidence_kind": "freqtrade_dryrun_mark_to_market",
        "equity_path": str(output.relative_to(PROJECT_ROOT)),
        "started_at": state["started_at"],
        "ended_at": None,
        "strategy": state["strategy"],
        "database_path": state["database"],
        "protocol_sha256": state["protocol_sha256"],
        "source": "authenticated_loopback_freqtrade_net_liquidation",
        "interval_seconds": 60,
        "seal": None,
    }
    atomic_write(manifest_path, payload)
    return manifest_path


def seal_equity_manifest(state: dict, *, status: str) -> Path | None:
    value = state.get("equity_output")
    if not value:
        return None
    output = PROJECT_ROOT / value
    if not output.is_file():
        return None
    manifest_path = PROJECT_ROOT / state.get("equity_manifest", str(equity_manifest_path(output).relative_to(PROJECT_ROOT)))
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        payload = {
            "schema_version": 1,
            "evidence_kind": "freqtrade_dryrun_mark_to_market",
            "equity_path": str(output.relative_to(PROJECT_ROOT)),
            "started_at": state.get("started_at"),
            "strategy": state.get("strategy"),
            "database_path": state.get("database"),
            "protocol_sha256": state.get("protocol_sha256"),
            "source": "authenticated_loopback_freqtrade_net_liquidation",
            "interval_seconds": 60,
        }
    content = output.read_text(encoding="utf-8")
    complete_lines = [line for line in content.splitlines() if line.strip()]
    last_observed_at = None
    if complete_lines and content.endswith("\n"):
        last_observed_at = json.loads(complete_lines[-1]).get("observed_at")
    payload.update(
        {
            "status": status,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "seal": {
                "bytes": output.stat().st_size,
                "sha256": digest(output),
                "observations": len(complete_lines) if content.endswith("\n") else max(0, len(complete_lines) - 1),
                "last_observed_at": last_observed_at,
            },
        }
    )
    atomic_write(manifest_path, payload)
    return manifest_path


def protocol_database_path(protocol: dict[str, str]) -> Path:
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    protocol_id = sha256(encoded).hexdigest()[:16]
    return PROJECT_ROOT / "user_data" / f"tradesv3.alpha001-forward-dryrun-{protocol_id}.sqlite"


def select_database_path(existing: dict | None, protocol: dict[str, str]) -> Path:
    """Reuse a database only when it is already bound to the exact protocol."""
    if existing and existing.get("protocol_sha256") == protocol:
        database_value = existing.get("database")
        if isinstance(database_value, str):
            return PROJECT_ROOT / database_value
    return protocol_database_path(protocol)


def database_summary(path: Path = DB_PATH) -> dict:
    if not path.is_file():
        return {"exists": False, "trades": 0, "open_trades": 0, "closed_trades": 0, "closed_profit_abs": 0.0}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone()
            if not table:
                return {"exists": True, "trades_table": False, "bytes": path.stat().st_size}
            trades, open_trades, closed_trades, closed_profit = connection.execute(
                "SELECT COUNT(*), SUM(CASE WHEN is_open = 1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN is_open = 0 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(CASE WHEN is_open = 0 THEN close_profit_abs ELSE 0 END), 0) FROM trades"
            ).fetchone()
            return {
                "exists": True,
                "trades_table": True,
                "bytes": path.stat().st_size,
                "trades": int(trades or 0),
                "open_trades": int(open_trades or 0),
                "closed_trades": int(closed_trades or 0),
                "closed_profit_abs": float(closed_profit or 0.0),
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"exists": True, "error": f"{type(exc).__name__}: {exc}", "bytes": path.stat().st_size}


def inspect_bot_log(
    path: Path,
    *,
    now: datetime | None = None,
    local_timezone=None,
    expected_pid: int | None = None,
    since: datetime | None = None,
) -> dict:
    """Inspect a bounded log tail for heartbeat freshness and hard errors."""
    now = now or datetime.now(timezone.utc)
    local_timezone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
    if not path.is_file():
        return {
            "exists": False,
            "recent_error_count": 0,
            "last_heartbeat_at": None,
            "heartbeat_age_seconds": None,
            "heartbeat_stale_after_seconds": HEARTBEAT_STALE_SECONDS,
        }
    size = path.stat().st_size
    with path.open("rb") as handle:
        offset = max(0, size - LOG_TAIL_BYTES)
        handle.seek(offset)
        content = handle.read().decode("utf-8", errors="replace")
    if offset:
        content = content.partition("\n")[2]
    lines = content.splitlines()
    if since is not None:
        since = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        scoped_lines = []
        include = False
        for line in lines:
            try:
                line_time = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(
                    tzinfo=local_timezone
                ).astimezone(timezone.utc)
                include = line_time >= since
            except ValueError:
                pass
            if include:
                scoped_lines.append(line)
        lines = scoped_lines
    error_markers = (" - ERROR - ", " - CRITICAL - ", "Traceback (most recent call last)")
    recent_error_count = sum(any(marker in line for marker in error_markers) for line in lines)
    last_heartbeat = None
    heartbeat_pid = None
    for line in reversed(lines):
        if "Bot heartbeat." not in line:
            continue
        match = re.search(r"\bPID=(\d+)\b", line)
        parsed_pid = int(match.group(1)) if match else None
        if expected_pid is not None and parsed_pid != expected_pid:
            continue
        try:
            local_time = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=local_timezone)
            last_heartbeat = local_time.astimezone(timezone.utc)
            heartbeat_pid = parsed_pid
        except ValueError:
            pass
        break
    heartbeat_age = max(0.0, (now - last_heartbeat).total_seconds()) if last_heartbeat else None
    return {
        "exists": True,
        "bytes": size,
        "recent_error_count": recent_error_count,
        "last_heartbeat_at": last_heartbeat.isoformat() if last_heartbeat else None,
        "heartbeat_pid": heartbeat_pid,
        "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
        "heartbeat_stale_after_seconds": HEARTBEAT_STALE_SECONDS,
    }


def equity_output_summary(path_value: str | None, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not path_value:
        return {"configured": False, "exists": False, "observations": 0, "age_seconds": None}
    path = PROJECT_ROOT / path_value
    if not path.is_file():
        return {"configured": True, "path": path_value, "exists": False, "observations": 0, "age_seconds": None}
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    with path.open("rb") as handle:
        observations = sum(1 for line in handle if line.strip())
    return {
        "configured": True,
        "path": path_value,
        "exists": True,
        "bytes": path.stat().st_size,
        "observations": observations,
        "modified_at": modified.isoformat(),
        "age_seconds": round(max(0.0, (now - modified).total_seconds()), 3),
    }


def inspect_equity_manifest(
    path_value: str | None,
    *,
    state: dict,
    current_protocol: dict[str, str],
    project_root: Path = PROJECT_ROOT,
) -> dict:
    """Validate the active evidence binding used by the running recorder."""
    if not path_value:
        return {"configured": False, "exists": False, "valid": False, "reason": "manifest not configured"}
    path = project_root / path_value
    result = {"configured": True, "path": path_value, "exists": path.is_file(), "valid": False}
    if not path.is_file():
        result["reason"] = "manifest missing"
        return result
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        checks = {
            "schema_version": manifest.get("schema_version") == 1,
            "active_status": manifest.get("status") == "ACTIVE",
            "evidence_kind": manifest.get("evidence_kind") == "freqtrade_dryrun_mark_to_market",
            "equity_path": manifest.get("equity_path") == state.get("equity_output"),
            "started_at": manifest.get("started_at") == state.get("started_at"),
            "strategy": manifest.get("strategy") == state.get("strategy"),
            "database_path": manifest.get("database_path") == state.get("database"),
            "source": manifest.get("source") == "authenticated_loopback_freqtrade_net_liquidation",
            "launch_protocol": manifest.get("protocol_sha256") == state.get("protocol_sha256"),
            "current_protocol": manifest.get("protocol_sha256") == current_protocol,
            "unsealed": manifest.get("seal") is None,
        }
        failed = [name for name, passed in checks.items() if not passed]
        result.update(
            {
                "status": manifest.get("status"),
                "checks": checks,
                "valid": not failed,
                "reason": None if not failed else "failed checks: " + ", ".join(failed),
            }
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def inspect_entry_execution_manifest(
    path_value: str | None,
    *,
    state: dict,
    current_protocol: dict[str, str],
    project_root: Path = PROJECT_ROOT,
) -> dict:
    if not path_value:
        return {"configured": False, "exists": False, "valid": False, "reason": "manifest not configured"}
    path = project_root / path_value
    result = {"configured": True, "path": path_value, "exists": path.is_file(), "valid": False}
    if not path.is_file():
        result["reason"] = "manifest missing"
        return result
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        source_value = manifest.get("source_path")
        source_path = project_root / source_value if isinstance(source_value, str) else None
        try:
            updated_at = datetime.fromisoformat(str(manifest.get("updated_at")))
            if updated_at.tzinfo is None:
                raise ValueError("timezone missing")
            heartbeat_fresh = (
                0 <= (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds() <= 180
            )
        except (TypeError, ValueError):
            heartbeat_fresh = False
        source_rows = 0
        if source_path and source_path.is_file():
            with source_path.open("rb") as handle:
                source_rows = sum(1 for line in handle if line.strip())
        checks = {
            "schema_version": manifest.get("schema_version") == 1,
            "active_status": manifest.get("status") == "ACTIVE",
            "evidence_kind": manifest.get("evidence_kind") == "public_l2_shadow",
            "promotion_purpose": manifest.get("evidence_purpose") == "live_promotion_candidate",
            "trigger_kind": manifest.get("trigger_kind") == "alpha001_live_order_attempt",
            "source_path": manifest.get("source_path") == state.get("entry_execution_output"),
            "source_exists": bool(source_path and source_path.is_file()),
            "source_sha256": bool(
                source_path
                and source_path.is_file()
                and manifest.get("source_sha256") == digest(source_path)
            ),
            "row_count": manifest.get("orders") == source_rows,
            "heartbeat_fresh": heartbeat_fresh,
            "launch_protocol": manifest.get("protocol_sha256") == state.get("protocol_sha256"),
            "current_protocol": manifest.get("protocol_sha256") == current_protocol,
            "symbols": set(manifest.get("symbols") or [])
            == {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"},
            "timeout": manifest.get("timeout_seconds") == 300,
            "markout": manifest.get("markout_seconds") == 300,
        }
        failed = [name for name, passed in checks.items() if not passed]
        result.update(
            {
                "status": manifest.get("status"),
                "orders": manifest.get("orders"),
                "checks": checks,
                "valid": not failed,
                "reason": None if not failed else "failed checks: " + ", ".join(failed),
            }
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def forward_operational_health(
    *,
    state_status: str,
    running: bool,
    protocol_current: bool | None,
    log_health: dict,
    started_at: str | None,
    equity_recorder_running: bool | None = None,
    equity_age_seconds: float | None = None,
    equity_manifest_valid: bool | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    if not running:
        return "TERMINAL" if state_status in {"STOPPED", "FINISHED"} else "PROCESS_MISSING"
    if protocol_current is not True:
        return "RUNNING_PROTOCOL_DRIFT"
    if int(log_health.get("recent_error_count") or 0) > 0:
        return "RUNNING_WITH_ERRORS"
    if equity_manifest_valid is False:
        return "RUNNING_EQUITY_MANIFEST_INVALID"
    if equity_recorder_running is False:
        return "RUNNING_EQUITY_RECORDER_MISSING"
    if equity_age_seconds is not None and equity_age_seconds > HEARTBEAT_STALE_SECONDS:
        return "RUNNING_EQUITY_STALE"
    heartbeat_age = log_health.get("heartbeat_age_seconds")
    if heartbeat_age is not None:
        return "RUNNING_HEARTBEAT_STALE" if heartbeat_age > HEARTBEAT_STALE_SECONDS else "RUNNING_HEALTHY"
    try:
        started = datetime.fromisoformat(started_at or "")
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        uptime = max(0.0, (now - started).total_seconds())
    except ValueError:
        uptime = HEARTBEAT_STALE_SECONDS + 1
    return "RUNNING_STARTING" if uptime <= HEARTBEAT_STALE_SECONDS else "RUNNING_HEARTBEAT_MISSING"


def preflight() -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "live_preflight.py"),
            "--base",
            str(BASE_CONFIG),
            "--overlay",
            str(OVERLAY_CONFIG),
            "--allow-running-dry-run",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def status_payload() -> dict:
    state = load_state() or {"status": "NEVER_STARTED", "pid": None}
    running = process_running(state.get("pid"))
    launch_protocol = state.get("protocol_sha256")
    current_protocol = protocol_digests()
    now = datetime.now(timezone.utc)
    try:
        run_started_at = datetime.fromisoformat(str(state.get("started_at")))
    except (TypeError, ValueError):
        run_started_at = None
    log_health = inspect_bot_log(
        BOT_LOG,
        now=now,
        expected_pid=state.get("pid"),
        since=run_started_at,
    )
    recorder_running = recorder_process_running(state.get("equity_recorder_pid"))
    entry_recorder_running = entry_recorder_process_running(state.get("entry_execution_recorder_pid"))
    equity_output = equity_output_summary(state.get("equity_output"), now=now)
    equity_manifest = inspect_equity_manifest(
        state.get("equity_manifest"),
        state=state,
        current_protocol=current_protocol,
    )
    entry_manifest = inspect_entry_execution_manifest(
        state.get("entry_execution_manifest"),
        state=state,
        current_protocol=current_protocol,
    )
    state["running"] = running
    state["protocol_current"] = launch_protocol == current_protocol if launch_protocol else None
    database_value = state.get("database")
    database_path = PROJECT_ROOT / database_value if isinstance(database_value, str) else DB_PATH
    state["database"] = database_summary(database_path)
    state["database"]["path"] = str(database_path.relative_to(PROJECT_ROOT))
    state["bot_log"] = {
        "path": str(BOT_LOG.relative_to(PROJECT_ROOT)),
        "exists": BOT_LOG.is_file(),
        "bytes": BOT_LOG.stat().st_size if BOT_LOG.is_file() else 0,
        "modified_at": datetime.fromtimestamp(BOT_LOG.stat().st_mtime, timezone.utc).isoformat()
        if BOT_LOG.is_file()
        else None,
        "health": log_health,
    }
    state["equity_recorder"] = {
        "pid": state.get("equity_recorder_pid"),
        "running": recorder_running,
        "output": equity_output,
        "source": "authenticated_loopback_freqtrade_net_liquidation",
        "interval_seconds": 60,
    }
    state["equity_manifest_health"] = equity_manifest
    state["entry_execution_recorder"] = {
        "pid": state.get("entry_execution_recorder_pid"),
        "running": entry_recorder_running,
        "output": equity_output_summary(state.get("entry_execution_output"), now=now),
    }
    state["entry_execution_manifest_health"] = entry_manifest
    state["sleep_guard"] = inspect_caffeinate_guard(state.get("pid"))
    if state.get("status") == "NEVER_STARTED":
        state["operational_health"] = "NEVER_STARTED"
    else:
        state["operational_health"] = forward_operational_health(
            state_status=str(state.get("status") or "UNKNOWN"),
            running=running,
            protocol_current=state["protocol_current"],
            log_health=log_health,
            started_at=state.get("started_at"),
            equity_recorder_running=recorder_running if state.get("equity_output") else None,
            equity_age_seconds=equity_output.get("age_seconds"),
            equity_manifest_valid=equity_manifest.get("valid") if state.get("equity_output") else None,
            now=now,
        )
        if running and state.get("entry_execution_output") and not entry_recorder_running:
            state["operational_health"] = "RUNNING_ENTRY_EXECUTION_RECORDER_MISSING"
        elif running and state.get("entry_execution_manifest") and not entry_manifest.get("valid"):
            state["operational_health"] = "RUNNING_ENTRY_EXECUTION_MANIFEST_INVALID"
        if (
            running
            and state["sleep_guard"]["available"]
            and not state["sleep_guard"]["running"]
            and state["operational_health"].startswith("RUNNING_")
        ):
            state["operational_health"] = "RUNNING_SLEEP_GUARD_MISSING"
    return state


def start(_args) -> None:
    existing = load_state()
    if existing and process_running(existing.get("pid")):
        raise SystemExit(f"Alpha #1 forward dry-run already running with pid {existing['pid']}")
    if existing and existing.get("equity_output"):
        seal_equity_manifest(existing, status="SEALED_AFTER_PROCESS_EXIT")
    cohort = load_forward_cohort()
    checked = preflight()
    if checked.returncode != 0:
        print(checked.stdout, end="")
        print(checked.stderr, end="", file=sys.stderr)
        raise SystemExit(checked.returncode)

    BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    launch_protocol = protocol_digests()
    run_database = select_database_path(existing, launch_protocol)
    supervisor_log = BOT_LOG.with_name(
        f"alpha001-forward-supervisor-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    )
    entry_log_start_offset = BOT_LOG.stat().st_size if BOT_LOG.is_file() else 0
    command = [
        sys.executable,
        "-m",
        "freqtrade",
        "trade",
        "--config",
        str(BASE_CONFIG),
        "--config",
        str(OVERLAY_CONFIG),
        "--strategy",
        "FuturesBtcRegimeRiskBudgetLong",
        "--db-url",
        f"sqlite:///{run_database}",
        "--logfile",
        str(BOT_LOG),
    ]
    environment = dict(os.environ)
    for name in SENSITIVE_ENV_NAMES:
        environment.pop(name, None)
    api_username = "alpha001-recorder"
    api_password = secrets.token_urlsafe(32)
    environment.update(
        {
            "FREQTRADE__API_SERVER__USERNAME": api_username,
            "FREQTRADE__API_SERVER__PASSWORD": api_password,
            "FREQTRADE__API_SERVER__JWT_SECRET_KEY": secrets.token_urlsafe(48),
            "FREQTRADE__API_SERVER__WS_TOKEN": secrets.token_urlsafe(48),
        }
    )
    with supervisor_log.open("ab", buffering=0) as handle:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    caffeinate_pid = None
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        keeper = subprocess.Popen(
            [caffeinate, "-ims", "-w", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        caffeinate_pid = keeper.pid
    state = {
        "schema_version": 1,
        "status": "STARTING",
        "pid": process.pid,
        "caffeinate_pid": caffeinate_pid,
        "started_at": started_at,
        "strategy": "FuturesBtcRegimeRiskBudgetLong",
        "cohort_id": cohort["cohort_id"],
        "cohort_preregistration": str(COHORT_PREREGISTRATION_PATH.relative_to(PROJECT_ROOT)),
        "mode": "dry_run_no_exchange_credentials",
        "base_config": str(BASE_CONFIG.relative_to(PROJECT_ROOT)),
        "overlay_config": str(OVERLAY_CONFIG.relative_to(PROJECT_ROOT)),
        "database": str(run_database.relative_to(PROJECT_ROOT)),
        "bot_log": str(BOT_LOG.relative_to(PROJECT_ROOT)),
        "supervisor_log": str(supervisor_log.relative_to(PROJECT_ROOT)),
        "protocol_sha256": launch_protocol,
        "preflight": checked.stdout.strip().splitlines(),
    }
    atomic_write(STATE_PATH, state)
    balance_ready = False
    balance_error = None
    deadline = time.monotonic() + STARTUP_PROBE_TIMEOUT_SECONDS
    while process_running(process.pid) and time.monotonic() < deadline:
        try:
            balance = fetch_balance(API_URL, api_username, api_password, 3)
            balance_ready = balance.get("note") == "Simulated balances" and float(balance.get("total_bot") or 0) > 0
            if balance_ready:
                break
        except Exception as exc:  # bounded startup probe; exact failure is recorded without credentials
            balance_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    if not balance_ready:
        if process_running(process.pid):
            os.kill(process.pid, signal.SIGTERM)
        state.update({"status": "START_FAILED", "balance_probe_error": balance_error})
        atomic_write(STATE_PATH, state)
        print(json.dumps(status_payload(), indent=2))
        raise SystemExit(3)

    EQUITY_ROOT.mkdir(parents=True, exist_ok=True)
    equity_output = EQUITY_ROOT / f"alpha001-forward-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    recorder_log = supervisor_log.with_name(supervisor_log.stem.replace("supervisor", "equity-recorder") + ".log")
    recorder_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "record_mtm_equity.py"),
        "--url",
        API_URL,
        "--duration-seconds",
        str(366 * 24 * 3600),
        "--interval-seconds",
        "60",
        "--output",
        str(equity_output),
    ]
    recorder_environment = dict(environment)
    recorder_environment.update(
        {
            "FREQTRADE_API_USERNAME": api_username,
            "FREQTRADE_API_PASSWORD": api_password,
        }
    )
    with recorder_log.open("ab", buffering=0) as handle:
        recorder = subprocess.Popen(
            recorder_command,
            cwd=PROJECT_ROOT,
            env=recorder_environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    entry_root = PROJECT_ROOT / "research" / "execution" / "raw"
    entry_root.mkdir(parents=True, exist_ok=True)
    entry_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    entry_output = entry_root / f"alpha001-signal-entry-{entry_stamp}.jsonl"
    entry_manifest = entry_output.with_suffix(".manifest.json")
    entry_recorder_log = supervisor_log.with_name(
        supervisor_log.stem.replace("supervisor", "entry-execution-recorder") + ".log"
    )
    entry_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "record_alpha001_entry_execution.py"),
        "--log-path", str(BOT_LOG),
        "--output", str(entry_output),
        "--manifest", str(entry_manifest),
        "--start-offset", str(entry_log_start_offset),
    ]
    with entry_recorder_log.open("ab", buffering=0) as handle:
        entry_recorder = subprocess.Popen(
            entry_command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(2)
    running = process_running(process.pid)
    recorder_running = recorder_process_running(recorder.pid)
    entry_recorder_running = entry_recorder_process_running(entry_recorder.pid)
    state.update(
        {
            "equity_recorder_pid": recorder.pid,
            "equity_output": str(equity_output.relative_to(PROJECT_ROOT)),
            "equity_recorder_log": str(recorder_log.relative_to(PROJECT_ROOT)),
            "entry_execution_recorder_pid": entry_recorder.pid,
            "entry_execution_output": str(entry_output.relative_to(PROJECT_ROOT)),
            "entry_execution_manifest": str(entry_manifest.relative_to(PROJECT_ROOT)),
            "entry_execution_recorder_log": str(entry_recorder_log.relative_to(PROJECT_ROOT)),
            "api_scope": "authenticated_loopback_only",
            "status": "RUNNING"
            if running and recorder_running and entry_recorder_running
            else "START_FAILED",
        }
    )
    manifest_path = write_active_equity_manifest(state, equity_output)
    state["equity_manifest"] = str(manifest_path.relative_to(PROJECT_ROOT))
    atomic_write(STATE_PATH, state)
    print(json.dumps(status_payload(), indent=2))
    if not running or not recorder_running or not entry_recorder_running:
        if entry_recorder_running:
            os.kill(entry_recorder.pid, signal.SIGTERM)
        if running:
            os.kill(process.pid, signal.SIGTERM)
        raise SystemExit(3)


def status(_args) -> None:
    print(json.dumps(status_payload(), indent=2))


def stop(_args) -> None:
    state = load_state()
    if not state or not process_running(state.get("pid")):
        print(json.dumps({"status": "NOT_RUNNING"}, indent=2))
        return
    if recorder_process_running(state.get("equity_recorder_pid")):
        os.kill(int(state["equity_recorder_pid"]), signal.SIGTERM)
        for _ in range(50):
            if not recorder_process_running(state.get("equity_recorder_pid")):
                break
            time.sleep(0.1)
    if entry_recorder_process_running(state.get("entry_execution_recorder_pid")):
        os.kill(int(state["entry_execution_recorder_pid"]), signal.SIGTERM)
        for _ in range(100):
            if not entry_recorder_process_running(state.get("entry_execution_recorder_pid")):
                break
            time.sleep(0.1)
    manifest_path = seal_equity_manifest(state, status="SEALED_ON_STOP")
    os.kill(int(state["pid"]), signal.SIGTERM)
    state["status"] = "STOP_REQUESTED"
    state["stop_requested_at"] = datetime.now(timezone.utc).isoformat()
    if manifest_path:
        state["equity_manifest"] = str(manifest_path.relative_to(PROJECT_ROOT))
    atomic_write(STATE_PATH, state)
    print(json.dumps({"status": "STOP_REQUESTED", "pid": state["pid"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("start", start), ("status", status), ("stop", stop)):
        subparser = subparsers.add_parser(name)
        subparser.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
