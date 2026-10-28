#!/usr/bin/env python3
"""Build the cumulative Alpha #1 forward dry-run evidence report."""

from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.forward_dryrun import (
    audit_mtm_segments,
    build_forward_report,
    load_forward_trades,
    load_mtm_observations,
)
from scripts.manage_alpha001_forward_dryrun import DB_PATH, STATE_PATH, protocol_digests


def audit_net_liquidation_rows(paths: list[Path]) -> dict:
    """Recompute every complete row's declared net-liquidation identity."""
    audited_rows = 0
    failures = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
            lines = content.splitlines()
            if content and not content.endswith("\n"):
                lines = lines[:-1]
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("equity_method") != "balance_total_bot_plus_open_trade_net_liquidation_adjustment":
                    raise ValueError(f"line {line_number}: unexpected equity method")
                equity = float(row["equity"])
                balance_total = float(row["balance_total_bot"])
                adjustment = float(row["open_trade_net_liquidation_adjustment"])
                funding = float(row["open_funding_pnl"])
                open_trades = int(row["open_trades"])
                if not all(isfinite(value) for value in (equity, balance_total, adjustment, funding)):
                    raise ValueError(f"line {line_number}: non-finite valuation component")
                tolerance = max(1e-9, abs(equity) * 1e-12)
                if abs(equity - (balance_total + adjustment)) > tolerance:
                    raise ValueError(f"line {line_number}: net-liquidation identity mismatch")
                if equity <= 0 or balance_total <= 0 or open_trades < 0:
                    raise ValueError(f"line {line_number}: invalid valuation domain")
                audited_rows += 1
        except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            failures.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
    return {
        "status": "PASS" if audited_rows > 0 and not failures else "FAIL_CLOSED",
        "audited_rows": audited_rows,
        "failures": failures,
    }


