# 멀티전략 퀀트 플랫폼

## 현재 구현 범위 — Phase 1~3 기반

기존 Freqtrade 선물 봇을 실행 엔진으로 유지하고, 그 바깥에 연구 통제 계층을 추가했다. 이 계층은
주문을 제출하거나 기본 전략을 자동 변경하지 않는다.

| 구성 | 경로 | 역할 |
|---|---|---|
| 데이터 무결성 검사 | `quant/data/validator.py` | 누락·중복·순서·OHLC·거래량·시간 간격을 fail-closed 검사 |
| 비용 모델 | `quant/execution/cost_model.py` | 수수료·스프레드·슬리피지·펀딩·부분체결 비용을 분리 계산 |
| 결과 로더 | `quant/research/result_loader.py` | Freqtrade ZIP에서 자연 청산과 강제 종료를 분리하고 비용/펀딩을 기록 |
| 실험 등록부 | `research/registry.json` | 가설·이유·결과·판정과 진행 중 결과 갱신 이벤트를 append-only로 보존. Alpha #7의 큰 원본 리포트는 해시 기반 gzip 증거 보관소에 별도 봉인 |
| 승격 게이트 | `quant/research/gates.py` | 개발/OOS/비용스트레스/편향/표본수로 PASS·REVIEW·FAIL 판정 |
| 보고서 | `reports/experiment-*.json` | 각 평가의 재현 가능한 메트릭과 게이트 근거 |
| Universe 엔진 | `quant/data/universe.py` | 현재 시점 이후의 스냅샷을 보지 않는 point-in-time 종목 선정 |
| Universe 수집기 | `scripts/collect_universe_snapshot.py`, `scripts/run_universe_scheduler.py` | 매일 00:30~00:45 UTC에 Binance USDT-M 전체시장을 한 번만 수집하고 raw SHA-256·현재 프로토콜·이전 manifest에 봉인 (읽기 전용) |
| Universe 축적 게이트 | `quant/data/universe_history.py` | 백필 없이 전체 계약 스냅샷의 체인·고정 수집창·연속 일수·최신성을 검사하고 180일 전에는 동적 Universe를 봉인 |
| Regime 엔진 | `quant/regime/classifier.py` | 공통 BTC BULL/BEAR/RANGE/PANIC/TRANSITION 판정 |
| Edge/Cost 예산 | `quant/research/evidence.py` | 사전 등록한 거래당 gross edge가 수수료·스프레드·슬리피지·펀딩을 지불 가능한지 판정 |
| 플랫폼 Scoreboard | `reports/platform-scoreboard.{json,html}` | 상태·실패 원인·비용 손익분해·표본·회전율을 한 화면에 집계 |
| AOA 기록 분석기 | `quant/research/aoa_history.py` | 공개 BitMEX 체결을 주문·포지션 에피소드로 재구성하고 비용·강제청산·시장 문맥을 분리 |
| AOA 행동 증류기 | `quant/research/aoa_behavior.py` | 2018~2021 체결을 시간순 분할해 거래 시점·방향 정책을 학습하고 잠금 평가 |
| Maker 실행 품질 | `quant/execution/maker_quality.py` | 체결률·maker 비중·실효 수수료·미체결 기회비용·체결 후 역선택을 실측 |
| Shadow maker probe | `quant/execution/shadow_maker.py` | 공개 depth/aggTrades만으로 주문 없는 가상 queue 체결 표본을 축적 |
| 미시구조 수집기 | `scripts/collect_microstructure.py` | 공식 U/u/pu 순번으로 로컬 호가를 복원하고 raw depth·aggTrade·특징을 해시 보존 |
| Alpha #7 평가기 | `quant/research/microstructure.py` | 5단계 호가/체결 불균형, queue 미체결, 7bp 비용, 100ms 지연, 시간블록 부트스트랩을 평가 |
| 월 목표 게이트 | `quant/research/monthly_target.py` | 완전 월 기준 기하수익률·월 손실·낙폭·표본·2배 비용을 함께 판정 |
| 파산위험 엔진 | `quant/research/risk.py` | 7일 block bootstrap으로 1년 파산확률·손실확률·MDD 분포·목표 도달확률을 계산 |
| 목표 준비도 | `quant/research/goal_readiness.py` | 독립 sleeve, 월 10%, 위험, 비용, 편향, 주문경로 증거를 합치되 미완성 표본은 승격하지 않음 |
| MTM 자산 검증기 | `quant/research/equity.py` | 결과 기간과 일치하는 연속 UTC 평가자산을 검증하고 월수익·MDD·일별 위험을 재계산 |
| 드라이런 자산 기록기 | `scripts/record_mtm_equity.py`, `quant/monitoring/equity.py` | 로컬 Freqtrade `total_bot`을 읽기 전용으로 보존하고 자정 이전 값만 일별 곡선으로 변환 |
| 청산 위험 텔레메트리 | `scripts/collect_liquidation_telemetry.py`, `quant/research/liquidation_history.py` | 검열된 Binance 강제청산 스냅샷·mark price·OI를 포워드 수집하고 완전 UTC일 커버리지를 검사; 수익 알파/승격 증거로 사용하지 않음 |

