#!/usr/bin/env python3
"""Estimate Alpha #7 campaign storage and verify a healthy collector sample."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.campaign import microstructure_campaign_preflight


def render_markdown(report: dict) -> str:
    run_lines = "\n".join(
        f"- `{item['run_id']}`: {item['duration_seconds']}초, timing={str(item['timing_healthy']).lower()}, current_protocol={str(item['current_protocol']).lower()}, healthy={str(item['healthy']).lower()}, {item['compressed_bytes'] / 1024**2:.2f}MiB"
        for item in report["sample_runs"]
    ) or "- 표본 없음"
    check_lines = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`"
        for name, passed in report["checks"].items()
    )
    return f"""# Alpha #7 — 수집 캠페인 사전점검

현재 판정은 **{report['status']}**이다. 이는 수집 자원과 표본 무결성 판정이며 전략 수익성 PASS가 아니다.

- 캠페인: {report['campaign_hours']:,}시간({report['campaign_days']:.1f}일)
- 예상 저장공간: {report['estimated_storage_gib']:.2f}GiB
- 현재 여유 공간: {report['current_free_gib']:.2f}GiB
- 완료 후 예상 여유 공간: {report['projected_free_after_gib']:.2f}GiB
- 필수 잔여 하한: {report['minimum_free_after_gib']:.2f}GiB
- 저장량 안전계수: {report['safety_multiplier']:.2f}배

## 검사

{check_lines}

## 표본 run

{run_lines}

백그라운드 실행 명령은 `make microstructure-campaign-start HOURS={report['campaign_hours']} MIN_FREE_GB={report['minimum_free_after_gib']:g}`이다.
`make microstructure-campaign-status`로 상태를 확인하고, `make microstructure-campaign-stop`은 현재
청크의 manifest를 봉인한 뒤 정상 종료를 요청한다. 캠페인은 시작 전에 같은 검사를 다시 수행하고 각
청크 전에 실제 남은 공간을 확인한다. 공개
depth·aggTrade만 저장하며 API 키를 읽거나 주문을 제출하지 않는다.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("research/microstructure"))
    parser.add_argument("--hours", type=int, default=456)
    parser.add_argument("--minimum-free-gb", type=float, default=25.0)
    parser.add_argument("--safety-multiplier", type=float, default=1.5)
    parser.add_argument("--output", type=Path, default=Path("reports/alpha007-campaign-preflight.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/alpha007-campaign-preflight.md"))
    args = parser.parse_args()
    report = microstructure_campaign_preflight(
        args.input,
        campaign_hours=args.hours,
        minimum_free_after_gib=args.minimum_free_gb,
        safety_multiplier=args.safety_multiplier,
        disk_path=PROJECT_ROOT,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
