#!/usr/bin/env python3
"""Distill causal AOA entry, direction, and quality models with time splits."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.aoa_behavior import (
    attach_episode_labels,
    build_causal_features,
    fit_chronological_behavior_models,
)
from quant.research.aoa_history import (
    fetch_bitmex_xbtusd_hourly,
    reconstruct_xbtusd_episodes,
    scan_executions,
)


def render_report(result: dict) -> str:
    action = result["action_timing"]
    direction = result["direction"]
    quality = result["quality_filter"]
    all_test, selected = quality["test_all"], quality["test_selected"]
    action_coefficients = ", ".join(item["feature"] for item in action["top_coefficients"][:5])
    direction_coefficients = ", ".join(item["feature"] for item in direction["top_coefficients"][:5])
    return f"""# AOA 행동모방 시간순 검증

## 설계

- 학습: 2018~2019
- 임계값 고정: 2020
- 잠금 평가: 2021
- 입력: 체결 전에 완료된 BitMEX XBTUSD 1시간봉만 사용
- 과제: 거래 시점, 롱/숏 방향, 수익성이 낮은 원 거래의 사전 필터링

## 거래 시점 모형

- 2021 실제 진입 기본 비율: {action['test_base_rate']:.2%}
- Average precision: {action['test_average_precision']:.3f}
- ROC AUC: {action['test_roc_auc']:.3f}
- 고정 임계값 precision/recall: {action['test_precision']:.2%} / {action['test_recall']:.2%}
- 기본 비율 대비 precision lift: {action['precision_lift_over_base']:.2f}배
- 주요 설명 변수: {action_coefficients}

## 방향 모형

- 2021 방향 정확도: {direction['test_accuracy']:.2%}
- Balanced accuracy: {direction['test_balanced_accuracy']:.2%}
- ROC AUC: {direction['test_roc_auc']:.3f}
- 단순 24시간 모멘텀 방향 정확도: {direction['24h_momentum_baseline_accuracy']:.2%}
- 2020 고정 상위 30% 확신 필터: 2021 {direction['confident_test_episodes']}개 선택,
  정확도 {direction['confident_test_accuracy']:.2%}
- 주요 설명 변수: {direction_coefficients}

## 사전 품질 필터

2020 점수 분포의 상위 30% 기준을 고정해 2021 원 거래에 적용했다.

| 구분 | 거래 | 승률 | 평균 return proxy | Net XBT | PF |
|---|---:|---:|---:|---:|---:|
| 2021 전체 | {all_test['episodes']:,} | {all_test['win_rate']:.2%} | {all_test['mean_return_proxy']:.4%} | {all_test['net_pnl_xbt']:.2f} | {all_test['profit_factor']:.3f} |
| 사전 선택 | {selected['episodes']:,} | {selected['win_rate']:.2%} | {selected['mean_return_proxy']:.4%} | {selected['net_pnl_xbt']:.2f} | {selected['profit_factor']:.3f} |

## 판정 원칙

이 결과는 워뇨띠의 비공개 규칙을 복원했다는 뜻이 아니다. 시간순 잠금 구간에서 거래 시점과
방향의 예측력이 실제로 남는지, 그리고 결과를 보지 않고 고정한 품질 필터가 원 거래의 PF를
개선하는지만 평가한다. Binance 전략으로 옮기기 전에는 이 세 과제가 모두 무작위·단순 기준선을
명확히 넘어야 한다. 원자료의 취소·미체결 주문, 전체 담보, 재량 판단은 여전히 관측할 수 없다.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("워뇨띠 매매"))
    parser.add_argument("--json-output", type=Path, default=Path("reports/aoa-behavior-model.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/aoa-behavior-model.md"))
    parser.add_argument("--model-output", type=Path, default=Path("research/aoa-behavior-model-spec.json"))
    args = parser.parse_args()

    execution_paths = sorted(args.source_dir.glob("aoa-execution-*.csv"))
    _, fills, _ = scan_executions(execution_paths)
    episodes, _ = reconstruct_xbtusd_episodes(fills)
    bars = fetch_bitmex_xbtusd_hourly(
        datetime(2018, 3, 1, tzinfo=timezone.utc), datetime(2022, 1, 2, tzinfo=timezone.utc)
    )
    features = build_causal_features(bars)
    candidates, episode_rows = attach_episode_labels(features, episodes)
    result = fit_chronological_behavior_models(candidates, episode_rows)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["source"] = "user-supplied AOA executions + BitMEX public XBTUSD 1h bars"
    result["episodes_reconstructed"] = len(episodes)
    result["hourly_bars"] = len(bars)

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_report(result), encoding="utf-8")
    args.model_output.write_text(
        json.dumps(result["deployable_model_spec"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"json": str(args.json_output), "markdown": str(args.markdown_output), "model": str(args.model_output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