## 핵심 원칙

- `PASS`는 연구 적합성일 뿐, 기본 전략·레버리지·`dry_run`·실거래 설정을 바꾸지 않는다.
- 개발 전용 결과는 `best_development_diagnostic`에만 표시하며 잠금 validation이 없는 전략을
  `best_oos`로 순위화하지 않는다. 진행 중 Alpha #7은 원본 실험을 수정하지 않고 `result_update`
  이벤트를 덧붙인다.
- OOS 자연 청산 거래 수가 30건 미만이면 다른 항목이 좋아도 `REVIEW`다.
- 손실·비용·편향 검사 하나라도 실패하면 `FAIL`이다.
- 강제 종료(`force_exit`) 거래는 자연 청산 성과와 별도로 기록한다.
- 펀딩 데이터가 없거나 데이터 검사에 오류가 있으면 해당 데이터로 백테스트를 진행하지 않는다.
- 레버리지 3배 초과, 레버리지 반영 거래 MAE 50% 초과, 강제청산 1건 이상은 월 목표 승격을 막는다.
- 현재 Universe 엔진은 시점별 거래대금·스프레드·호가 깊이 스냅샷을 입력으로 받는다. 2026-09-21과
  2026-10-06의 수동 자료는 고정 UTC 창과 프로토콜 manifest가 없으므로 진단용 legacy다. 승격 가능한
  순수 포워드 표본은 사전등록 후 2026-10-10 00:30 UTC부터 시작한다. 하루라도 놓치면 백필하지 않고
  연속일 카운트를 다시 쌓으며, 180일 전에는 동적 Universe를 최종 OOS에 사용하지 않는다. 기본
  Alpha #1 종목군도 자동으로 바꾸지 않는다.

## 명령