def audit_database_binding(integrity: dict, *, project_root: Path, expected_database: str) -> dict:
    """Require every eligible MTM segment to name the exact trade database."""
    segments = []
    for segment in integrity.get("segments", []):
        if segment.get("integrity") != "PASS" or segment.get("protocol_current") is not True:
            continue
        manifest_path = project_root / str(segment["manifest"])
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            actual = manifest.get("database_path")
            if actual != expected_database:
                raise ValueError(f"database binding mismatch: {actual!r}")
            segments.append({"manifest": segment["manifest"], "database_path": actual, "status": "PASS"})
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            segments.append(
                {
                    "manifest": segment.get("manifest"),
                    "database_path": None,
                    "status": "FAIL",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    passed = bool(segments) and all(item["status"] == "PASS" for item in segments)
    return {"status": "PASS" if passed else "FAIL_CLOSED", "segments": segments}


def enforce_data_integrity(report: dict, integrity: dict, *, protocol_current: bool = True) -> dict:
    """Make evidence and protocol integrity authoritative over performance."""
    if integrity.get("status") != "PASS":
        report["status"] = "FAIL_CLOSED_DATA_INTEGRITY"
        report["warning"] = (
            "MTM evidence integrity failed closed; this report must not be used for promotion or profitability claims."
        )
    elif not protocol_current:
        report["status"] = "FAIL_CLOSED_PROTOCOL_DRIFT"
        report["warning"] = (
            "The running protocol differs from the recorded launch protocol; this report must not be used for promotion or profitability claims."
        )
    return report


def markdown(report: dict) -> str:
    mtm = report["mtm"]
    trades = report["trades"]
    policy_scope = report["policy_scope"]
    lines = [
        "# Alpha #1 — forward dry-run 누적 보고서",
        "",
        f"현재 판정은 **{report['status']}**이다. 이 보고서는 실거래 수익을 주장하지 않는다.",
        "",
        "## 정책 적용 범위",
        "",
        f"- Cohort: `{policy_scope['cohort']}`",
        "- 현행 자본보전 정책의 승격 근거: 아니오",
        f"- {policy_scope['reason']}",
        "",
        "## 수집 상태",
        "",
        f"- MTM 관측: {mtm['observations']}",
        f"- MTM 연속성: {mtm['continuous']}",
        f"- 최대 관측 간격: {mtm['maximum_gap_seconds']}",
        f"- 완전 UTC 일 경계: {len(mtm['daily_boundaries'])}",
        f"- 완전 월: {mtm['monthly']['complete_months']} / 12",
        f"- 최신 MTM 수익률: {mtm['total_return']}",
        f"- MTM 최대낙폭: {mtm['maximum_drawdown']}",
        "",
        "## 거래 상태",
        "",
        f"- 전체 거래: {trades['all_trades']}",
        f"- 자연청산: {trades['natural_closed_trades']} / 100",
        f"- 강제청산 제외 건수: {trades['forced_closed_trades']}",
        f"- 비용 후 폐쇄손익: {trades['net_profit']}",
        f"- 거래 수수료: {trades['trading_fees']}",
        f"- 펀딩 PnL: {trades['funding_pnl']}",
        f"- 제거한 유리한 펀딩: {trades['favorable_funding_removed']}",
        f"- 불리한 펀딩 비용: {trades['adverse_funding_paid']}",
        f"- 펀딩 스트레스 후 손익: {trades['funding_stress_net_profit']}",
        f"- 2배 수수료 스트레스 손익: {trades['double_fee_net_profit']}",
        f"- 슬리피지 스트레스: 편도 {trades['slippage_bps_per_side']}bp",
        f"- 모델 슬리피지 비용: {trades['modeled_slippage']}",
        f"- 슬리피지 스트레스 후 손익: {trades['slippage_stress_net_profit']}",
        "",
        "## 승격 게이트",
        "",
    ]
    for check in report["gate"]["checks"]:
        lines.append(f"- [{'x' if check['passed'] else ' '}] {check['name']}: {check['actual']} (required {check['required']})")
    lines.extend(["", report["warning"], ""])
    return "\n".join(lines)


def main() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    observation_paths = sorted((PROJECT_ROOT / "research" / "equity" / "raw").glob("alpha001-forward-*.jsonl"))
    current_digests = protocol_digests()
    integrity = audit_mtm_segments(observation_paths, project_root=PROJECT_ROOT, current_protocol=current_digests)
    database_value = state.get("database")
    database_path = PROJECT_ROOT / database_value if isinstance(database_value, str) else DB_PATH
    database_binding = audit_database_binding(
        integrity,
        project_root=PROJECT_ROOT,
        expected_database=str(database_path.relative_to(PROJECT_ROOT)),
    )
    integrity["database_binding"] = database_binding
    if database_binding["status"] != "PASS":
        integrity["status"] = "FAIL_CLOSED"
    valuation_audit = audit_net_liquidation_rows(integrity["eligible_paths"])
    integrity["net_liquidation_rows"] = valuation_audit
    if valuation_audit["status"] != "PASS":
        integrity["status"] = "FAIL_CLOSED"
    observations = load_mtm_observations(integrity["eligible_paths"])
    trades = load_forward_trades(database_path)
    current_protocol = state.get("protocol_sha256") == current_digests
    report = build_forward_report(
        observations=observations,
        trades=trades,
        protocol_current=current_protocol,
        data_integrity={key: value for key, value in integrity.items() if key != "eligible_paths"},
    )
    report = enforce_data_integrity(report, integrity, protocol_current=current_protocol)
    report["basis"] = (
        "credential-free Binance futures dry-run with append-only, fee-and-funding-aware net-liquidation MTM observations"
    )
    report["database"] = str(database_path.relative_to(PROJECT_ROOT))
    output_json = PROJECT_ROOT / "reports" / "alpha001-forward.json"
    output_md = PROJECT_ROOT / "reports" / "alpha001-forward.md"
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
