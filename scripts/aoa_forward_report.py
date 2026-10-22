#!/usr/bin/env python3
"""Seal and report the AOA locked holdout observation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.aoa_forward import audit_aoa_forward


def _markdown(report: dict) -> str:
    metrics = report["metrics"]
    return f"""# AOA 잠금 역사적 홀드아웃 결과

- 상태: **{report['status']}**
- 실거래 권한: **{report['live_order_authorization']}**
- 평가 구간: `{report['integrity']['exact_timerange']}` (종료시각 제외)
- 표본 시작 전 등록 여부: `{str(report['integrity']['registration_precedes_sample']).lower()}`
- 자연거래: {metrics['organic_trades']}건
- 순손익: {metrics['net_pnl']:.6f} USDT ({metrics['net_return'] * 100:.4f}%)
- 완전월: {metrics['complete_months']}개월
- 최대낙폭: {metrics['maximum_drawdown'] * 100:.4f}%

동결 전략·모형·설정, 백테스트 내부 전략 소스, 실행 메타데이터, 사용한 시장자료 해시가
모두 일치했다. 하지만 등록일은 평가 구간 종료 뒤이므로 이것은 엄밀한 전향 표본이 아니라
사후 봉인한 미열람 역사적 holdout이다. 또한 15일 동안 거래가 한 건도 없어 수익성·비용
내구성·월 10% 목표에 관한 정보는 얻지 못했다. PASS는 허용되지 않으며 파라미터나 방향을
바꾸지 않는다. 향후 완전히 새로운 기간을 시작 전에 다시 봉인해 관측하지 않는 한 이 결과를
전향 증거로 간주할 수 없다.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=PROJECT_ROOT / "research" / "aoa-forward-preregistration.json",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=PROJECT_ROOT / "reports" / "aoa-forward-locked.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=PROJECT_ROOT / "reports" / "aoa-forward-locked.md",
    )
    parser.add_argument("--data", type=Path, action="append", required=True)
    args = parser.parse_args()
    report = audit_aoa_forward(
        project_root=PROJECT_ROOT,
        preregistration_path=args.preregistration,
        result_path=args.result,
        data_paths=args.data,
    )
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "organic_trades": report["metrics"]["organic_trades"],
                "output": str(args.json_output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