```bash
make research-test

# 기존 결과의 실패 원인과 비용 손익분해를 읽기 전용으로 다시 집계한다.
make platform-scoreboard

# 알파를 백테스트하기 전에 거래당 비용 예산을 검사한다. buffer <= 0이면 실패한다.
make edge-budget GROSS_EDGE_BPS=30 FEE_BPS=10 SPREAD_BPS=2 SLIPPAGE_BPS=4 FUNDING_BPS=1

# 공개 Binance API만 읽어 현재 5개 연구 종목을 불변 JSON 배치로 기록한다.
make universe-snapshot

# 수동 전체시장 수집은 진단용이다. 승격 표본은 아래 scheduler만 만든다.
make universe-snapshot-all
make universe-scheduler-start
make universe-scheduler-status
make universe-status

# 전체 USDT 무기한 계약을 대상으로 하려면 Freqtrade 런타임에서 명시적으로 실행한다.
RUNTIME_PATH=$(cat /tmp/quant-runtime-path)
"$RUNTIME_PATH/bin/python" scripts/collect_universe_snapshot.py --all-usdt-perpetual --depth-pairs 50

# Freqtrade Feather 파일은 Freqtrade 가상환경에서 실행한다.
RUNTIME_PATH=$(cat /tmp/quant-runtime-path)
"$RUNTIME_PATH/bin/python" scripts/validate_data.py \
  user_data/data/binance/futures/BTC_USDT_USDT-4h-futures.feather \
  --interval-ms 14400000

make research-evaluate \
  RESEARCH_STRATEGY=FuturesBtcRegimeRiskBudgetLong \
  HYPOTHESIS='pre-committed hypothesis' \
  REASON='why this test was run' \
  DEV_RESULT=path/to/development.zip \
  VAL_RESULT=path/to/validation.zip \
  STRESS_RESULT=path/to/cost-stress.zip
```

## Phase 3 — Edge Discovery & Evidence Expansion

Alpha #5와 #6은 개발 진단에서 종료됐고, 포트폴리오 엔진과 Alpha #1의 종목 확대는 아직 열지
않는다. 다음 연구는 아래 순서를 지킨다.

1. `make platform-scoreboard`로 기존 실험을 `NO_SIGNAL_EDGE`, `OOS_DECAY`, `FUNDING_DOMINATED`,
   `COST_DOMINATED`, `INSUFFICIENT_SAMPLE`, `CONCENTRATED_PNL` 중 하나 이상으로 기계적으로 분류한다.
   Scoreboard는 price/fee/funding/slippage 순손익분해와 보유시간·MFE/MAE·승자집중도·Regime별 PnL도
   원본 보고서에 존재하는 범위에서 보존한다. `failure_reasons`는 모든 해당 사유, `primary_failure_reasons`는
   우선순위 하나씩을 집계한다. 원래 판정은 변경하지 않는다.
2. 새 가설은 백테스트 전에 거래당 기대 gross edge와 round-trip 비용을 bps로 사전 등록하고, edge buffer가
   양수일 때만 실행한다. 이는 수익 보장이 아니라 명백히 비용을 못 이기는 가설을 조기에 거르는 장치다.
3. Universe 수집기는 00:30~00:45 UTC 고정 창에서 하루 한 배치만 만들고 raw SHA-256·수집 코드·정책·
   이전 manifest 해시에 결박한다. 수집 시점 이후 데이터를 과거 종목 선정에 쓰지 않으며, 수집 누락·
   불완전 배치는 보완하거나 재작성하지 않는다.
4. `ALPHA_001_TREND_LONG`의 1B 확장은 충분한 과거 Universe 스냅샷으로 개발/OOS 구간을 재구성할 수
   있을 때만 연다. 신호·청산·리스크 숫자는 0001과 동일해야 하며, 지금의 단일 현재 스냅샷으로는 실행 불가다.
5. 새 실험은 `--hypothesis-family`, 변경 사유(파라미터/데이터/Universe), `development_seen`,
   `validation_seen`, `locked_seen`, `locked_seen_count`를 등록부에 남긴다. 이미 본 locked 구간을 새
   전략의 최종 미관측 OOS라고 주장할 수 없다. 앞으로 축적되는 shadow/dry-run 표본만 새 최종 OOS 후보가 된다.

새 Alpha는 이 순서를 생략해 기본 전략이나 실거래로 승격할 수 없다.

## AOA 공개 체결 연구

`make aoa-analyze`는 `워뇨띠 매매/`의 대용량 CSV를 스트리밍으로 읽고
`reports/aoa-history-analysis.{md,json}`을 다시 만든다. XBTUSD 에피소드 손익은 BitMEX 역계약
공식을 사용하며, 1계약 잔여 포지션은 경제적으로 청산된 dust로 취급한다. 지갑 원장은 잔액
재계산 불일치가 존재하므로 수익률·레버리지·낙폭 추정에 쓰지 않는다.

