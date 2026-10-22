# Alpha #7 — 호가 불균형 기반 maker 연구

현재 판정은 **ACCUMULATING**이다. PASS 전까지 수익성 근거로 사용하지 않는다.

## 수집 및 무결성

- 검증된 전체 run: 3,591개
- timing 적격 run: 3,565개
- timing 제외 run: 26개
- 전체 원본: depth 11,669,734건, aggTrade 14,070,071건, feature 11,654,204건, 압축 6943.80MiB
- 성과 입력 적격: depth 10,912,504건, aggTrade 12,870,409건, feature 10,897,173건
- 데이터 무결성: `PASS_WITH_TIMING_EXCLUSIONS`
- 프로토콜 지문 버전: 전체 3개, 평가 완전일 1개
- 구형 무지문 run: 3개
- API 키·주문 제출: 없음

UTC와 monotonic 활성시간이 5% 또는 5초를 넘게 어긋난 run은 원본과 manifest만 보존하고 coverage·신호·성과에서 제외한다.

## Coverage

- 2026-10-06: 636초, 0.736%, complete=false
- 2026-10-07: 21,602초, 25.002%, complete=false
- 2026-10-08: 28,795초, 33.328%, complete=false
- 2026-10-09: 39,289초, 45.473%, complete=false
- 2026-10-10: 84,579초, 97.892%, complete=true
- 2026-10-11: 84,333초, 97.608%, complete=true
- 2026-10-12: 84,131초, 97.374%, complete=true
- 2026-10-13: 83,846초, 97.044%, complete=true
- 2026-10-14: 83,537초, 96.686%, complete=true
- 2026-10-15: 83,168초, 96.259%, complete=true
- 2026-10-16: 82,848초, 95.889%, complete=true
- 2026-10-17: 82,849초, 95.890%, complete=true
- 2026-10-18: 82,516초, 95.505%, complete=true
- 2026-10-19: 82,455초, 95.434%, complete=true
- 2026-10-20: 82,139초, 95.068%, complete=true
- 2026-10-21: 81,773초, 94.645%, complete=false
- 2026-10-22: 26,085초, 30.191%, complete=false

- 완전 UTC일: 11/17
- 감지 신호: 15,647건
- 불완전일 제외 신호: 2,836건

## 잠금 게이트

- WAIT/FAIL `complete_collection_days`: actual=11, required=17
- PASS `locked_eligible_orders`: actual=1217, required=1000
- WAIT/FAIL `locked_queue_fills`: actual=102, required=200
- WAIT/FAIL `locked_net_60s_markout`: actual=-0.0007780721744636218, required=> 0 after 7 bps fees
- WAIT/FAIL `hour_block_bootstrap_95pct_lower_bound`: actual=-0.0008429179057585283, required=> 0
- WAIT/FAIL `plus_100ms_latency_net_60s_markout`: actual=-0.0008154595128511599, required=> 0 after 7 bps fees
- WAIT/FAIL `single_hour_profit_concentration`: actual=0.6259903971523736, required=<= 0.20

고정 방식은 top-5 depth imbalance와 과거 5초 aggressive flow의 방향 일치, 3초 price-time queue,
취소 물량 미인정, maker 2bp 진입과 taker 5bp 청산, executable bid/ask markout, +100ms 지연,
시간 block bootstrap, 단일 시간대 이익 집중도 상한을 사용한다. 개발 7일·운영확인 3일·잠금 OOS
7일의 완전일을 채운 뒤에만 판정한다.

기계 보고서는 `reports/alpha007-accumulation.json`, 고정 가설과 모든 사전 수정은
`research/alpha007-preregistration.json`에 있다.
