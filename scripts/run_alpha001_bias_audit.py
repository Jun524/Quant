#!/usr/bin/env python3
"""Run and seal current-protocol Freqtrade lookahead and recursive audits."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.live_preflight import (
    BIAS_AUDIT_POLICY,
    BIAS_AUDIT_SCHEMA_VERSION,
    BIAS_AUDIT_FREQTRADE_VERSION,
    BIAS_AUDIT_TIMERANGE,
    LIVE_STARTUP_CANDLES,
    LIVE_STRATEGY,
    MAXIMUM_RECURSIVE_VARIANCE_PCT,
    MINIMUM_LOOKAHEAD_SIGNALS,
)
from scripts.manage_alpha001_forward_dryrun import protocol_digests


DEFAULT_FREQTRADE = PROJECT_ROOT / ".venv-research" / "bin" / "freqtrade"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "alpha001-bias-audit.json"
DEFAULT_ARTIFACT_DIR = PROJECT_ROOT / "reports" / "bias-audit"
CONFIG = PROJECT_ROOT / "user_data" / "config.futures.json"
LOOKAHEAD_CONFIG = PROJECT_ROOT / "user_data" / "config.futures.bias-audit.json"
PAIR = "BTC/USDT:USDT"


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def relative_artifact(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT.resolve()))
    except ValueError as error:
        raise ValueError("bias-audit artifacts must be inside the project root") from error


def parse_lookahead(path: Path) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        return {}, [f"lookahead CSV cannot be parsed: {type(error).__name__}"]
    matching = [row for row in rows if row.get("strategy") == LIVE_STRATEGY]
    if len(matching) != 1:
        return {}, ["lookahead CSV does not contain exactly one live-strategy row"]
    row = matching[0]
    try:
        total_signals = int(row["total_signals"])
        biased_entries = int(row["biased_entry_signals"])
        biased_exits = int(row["biased_exit_signals"])
    except (KeyError, TypeError, ValueError):
        return {}, ["lookahead CSV contains invalid signal counts"]
    has_bias = str(row.get("has_bias", "")).strip().lower() == "true"
    if str(row.get("has_bias", "")).strip().lower() not in {"true", "false"}:
        failures.append("lookahead CSV contains an invalid has_bias value")
    if total_signals < MINIMUM_LOOKAHEAD_SIGNALS:
        failures.append(f"lookahead verified fewer than {MINIMUM_LOOKAHEAD_SIGNALS} signals")
    if has_bias or biased_entries != 0 or biased_exits != 0:
        failures.append("lookahead found biased signals")
    return {
        "passed": not failures,
        "has_bias": has_bias,
        "total_signals": total_signals,
        "biased_entry_signals": biased_entries,
        "biased_exit_signals": biased_exits,
        "biased_indicators": row.get("biased_indicators", ""),
        "artifact_path": relative_artifact(path),
        "artifact_sha256": file_digest(path),
    }, failures


def parse_recursive(path: Path) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return {}, [f"recursive output cannot be parsed: {type(error).__name__}"]
    if "No lookahead bias on indicators found." not in content:
        failures.append("recursive analysis did not clear its indicator lookahead check")

    values: dict[str, float] = {}
    for line in content.splitlines():
        if not line.startswith("│"):
            continue
        cells = [cell.strip() for cell in line.strip("│").split("│")]
        if len(cells) < 2 or cells[0].lower() == "indicators":
            continue
        value = cells[1]
        if value == "-":
            parsed = 0.0
        elif value.endswith("%"):
            try:
                parsed = abs(float(value[:-1]))
            except ValueError:
                failures.append(f"recursive variance is invalid for {cells[0]}")
                continue
        else:
            continue
        if not math.isfinite(parsed):
            failures.append(f"recursive variance is non-finite for {cells[0]}")
            continue
        values[cells[0]] = parsed
    if not values:
        failures.append("recursive output contains no indicator rows")
    maximum = max(values.values(), default=float("inf"))
    if maximum > MAXIMUM_RECURSIVE_VARIANCE_PCT:
        failures.append(
            f"recursive maximum variance {maximum:.6f}% exceeds "
            f"{MAXIMUM_RECURSIVE_VARIANCE_PCT:.2f}%"
        )
    return {
        "passed": not failures,
        "pair": PAIR,
        "startup_candle_count": LIVE_STARTUP_CANDLES,
        "maximum_abs_variance_pct": maximum,
        "indicator_abs_variance_pct": values,
        "indicator_lookahead_bias": "No lookahead bias on indicators found." not in content,
        "artifact_path": relative_artifact(path),
        "artifact_sha256": file_digest(path),
    }, failures


def run_command(command: list[str], *, output_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if output_path is not None:
        atomic_write(output_path, completed.stdout)
    return completed


def version_string(freqtrade: Path) -> str:
    completed = run_command([str(freqtrade), "--version"])
    if completed.returncode != 0:
        raise RuntimeError("Freqtrade version command failed")
    match = re.search(r"Freqtrade Version:\s*freqtrade\s+([^\s]+)", completed.stdout)
    if not match:
        raise RuntimeError("Freqtrade version could not be parsed")
    return match.group(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freqtrade", type=Path, default=DEFAULT_FREQTRADE)
    parser.add_argument("--timerange", default=BIAS_AUDIT_TIMERANGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args()

    started_protocol = protocol_digests()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    lookahead_csv = args.artifact_dir / f"alpha001-lookahead-{stamp}.csv"
    lookahead_log = args.artifact_dir / f"alpha001-lookahead-{stamp}.log"
    recursive_log = args.artifact_dir / f"alpha001-recursive-{stamp}.log"
    failures: list[str] = []

    try:
        freqtrade_version = version_string(args.freqtrade)
    except (OSError, RuntimeError) as error:
        print(f"FAIL: {error}")
        raise SystemExit(2) from error
    if freqtrade_version != BIAS_AUDIT_FREQTRADE_VERSION:
        failures.append(
            f"Freqtrade {freqtrade_version} does not match locked version "
            f"{BIAS_AUDIT_FREQTRADE_VERSION}"
        )
    if args.timerange != BIAS_AUDIT_TIMERANGE:
        failures.append(
            f"timerange {args.timerange} does not match locked range {BIAS_AUDIT_TIMERANGE}"
        )

    lookahead_command = [
        str(args.freqtrade),
        "lookahead-analysis",
        "--config", str(CONFIG),
        "--config", str(LOOKAHEAD_CONFIG),
        "--strategy", LIVE_STRATEGY,
        "--timerange", args.timerange,
        "--fee", "0.0005",
        "--minimum-trade-amount", "10",
        "--targeted-trade-amount", "30",
        "--no-color",
        "--lookahead-analysis-exportfilename", str(lookahead_csv),
    ]
    lookahead_process = run_command(lookahead_command, output_path=lookahead_log)
    if lookahead_process.returncode != 0:
        failures.append(f"lookahead command exited {lookahead_process.returncode}")
    if lookahead_csv.is_file():
        lookahead, lookahead_failures = parse_lookahead(lookahead_csv)
        failures.extend(lookahead_failures)
        lookahead["command_log_path"] = relative_artifact(lookahead_log)
        lookahead["command_log_sha256"] = file_digest(lookahead_log)
    else:
        lookahead = {"passed": False}
        failures.append("lookahead command did not produce its CSV artifact")

    recursive_command = [
        str(args.freqtrade),
        "recursive-analysis",
        "--config", str(CONFIG),
        "--strategy", LIVE_STRATEGY,
        "--timerange", args.timerange,
        "--no-color",
        "-p", PAIR,
        "--startup-candle", str(LIVE_STARTUP_CANDLES), "2000", "2400",
    ]
    recursive_process = run_command(recursive_command, output_path=recursive_log)
    if recursive_process.returncode != 0:
        failures.append(f"recursive command exited {recursive_process.returncode}")
    recursive, recursive_failures = parse_recursive(recursive_log)
    failures.extend(recursive_failures)

    ending_protocol = protocol_digests()
    if ending_protocol != started_protocol:
        failures.append("research protocol changed while bias audit was running")

    receipt = {
        "schema_version": BIAS_AUDIT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy": BIAS_AUDIT_POLICY,
        "candidate": LIVE_STRATEGY,
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "freqtrade_version": freqtrade_version,
        "timerange": args.timerange,
        "protocol_sha256": started_protocol,
        "lookahead": lookahead,
        "recursive": recursive,
        "commands": {
            "lookahead": lookahead_command,
            "recursive": recursive_command,
        },
    }
    atomic_write(args.output, json.dumps(receipt, indent=2) + "\n")
    print(f"Bias audit receipt: {args.output}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        raise SystemExit(2)
    print(
        "PASS: current-protocol lookahead and recursive evidence was sealed "
        f"(signals={lookahead['total_signals']}, max_variance={recursive['maximum_abs_variance_pct']:.6f}%)."
    )


if __name__ == "__main__":
    main()