`AoaBehaviorPrototype`은 관측된 24h/168h 방향 정렬, 짧은 되돌림, maker 지향 실행을 재현 가능한
규칙으로 옮긴 연구 코드다. 정확한 재량 신호의 복제품이 아니며, 강제청산과 불리한 물타기는
의도적으로 제거했다. 이 전략은 이미 본 자료에서 유도됐으므로 Experiment 0005로 등록하지 않고,
기본 `dry_run` 설정에도 연결하지 않는다. 고정 규칙과 향후 승격 조건은
`research/aoa-behavior-prototype.md`에 있다.

행동을 고정 규칙보다 직접 증류한 `AoaDistilledPolicyPrototype`도 연구 전용으로 추가했다.
2018~2019 학습, 2020 임계값 고정, 2021 잠금 평가에서 거래 시점 ROC-AUC 0.896, 방향 ROC-AUC
0.623을 기록했다. 그러나 이미 본 Binance 2022-06~2026-09에서는 전량 taker 비용 -0.64%,
과거 maker 비중을 가정한 혼합 비용 +0.21%에 불과했다. 후자는 실제 미체결과 역선택을 반영하지
못하며 슬리피지 여유도 체결당 0.348bp뿐이다. 따라서 Experiment 0005로 등록하거나 기본 봇에
연결하지 않는다. 전체 근거는 `reports/aoa-distilled-policy.md`에 있다.

2026-10-06에 전략·모형·설정을 해시로 동결하고 미리 지정한 2026-09-01~09-16 구간을
한 번 열었다. 등록일이 구간 종료 뒤이므로 이는 전향 표본이 아니라 사후 봉인한 미열람 역사적
holdout이다. 전량 taker 5bp/체결 가정에서 거래는 0건이었다. 따라서 손익 0은 우위가 아니라
정보가 없는 결과이며 상태는 `HISTORICAL_HOLDOUT_COMPLETE_NO_ACTIVITY`다. 백테스트 내부 전략
소스·실행 메모·입력자료까지 봉인한 영수증은 `reports/aoa-forward-locked.json`에 보존한다.
이 짧은 구간으로 PASS를 줄 수 없고 결과를 보고 방향·임계값·보유시간을 바꾸지 않는다.

월 목표 집계에서는 백테스트 전체 기간의 무거래 월도 0% 월로 포함한다. 이 기준에서 전량 taker
정책은 완전 월 51개의 기하 월수익률 -0.0125%이며 월 10% 도달 횟수는 0회다.

향후 shadow 주문은 `quant/execution/maker_execution_schema.json` 형식으로 저장하고
`make maker-execution-report EXECUTIONS=<csv|jsonl> GROSS_EDGE_BPS=<bps>`로 검사한다. 이 검사는
maker 주문을 냈다는 설정값이 아니라 실제 체결 수량, 미체결 후 가격 이동, 5분 역선택을 평가한다.
`make shadow-maker-probe SIDE=buy QTY=0.001`은 주문이나 API 키 없이 Binance 공개 호가와 체결만
관측한다. 이 표본이 충분히 쌓이기 전에는 스캘핑 전략의 비용 우위를 주장하지 않는다.

## Alpha #7 — 실측 미시구조 축적

`ALPHA_007_IMBALANCE_GUIDED_MAKER`는 첫 실측 전에 `research/alpha007-preregistration.json`으로
고정했다. 5단계 depth imbalance 절대값 0.60 이상과 직전 5초 aggressive flow imbalance 절대값
0.50 이상이 같은 방향일 때만 60초 간격으로 가상 post-only 주문을 만든다. 주문 금액은 50 USDT,
queue timeout은 3초, 비용은 maker 진입 2bp + 보수적 taker 청산 5bp다.

