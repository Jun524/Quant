#!/usr/bin/env python3
"""Build the Alpha #7 accumulation report from immutable collector runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.microstructure import analyze_microstructure
from quant.research.protocol import protocol_digests


def audit_complete_day_protocols(
    root: Path,
    report: dict,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict:
    """Require every timing-eligible run touching an evaluated day to be current."""
    complete_days = {
        str(item.get("day"))
        for item in report.get("coverage") or []
        if isinstance(item, dict) and item.get("complete") is True
    }
    verified_runs = (report.get("data_integrity") or {}).get("verified_runs") or []
    healthy_run_ids = {
        str(item.get("run_id"))
        for item in verified_runs
        if isinstance(item, dict)
        and (item.get("timing") or {}).get("healthy") is True
    }
    if len(healthy_run_ids) != sum(
        isinstance(item, dict) and (item.get("timing") or {}).get("healthy") is True
        for item in verified_runs
    ):
        raise ValueError("timing-eligible microstructure run IDs must be unique")
    current_protocol = protocol_digests(project_root)
    evaluated_runs: list[str] = []
    obsolete_runs: list[str] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = str(manifest.get("run_id") or "")
        if run_id not in healthy_run_ids:
            continue
        result = manifest.get("result") or {}
        try:
            started = datetime.fromisoformat(
                str(result["started_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            ended = datetime.fromisoformat(
                str(result["ended_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"run {run_id} has invalid UTC interval metadata") from exc
        if ended <= started:
            raise ValueError(f"run {run_id} has a non-positive collection interval")
        last_inclusive = ended - timedelta(microseconds=1)
        touched_days: set[str] = set()
        day = started.date()
        while day <= last_inclusive.date():
            touched_days.add(day.isoformat())
            day += timedelta(days=1)
        if not touched_days.intersection(complete_days):
            continue
        evaluated_runs.append(run_id)
        if manifest.get("protocol_sha256") != current_protocol:
            obsolete_runs.append(run_id)
    if complete_days and not evaluated_runs:
        raise ValueError("complete microstructure days have no timing-eligible source runs")
    if obsolete_runs:
        raise ValueError(
            "evaluated complete microstructure days contain obsolete protocol runs: "
            + ", ".join(obsolete_runs)
        )
    return {
        "status": "PASS",
        "complete_days": sorted(complete_days),
        "timing_eligible_runs_touching_complete_days": len(evaluated_runs),
        "obsolete_runs": [],
        "current_protocol_sha256": current_protocol,
        "guard_protocol_sha256": {
            "scripts/analyze_microstructure.py": sha256(
                Path(__file__).read_bytes()
            ).hexdigest()
        },
    }


def render_markdown(report: dict) -> str:
    totals = report["collection_totals"]
    eligible = totals["timing_eligible_runs"]
    all_runs = totals["all_verified_runs"]
    coverage = report.get("coverage") or []
    coverage_lines = "\n".join(
        f"- {item['day']}: {item['observed_seconds']:,}초, {item['coverage_ratio']:.3%}, complete={str(item['complete']).lower()}"
        for item in coverage
    ) or "- 아직 timing 적격 관측일이 없음"
    check_lines = "\n".join(
        f"- {'PASS' if item['passed'] else 'WAIT/FAIL'} `{item['name']}`: actual={item['actual']}, required={item['required']}"
        for item in report["checks"]
    )
    return f"""# Alpha #7 — 호가 불균형 기반 maker 연구

현재 판정은 **{report['status']}**이다. PASS 전까지 수익성 근거로 사용하지 않는다.

## 수집 및 무결성

- 검증된 전체 run: {report['runs']:,}개
- timing 적격 run: {report['eligible_timing_runs']:,}개
- timing 제외 run: {report['excluded_timing_runs']:,}개
- 전체 원본: depth {all_runs['depth_events']:,}건, aggTrade {all_runs['aggregate_trades']:,}건, feature {all_runs['feature_rows']:,}건, 압축 {all_runs['compressed_bytes'] / 1024**2:.2f}MiB
- 성과 입력 적격: depth {eligible['depth_events']:,}건, aggTrade {eligible['aggregate_trades']:,}건, feature {eligible['feature_rows']:,}건
- 데이터 무결성: `{report['data_integrity']['status']}`
- 프로토콜 지문 버전: 전체 {report['data_integrity']['fingerprinted_protocol_variants']}개, 평가 완전일 {report['data_integrity']['evaluated_protocol_variants']}개
- 구형 무지문 run: {report['data_integrity']['legacy_prefingerprint_runs']}개
- API 키·주문 제출: 없음

UTC와 monotonic 활성시간이 5% 또는 5초를 넘게 어긋난 run은 원본과 manifest만 보존하고 coverage·신호·성과에서 제외한다.

## Coverage

{coverage_lines}

- 완전 UTC일: {report['complete_days']}/17
- 감지 신호: {report['all_detected_signals']:,}건
- 불완전일 제외 신호: {report['incomplete_day_signals_excluded']:,}건

## 잠금 게이트

{check_lines}

고정 방식은 top-5 depth imbalance와 과거 5초 aggressive flow의 방향 일치, 3초 price-time queue,
취소 물량 미인정, maker 2bp 진입과 taker 5bp 청산, executable bid/ask markout, +100ms 지연,
시간 block bootstrap, 단일 시간대 이익 집중도 상한을 사용한다. 개발 7일·운영확인 3일·잠금 OOS
7일의 완전일을 채운 뒤에만 판정한다.

기계 보고서는 `reports/alpha007-accumulation.json`, 고정 가설과 모든 사전 수정은
`research/alpha007-preregistration.json`에 있다.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("research/microstructure"))
    parser.add_argument("--output", type=Path, default=Path("reports/alpha007-accumulation.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/alpha007-accumulation.md"))
    args = parser.parse_args()
    report = analyze_microstructure(args.input)
    report["complete_day_protocol_audit"] = audit_complete_day_protocols(
        args.input, report
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    # Refresh the additive feasibility receipt after the primary report. This
    # stricter gate cannot alter or rescue the preregistered 7bp verdict.
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "alpha007_external_cost_stress.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )


if __name__ == "__main__":
    main()