```bash
# API 키와 주문 없이 1시간 수집
make microstructure-collect SYMBOL=BTCUSDT DURATION=3600

# 모든 불변 run을 다시 집계
make microstructure-report

# 자동 수집은 반드시 유한 시간과 디스크 하한을 지정
make microstructure-campaign HOURS=24 MIN_FREE_GB=25

# 승인된 장기 캠페인은 터미널과 분리해 시작·조회·정상 종료
make microstructure-campaign-start HOURS=456 MIN_FREE_GB=25
make microstructure-campaign-status
make microstructure-campaign-stop
```

수집기는 Binance 공식 절차대로 WebSocket 이벤트를 먼저 버퍼링하고 REST snapshot의
`lastUpdateId`를 연결한 뒤, 매 이벤트의 `pu == 이전 u`를 검사한다. 누락되면 즉시 새 snapshot으로
재동기화하며 queue 취소 물량은 체결로 인정하지 않는다. 각 run은 gzip JSONL과 SHA-256 manifest를
새 디렉터리에 남기고 덮어쓰지 않는다.

개발 7일, 운영확인 3일, 잠금 OOS 7일을 각각 95% 이상 수집해야 한다. 잠금 OOS에서 신호 1,000건,
queue 추정 완전체결 200건, 7bp 후 60초 평균 markout과 시간블록 bootstrap 하한 및 +100ms 지연
stress가 모두 양수이고 단일 시간대 gross-profit 집중도가 20% 이하여야 다음 단계로 간다. 현재
검증된 run 6개 중 timing 적격 4개와 제외 2개가 있으며, 적격 관측은 636초·신호 10건·완전일
0일이므로 판정은 `ACCUMULATING`이다. 최신 정확한 수치는 자동 생성되는
`reports/alpha007-accumulation.md`를 기준으로 한다. 이 표본은 수익성 근거가 아니다.

현재 표본 중 가장 높은 압축속도에 1.5배 안전계수를 적용한 사전점검은 raw depth 포함 19일
저장공간을 약 25.2GiB로 추정했다. 시장 활동에 따라 더 커질 수 있으므로 캠페인을 상시 실행하기 전에
디스크 여유와 네트워크를 확인한다.
캠페인 supervisor는 1시간 단위 run과 보고서를 만들며 남은 디스크가 지정 하한보다 작으면 자동
중단한다. 시작·종료의 불완전 UTC일을 버리고도 17개 완전일을 확보하려면 최소 18일이 필요하므로,
운영 여유를 포함한 권장 캠페인은 `make microstructure-campaign HOURS=456 MIN_FREE_GB=25`다. 기본값으로
시작하지 않는다. 17개 완전일이 찬 뒤 잠금구간 신호/체결이 부족하면 기간을 늘려 표본을 채우지 않고
그 가설을 `FAIL`로 닫는다.

장기 분석은 이전·현재·다음 1시간 청크만 메모리에 유지해 5분 markout을 보존하며, 캠페인 중 전체
보고서는 기본 24개 청크마다 한 번만 재계산한다. 따라서 전체 원시자료를 매시간 처음부터 다시
메모리에 올리지 않는다.
완결 UTC일이 하나라도 생기면 보고서 작성기는 그 날에 닿은 모든 timing-적격 run이
현재 고정 프로토콜과 일치하는지 검사한다. 통합 연구 건강성 검사도 이 감사를 원시
manifest에서 다시 재생하고 저장된 영수증과 비교하며, 구형 run이나 변조된 영수증은
`COMPLETE_DAY_PROTOCOL_AUDIT_FAIL`로 전체 상태를 즉시 폐쇄한다.
보고서에 들어간 마지막 run의 종료 시각까지 존재하는 원시 manifest 전체와
`verified_runs`도 정확히 일치해야 한다. 보고서 생성 후 새로 완료된 run은 허용하지만,
이미 존재하던 run을 보고서 목록에서 누락시키는 것은 허용하지 않는다.

`make microstructure-preflight HOURS=456 MIN_FREE_GB=25`는 모든 표본 해시, 현재 프로토콜과 일치하는
5분 무재접속·dual-clock 정상 실행, 관측 최대 압축속도의 1.5배 저장량, 캠페인 후 25GiB 잔여공간을
확인한다. `READY`도 수집 자원에
대한 판정일 뿐 전략 수익성 판정은 아니다. `microstructure-campaign`도 시작 전에 이 점검을 자동
실행하고 결과를 캠페인 로그 첫 줄에 보존한다.

장기 실행 관리자는 supervisor를 새 세션으로 분리하고, macOS의 `caffeinate`가 있으면 해당 PID가
살아 있는 동안 idle sleep을 막는다. 단일 파일 잠금으로 중복 실행을 거부하고 상태·완료/실패 청크 수를
`campaign-state.json`에 원자적으로 갱신한다. 종료 요청은 실행 중 수집기를 즉시 죽이지 않고 현재 청크가
manifest를 완성할 때까지 기다린다.

## Alpha #1 공통화 회귀 검증

`FuturesBtcRegimeRiskBudgetLong`은 공통 Regime Engine의 Alpha #1 호환 모드를 사용한다.
기존 50/200 BTC 필터를 그대로 보존한 결과, 회귀 검증은 개발 구간 20거래·+8.90%·PF 2.73,
잠금 검증 7거래·+2.23%·PF 3.05로 기존 기록과 일치했다. 연구용 별칭은
`ALPHA_001_TREND_LONG`이며, 기본 dry-run 설정의 전략명은 바꾸지 않았다.

## 일별 지표

새 연구 보고서는 평균·중앙값·기하 일수익률, 양/음수 일 비율, 최고/최저 일수익률, 역사적
95%/99% 일간 VaR, 최대 연속 손실일을 포함한다. 현재 Freqtrade export의 `daily_profit`은
백테스트 종료손익 기반이므로 **실시간 mark-to-market equity가 아니다**. 실거래 equity 기준
일별 성과는 이후 shadow/dry-run 계층에서 별도로 검증한다.

## Alpha #2 사전 등록

`ALPHA_002_MEAN_REVERSION_A`는 RANGE에서만 1시간봉 24시간 중심값 대비 -1.5 표준편차,
6시간 수익률 -3%, RSI 32 이하일 때 롱으로 평균회귀를 연구한다. 청산은 중심값 회복, 24시간
보유 제한, BTC BEAR/PANIC 전환 또는 -4% 보호 손절이다. `ALPHA_002_MEAN_REVERSION_B`는
더 깊은 이탈(-2σ/-5%/RSI 27, 48시간)을 사전 등록만 했으며 A 결과를 보고 파라미터를 고르는
용도로 사용하지 않는다.

Regime v1의 RANGE는 `ADX < 25`, 20일 EMA200 기울기의 절대값 `≤ 0.5%`로 사전 정의했다.
초기 `ADX < 18`/`≤ 0.2%` 정의는 개발 구간에 RANGE가 2일뿐이어서 전략 성과가 아니라 regime
표본 자체를 평가할 수 없었다. Alpha의 진입·청산 조건은 이 보정 전후로 변경하지 않는다.

MR-A의 Experiment 0002는 개발 +0.13%이지만 잠금 검증 -1.13%, 비용 스트레스 -1.18%로
`FAIL`이다. lookahead/recursive는 통과했어도 OOS PF 0.31, OOS 9거래이므로 평균회귀 Alpha를
dry-run에 추가하지 않는다.

## Alpha #3 사전 등록 및 결론

`ALPHA_003_SHORT_TREND`는 Alpha #1의 단순 반전이 아닌 독립 가설이다. 공통 BTC `BEAR`에서만,
BTC EMA50 < EMA200 및 EMA200 기울기 하락, 종목 종가 < 자체 EMA200 및 자체 EMA200 기울기
하락, 그리고 4시간봉 20일 Donchian 하단의 **첫** 종가 이탈을 모두 만족할 때 1배 숏을 낸다.
`PANIC`은 `BEAR`가 아니므로 신규 진입하지 않는다. 청산은 10일 Donchian 상단의 첫 종가 회복,
BTC가 BEAR를 벗어남, 또는 -8% 비상 손절로 고정했다. 계좌당 계획 위험은 0.4%다.

Experiment 0003은 개발 -1.36% (17 자연 청산, PF 0.62), 잠금 OOS +0.40% (18건, PF 1.14),
OOS 2배 수수료 +0.32%로 `FAIL`이다. 개발 수익이 음수이고 OOS PF가 1.20 미만이며 OOS 표본도
30건에 미달한다. 반면 lookahead/recursive는 통과했고, OOS BEAR 기대값은 +0.224 USDT/거래,
최대 단일 승자 집중도는 26.1%로 숏 특화 두 게이트는 통과했다. 이는 승격 근거가 아니라, 실패
원인을 좁히는 진단이다.

보고서는 가격 손익·거래 수수료·펀딩 손익·순손익을 분리한다. 특히 개발 구간에서 추정 가격손익은
+1.65 USDT였지만 펀딩 -14.45 USDT와 수수료 -0.76 USDT로 순손익 -13.56 USDT가 되었다. 숏
전략에는 MAE/MFE, 진입가 기준 최대 역방향 움직임(`worst_rebound_from_entry`), 최대 단일 손실,
중앙 보유시간, 펀딩 PnL, 청산 사유 분포도 함께 보관한다. intratrade 고점·저점의 시간 순서는
Freqtrade 요약 export에 없으므로 `worst_rebound_from_entry`는 거래 전체의 정확한 역행폭이며,
최저점 이후 반등이라는 시간순서 주장은 아니다.

따라서 Alpha #3은 dry-run/기본 설정에 추가하지 않고 종료한다. Experiment 0002의 MR-A `FAIL`도
보존하며, 둘 중 어느 쪽도 결과를 보고 EMA·Donchian·손절 파라미터를 재탐색하지 않는다. 다음
독립 가설은 `ALPHA_004_VOL_BREAKOUT`으로만 연다.

## Alpha #4 사전 등록 및 결론

`ALPHA_004_VOL_BREAKOUT`은 1시간봉에서 ATR(14)/가격과 직전 24시간 범위/가격이 각각 앞선
120시간 분포의 20백분위보다 낮고, 24시간 Donchian 범위를 처음 이탈하면서 직전 24시간 평균의
1.5배 이상 거래량을 동반할 때 롱/숏을 낸다. BULL·BEAR·RANGE·TRANSITION은 진입 태그로만
저장하고, `PANIC`만 진입 금지했다. 청산은 범위 복귀, PANIC, 48시간 보유 제한 또는 -4% 보호
손절이며, 신호는 1배·0.3% 계좌 위험 예산으로 고정했다. 상세 체결은 5분봉으로 재생했다.

Experiment 0004는 개발 -4.05% (544건, PF 0.63), 잠금 OOS -0.12% (315건, PF 0.98), 2배 수수료
OOS -2.13%, 그리고 OOS에서 체결마다 추가 5bp 슬리피지를 적용하면 -2.19%로 `FAIL`이다. OOS
자연 청산 표본은 30건 기준을 넘었지만, 개발/OOS/비용/슬리피지 및 돌파 기대값 게이트가 모두
실패했다. lookahead 및 재귀 검사는 통과했다.

5분 리플레이는 모든 자연 청산 거래를 100% 커버했다. OOS 평균 MFE 0.70%, 평균 MAE 0.45%,
MFE/MAE 1.54였지만, 순수익 성공률은 35.6%이고 사전 정의한 범위 복귀형 false-breakout 비율은
100%였다. 중앙 MFE 시점은 20분, MAE 시점은 25분, 중앙 보유시간은 60분이다. 이는 MFE/MAE만
보고 손절·채널 숫자를 조정해 살릴 수 있는 결과가 아니라, 이 고정된 압축-돌파 구조의 비용 후
기대값이 음수라는 반증이다.

OOS에서는 추정 가격손익 +19.80 USDT가 거래 수수료 -20.75 USDT와 펀딩 -0.23 USDT에 뒤집혔다.
모델 슬리피지 5bp/체결은 추가 -20.75 USDT다. BULL(-5.62 USDT, 126건)와 BEAR(-0.60 USDT,
153건) 모두 음수였으며, 소표본 RANGE(+0.57 USDT, 2건)·TRANSITION(+4.46 USDT, 34건)은 새
Regime 필터를 만들 근거가 아니다.

5분 원시 데이터의 0거래량 47개 시점은 5개 종목에 공통인 거래소 유지보수 구간이었다. 이를
가격·거래량으로 보간하지 않고 `volume=0`인 체결 불가 캔들로 보존했다. Alpha #4 역시 기본
설정·dry-run 후보에 추가하지 않는다. 따라서 Phase 2에서는 Alpha #1만 `REVIEW`이고 Alpha #2~#4는
`FAIL`이므로 Portfolio Engine은 만들지 않는다.

## Alpha #5·#6 개발 진단

`ALPHA_005_FUNDING_EXTREME_REVERSAL`은 과거 90개 펀딩 이벤트의 상·하위 5%에서 반대 방향으로
8시간 보유하는 하나의 가설로 고정했다. 개발구간 1,009건에서 +1.70%였지만 PF 1.019에 그쳤고,
추정 가격 PnL +108.81 USDT를 수수료 -111.22 USDT가 거의 모두 제거했다. 사전등록 PF 1.10을
넘지 못해 검증구간은 열지 않았다.

`ALPHA_006_CROSS_SECTIONAL_SWING`은 매주 월요일 5개 종목의 30일 상대강도와 100일 절대추세로
최강 롱·최약 숏을 선택했다. 개발구간 96건에서 +1.00%, PF 1.049, 최대낙폭 5.75%였다. 가격
PnL은 +18.96 USDT였지만 수수료 -4.25, 펀딩 -4.74 USDT가 차감됐고, PF 1.10 게이트를 넘지
못해 검증구간을 열지 않았다. 완전 월 32개의 기하 월수익률은 0.0715%, 최고 월은 2.91%다.

두 후보 모두 결과를 보고 방향·종목·손절·보유시간을 바꾸지 않았다. 상세 근거는
`reports/alpha005-diagnostic.md`, `reports/alpha006-diagnostic.md`에 있다. 이제 OHLCV만으로 단기
체결을 낙관하는 Alpha를 추가하지 않고, 공개 호가·체결 shadow 표본을 먼저 축적한다.

## Alpha #14 가설 검색과 전이 결론

Alpha #14는 OKEx 분기물에서 보고된 일간 basis factor를 Binance 무기한선물에
적용한 개발 전용 transfer diagnostic이다. 펀딩 분산 Alpha #9·#13과 동일한 넓은
계열의 3번째 trial이며, 독립성이나 실거래 승격 증거를 주장하지 않는다.

2022–2023년 728개 거래에서 비용 전 가격 PnL -241.02 USDT, 순수익률 -48.22%,
PF 0.68, 기하 월수익률 -2.70%, 최대 순청산 MTM 낙폭 53.52%였다. 즉 실행
비용을 줄여 구제할 수 있는 가설이 아니다. Experiment 0014는 역사적 검증을 열지 않고
`FAIL`로 종료했다. 다음 고빈도 교차예측 후보는 180일 point-in-time universe 충족 전에
시작하지 않는다.
