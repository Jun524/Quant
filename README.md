# Binance Spot + Futures Quant Starter

Binance 현물 시장을 대상으로 한 **백테스트 → 편향 검사 → 드라이런 → 소액 실거래**용
Freqtrade 프로젝트입니다. 기본 설정은 실제 주문을 내지 않는 `dry_run: true`이며 시작 상태도
`stopped`입니다.

> 이 저장소의 전략은 연구 기준선이지 수익을 보장하는 매매 추천이 아닙니다. 과거 성과는
> 미래 성과를 보장하지 않으며, 손실 가능한 자금만 사용해야 합니다.

## 운영 목표와 위험 원칙

이 프로젝트는 Binance USDT-M 선물에서 비용·슬리피지·펀딩·데이터 무결성을 반영한 전략을
연구합니다. 목표는 월간 수익률을 맞추는 것이 아니라 **자본 보전, 강제청산 회피, 규칙 준수**의
제약 아래 장기적인 비용 후 양의 기대값을 검증하는 것입니다. 수익은 검증된 구조의 결과이며,
검증 전에는 실거래를 금지합니다.

이 문서는 목표와 원칙만 선언합니다. 상위 연구 헌장은 [RESEARCH.md](RESEARCH.md),
변경 가능한 숫자 한도·경보/즉시 정지·감사·재개 절차는 버전 관리되는
[RISK_POLICY.md](RISK_POLICY.md), 기계 검증용 정책 값은
[`research/risk-policy.json`](research/risk-policy.json)에 둡니다. `Risk Policy`가 수치의
유일한 기준이며, 기존에 봉인된 실험은 당시의 사전등록을 그대로 보존합니다.

## 현재 판정: 실거래 금지

2026-09-17에 Binance 공개 데이터로 실행 호환성과 편향 검사를 마쳤습니다. 최초 1시간봉
롱·숏 기준선은 탈락했고, 개선한 4시간봉 롱 전용 후보는 개발 및 검증 구간의 최소 기준을
통과했습니다. 다만 검증 구간 확정 거래는 15건뿐이어서 **실거래 승격이 아니라 dry-run 연구
후보**입니다. 구체적인 수치와 판정은 `RESEARCH.md`에 기록했습니다.

## 왜 이 구성인가

- **Freqtrade**: Binance 지원, 데이터 다운로드, 백테스트, dry-run, 주문/리스크 관리,
  Web UI가 한 프로젝트에 들어 있습니다.
- **고정 이미지 `2026.8`**: 실행 환경이 갑자기 바뀌지 않도록 버전을 고정했습니다.
- **4시간 Donchian 추세 돌파**: 40일 돌파/20일 청산으로 회전율과 수수료 노출을 줄였습니다.
- **현물 롱온리**: 레버리지와 청산 위험을 배제한 첫 단계입니다.
- **정적 종목 목록**: 과거 백테스트에서 현재 거래대금 순위를 사용하는 생존 편향을 줄입니다.

## 1. 준비

macOS에서는 Docker Desktop을 설치하고 실행합니다.

```bash
brew install --cask docker
open -a Docker
docker version
```

이미 Docker가 있다면 다음으로 넘어갑니다.

## 2. 설정 검증 및 시세 데이터 받기

API 키 없이 실행할 수 있습니다.

```bash
make pull
make validate
make download TIMERANGE=20220101-
```

상장 시점이 늦은 종목 때문에 데이터가 짧을 수 있으므로 `make download` 로그를 확인합니다.

## 3. 연구 검증

수수료를 편도 0.1%로 명시해 보수적으로 백테스트합니다.

```bash
make backtest TIMERANGE=20220101-20251231
make lookahead TIMERANGE=20220101-20251231
make recursive TIMERANGE=20220101-20251231
```

그다음 데이터 기간을 최소 세 구간으로 분리해서 확인합니다.

1. 개발 구간: 전략 아이디어와 파라미터를 정합니다.
2. 검증 구간: 개발 중 보지 않은 데이터로 한 번만 평가합니다.
3. 워크포워드: 여러 시점을 이동하면서 재현되는지 확인합니다.

단순 수익률보다 최대 낙폭, 거래 수, Profit Factor, Sharpe/Sortino, 시장 보유 대비 성과,
종목별 쏠림을 함께 봅니다. 백테스트는 캔들 내부 체결과 슬리피지를 완벽히 재현하지 않습니다.

## 4. 드라이런

```bash
make dry-run
make logs
```

브라우저에서 <http://127.0.0.1:8080>을 열고 로그인합니다. 기본 로그인은 로컬 테스트용일
뿐이므로 `.env.example`을 `.env`로 복사한 뒤 암호와 JWT secret을 바꾸는 것이 좋습니다.
봇은 `initial_state: stopped`라서 UI에서 명시적으로 시작해야 합니다.

연구 기준을 통과한 새 전략에 한해서 최소 4~8주 동안 다음을 확인합니다.

- 백테스트와 실제 신호/체결 차이
- 주문 미체결, 네트워크 재접속, 거래소 점검 시 동작
- 수수료와 슬리피지 이후 성과
- 연속 손실과 최대 낙폭이 예상 범위 안인지

## 5. 실거래 전 체크리스트

1. Binance 전용 서브계정을 만듭니다.
2. API 키는 **현물 거래와 읽기만** 허용하고 출금 권한은 절대 켜지 않습니다.
3. 가능하면 고정 IP 화이트리스트를 설정합니다.
4. `.env`에 키를 넣고 Git에 커밋되지 않는지 확인합니다.
5. `user_data/config.json`의 `dry_run`을 `false`로 바꿉니다.
6. `stake_amount`를 충분히 작은 값으로 낮추고 소액으로 시작합니다.
7. 실거래 DB 파일명을 dry-run DB와 분리합니다.

API 키 예시:

```bash
cp .env.example .env
# .env를 편집하되 화면 공유, 로그, Git에 노출하지 않습니다.
```

실거래 전에는 `docker-compose.yml`의 DB 경로도 다음처럼 바꿉니다.

```text
sqlite:////freqtrade/user_data/tradesv3.live.sqlite
```

## Binance USDT-M 선물

현물과 완전히 분리된 선물 구성을 추가했습니다. 자세한 위험·비용·계정 설정은
`FUTURES.md`를 먼저 읽으세요.

```bash
make futures-validate
make futures-download TIMERANGE=20210101-
make futures-backtest TIMERANGE=20220101-20260901
make futures-cost-stress TIMERANGE=20250101-20260901
make futures-lookahead TIMERANGE=20220101-20260901
make futures-recursive TIMERANGE=20220101-20260901
# 두 검사를 현재 프로토콜과 원본 출력 해시에 결박해 한 번에 봉인
make alpha001-bias-audit TIMERANGE=20220101-20260901
# 백테스트 종료 강제청산을 제외한 확정 거래 지표
make futures-closed-metrics RESULT=user_data/backtest_results/<결과파일>.zip
```

선물 dry-run은 별도 컨테이너와 UI 포트를 사용합니다.

```bash
make futures-dry-run
make futures-logs
# UI: http://127.0.0.1:8081
```

선물 기본 후보는 BTC 일봉이 50일·200일 EMA 모두에서 강세일 때만 4시간봉 롱 돌파를 허용하고,
포지션당 계획 손실은 8%에서 제한합니다. 주문 금액은 계좌 위험 예산 0.4%를 기준으로 계산하며,
50 USDT는 상한입니다. 설정은 `isolated`, 1배 레버리지, 롱 전용, 최대 2포지션입니다. 선물 봇은
계속 `dry_run: true`, `initial_state: stopped`입니다. 잠금 검증 거래가 7건뿐이므로 실거래로
전환하면 안 됩니다.

## 주요 파일

- `user_data/config.json`: 거래소, 종목, 자금, dry-run 설정
- `user_data/config.futures.json`: 격리마진 USDT-M 선물 설정
- `user_data/strategies/TrendBreakoutStrategy.py`: 진입/청산/보호 규칙
- `user_data/strategies/FuturesTrendStrategy.py`: 탈락한 2배 롱·숏 기준선
- `user_data/strategies/FuturesSlowTrendStrategy.py`: 1배 저회전 연구 후보군
- `user_data/strategies/FuturesBtcRegimeStrategy.py`: BTC 장기 국면 필터, 8% 손실 상한, 계좌 위험 예산을 적용한 dry-run 기본 후보
- `scripts/live_preflight.py`: 실거래 전 구조·비밀값·설정 실수를 차단하는 검사
- `scripts/audit_binance_api_permissions.py`: Binance에서 현재 키 권한을 읽기 전용으로 조회하고 최소권한 영수증을 만드는 검사
- `scripts/audit_binance_futures_account.py`: 주문 없이 선물 계정 모드·포지션·미체결 주문·종목별 격리/레버리지를 검증하는 검사
- `user_data/config.futures.live.example.json`, `.env.live.example`, `docker-compose.live.example.yml`: 실거래용 비밀 분리 템플릿
- `IMPROVEMENTS.md`: 적용한 개선과 다음 실험 우선순위
- `scripts/closed_trade_metrics.py`: 종료 강제청산을 분리한 성과 검사
- `docker-compose.yml`: 고정 버전 실행 환경
- `Makefile`: 반복 가능한 연구·운영 명령
- `PLATFORM.md`: 멀티전략 플랫폼 Phase 1~2 기반(데이터 검사·비용·등록부·Universe·Regime) 운영 문서
- `research/registry.json`, `reports/`: 실험 가설과 PASS/REVIEW/FAIL 근거를 보존하는 연구 기록
- `scripts/sync_alpha007_registry.py`: 진행 중 결과를 원본 실험 수정 없이 append-only 이벤트로 동기화한다. 전체 기계 리포트는 `research/evidence/alpha007/<sha256>.json.gz`에 내용 주소 방식으로 봉인하고, 등록부에는 판정용 요약과 해시만 기록해 장기 수집 중 파일 팽창을 제한한다.
- `quant/research/aoa_history.py`, `scripts/analyze_aoa_history.py`: AOA 공개 체결을 주문·포지션 단위로 재구성하는 읽기 전용 분석기
- `user_data/strategies/AoaBehaviorPrototype.py`: 공개 기록의 반복 행동만 옮긴 BTC 전용 연구 프로토타입(기본 봇과 미연결)
- `quant/research/aoa_behavior.py`, `user_data/strategies/AoaDistilledPolicyPrototype.py`: 시간순으로 학습한 거래 시점·방향 정책과 Binance 전이 연구 전략(기본 봇과 미연결)
- `quant/research/aoa_forward.py`, `scripts/aoa_forward_report.py`: 사후 봉인한 AOA 미열람 역사적 홀드아웃의 전략·설정·결과·시장자료 해시 감사
- `quant/execution/maker_quality.py`, `scripts/maker_execution_report.py`: 실제 maker 체결률·미체결 비용·역선택 검증기
- `quant/execution/shadow_maker.py`, `scripts/shadow_maker_probe.py`: 주문 없는 공개 호가/체결 queue probe
- `scripts/collect_microstructure.py`, `quant/research/microstructure.py`: Binance 공식 순번을 검증하는 무주문 실측 수집·Alpha #7 평가
- `quant/research/monthly_target.py`, `MONTHLY_TARGET.md`: 과거 월간 목표 연구의 기록. 신규 후보의 승격 목표는 `RISK_POLICY.md`의 비용 후 기대값·자본보전 게이트를 따른다.
- `scripts/capital_preservation_pipeline.py`: 신규 후보의 잠긴 사전등록, 개발/후속 구간과 각각의 비용 스트레스, 실행 계약, 무결성 증거, 현재 Risk Policy를 SHA-256으로 결박해 역사 진단을 재현하는 경로. 이 결과는 승격·주문 권한을 만들지 않는다.
- `scripts/research_pipeline.py`: 과거 일반 GatePolicy 연구 기록용 레거시 경로. `--legacy-protocol`을 명시하지 않으면 실행되지 않으며 현재 자본보전 정책 증거로 사용할 수 없다.
- `quant/research/risk.py`: 군집 손실을 보존한 7일 block bootstrap 파산확률·MDD stress
- `quant/research/equity.py`: 끊김 없는 UTC mark-to-market 자산곡선 검증과 월별/MDD 재계산
- `quant/research/portfolio_performance.py`, `PORTFOLIO_PROMOTION.md`: 포트폴리오 폐쇄거래·일별 노출·MTM 손익 구성요소를 원본에서 재계산하고 비용 스트레스와 자산 증감을 대조하는 승격 감사
- `research/portfolio-trades.template.csv`: 역할별 사전등록 체결비용 하한을 적용할 폐쇄 거래 원장 헤더
- `research/portfolio-exposure.template.csv`: 거래 중첩 재계산과 대조할 UTC 일별 최대 gross/net 레버리지 원장 헤더
- `quant/research/portfolio_execution.py`, `scripts/portfolio_execution_audit.py`: 모든 롱·숏 진입/청산/보호주문 경로의 봉인 호가·aggTrade에서 체결 여부와 역할별 수수료·slippage·markout 비용을 다시 계산하는 포트폴리오 체결 감사
- `quant/research/signal_diagnostics.py`, `scripts/signal_diagnostics_report.py`: Qlib식 연구 흐름에서 가져온 횡단면 Rank IC·IC information ratio·상하위 gross spread·신호 순위 자기상관·회전율 진단. 미래수익 구간이 겹치면 선언된 구간에서 HAC(Newey–West) lag를 보수적으로 유도해 순진한 t값의 과신을 드러내며, 원자료·프로토콜 해시를 남긴다. 비용·체결을 포함하지 않으므로 승격에는 사용할 수 없다.
- `quant/execution/bounded_notional.py`: 거래소 수량 단위 때문에 최소 실행 주문이 연구·실거래 위험 상한을 넘는지 판정하는 순수 sizing gate. 상한 초과는 `RISK_CAP_VETO`이며, 주문액을 자동 증액하지 않는다.
- `scripts/audit_futures_order_feasibility.py`: live preflight 직전에 Binance 공개 필터·호가를 봉인해 각 화이트리스트 종목의 최소 실행 주문이 정책 최대 명목가 안에 드는지 확인한다. `futures-live-preflight`는 이 영수증이 15분 이내의 PASS가 아니면 실패한다.
- `research/signal-diagnostic.template.csv`: 인과 시각과 미래수익 구간을 명시하는 신호 진단 입력 스키마
- `quant/research/funding_dispersion_signal_panel.py`, `scripts/build_alpha013_signal_panel.py`: Alpha #13의 봉인된 원자료에서만 신호·미래수익 panel을 재구성한다. 기존 Alpha #13의 판정이나 사전등록을 바꾸지 않으며, 미래 7일 라벨이 완결되기 전에는 빈 panel과 `ACCUMULATING` 영수증만 생성한다.
- `scripts/record_mtm_equity.py`, `quant/monitoring/equity.py`: 주문 없는 드라이런 MTM 원자료 수집과 인과적 일별 변환
- `scripts/run_universe_scheduler.py`, `scripts/universe_status.py`, `quant/data/universe_history.py`: Alpha #15에만 적용되는, 매일 00:30 UTC 전체 USDT-M point-in-time Universe를 해시·manifest 체인으로 봉인하는 180일 연속 축적 게이트. 일반 후보의 역사 진단·30/60/90일 전진 검토를 지연시키지 않는다.
- `user_data/strategies/FundingExtremeReversalStrategy.py`: 개발 단계에서 종료된 Alpha #5 연구 후보
- `user_data/strategies/CrossSectionalSwingStrategy.py`: 개발 단계에서 종료된 Alpha #6 횡단면 스윙 후보
- `user_data/strategies/TimeSeriesMomentumStrategy.py`: 개발 단계에서 종료된 Alpha #8 월간 절대 모멘텀 후보
- `reports/alpha008-diagnostic.md`: Alpha #8 비용·펀딩·방향별 실패 근거와 잠금구간 미개봉 기록
- `quant/research/funding_carry.py`, `reports/alpha009-diagnostic.md`: 실제 mark funding과 비용을 분리한 Alpha #9 시장상대 캐리 실패 검증
- `reports/aoa-distilled-policy.md`: 워뇨띠 행동 증류 결과, 실패한 개선, 비용별 판정과 다음 승격 조건

## 다음 개발 순서

신규 연구의 통과 기준은 월 10%가 아니라 비용 후 기대값과 자본보전 게이트다. Alpha #5와 #6도 개발 게이트를
통과하지 못했으므로 다음 작업은 아래 순서로 진행한다.

1. 공개 depth/aggTrades로 shadow maker 체결률·미체결 기회비용·역선택 표본 축적
2. 표본 전에 고정한 미시구조 가설만 개발하고 OHLCV 스캘핑 백테스트는 근거로 쓰지 않기
3. 독립 Alpha가 둘 이상 게이트를 통과한 뒤에만 포트폴리오/리스크 예산 결합
4. 미래 dry-run 월 12개와 자연 청산 100건 이상 확보
5. 수수료 2배·슬리피지·lookahead·recursive를 모두 통과한 뒤 소액 실거래 검토

신규 횡단면 가설은 거래 규칙을 만들기 전에 `make signal-diagnostics
SIGNAL_PANEL=<csv> SIGNAL_DIAGNOSTIC_OUTPUT=<json>`으로 예측력의 방향·안정성·회전율을
확인할 수 있다. 입력 열은 `decision_at,forward_start_at,forward_end_at,symbol,signal,forward_return`
순서로 고정하며 각 미래수익 구간은 결정시점보다 뒤에서 시작해야 한다. 이 결과는 후보 선별용
진단일 뿐 손익, 비용 후 edge, OOS PASS 또는 실거래 승격을 뜻하지 않는다.

Alpha #13은 일반 CSV 입력 대신 `make alpha013-signal-panel`로 동일한 봉인 가격·펀딩 원자료에서 panel과 영수증을 만든다. `alpha013-status`는 panel 바이트·행수·헤더·원자료 인벤토리·프로토콜 해시까지 다시 확인하므로, 하나라도 달라지면 `INVALID`로 표시한다. `READY_FOR_SIGNAL_DIAGNOSTIC`이 된 뒤에만 그 CSV를 `signal-diagnostics`에 입력할 수 있다. 이 결과도 원가격의 미헤지 gross return일 뿐, stop·수수료·슬리피지·funding·실제 체결을 포함하지 않으므로 Alpha #13의 별도 비용 후 평가를 대체하지 않는다.

Funding 이벤트의 `fundingTime`은 결제 시각일 뿐, 해당 값이 결정 전에 로컬에 도착했다는 증명이 아니다. `make alpha013-decision-availability-audit`는 각 월요일 04:00 결정에 대해 05:00 진입 전 생성된 별도 수신 영수증을 요구한다. 영수증이 없으면 `CAUSAL_AVAILABILITY_VETO`이며, 나중에 REST에서 재구성한 funding은 신호 증거로 인정하지 않는다.

`make goal-readiness`는 위 과정을 `reports/goal-readiness.json`과 `.md` 한 장으로 합친다.
연구 프로세스 상태, 독립 PASS sleeve 수, 결합 비용 후 장기 기대값·낙폭·파산위험·비용·주문경로 게이트를
서로 분리하며, 캘리브레이션이나 미완성 forward 표본을 승격 증거로 계산하지 않는다. 포트폴리오
현재 승격 정책 V20은 모든 sleeve에 현재 프로토콜로 결박된 lookahead/recursive PASS도 요구한다.
승격 영수증은 manifest·결합 보고서·sleeve 보고서·실행 감사뿐 아니라 비용 후 기대값 및 포트폴리오
평가기 코드의 SHA-256도 묶으며, 준비도 보고서가 원본을 다시 해시한다.
보고서가 좋아도 별도 live preflight 전까지 주문 권한은 항상 `CLOSED`다.

Alpha #7 수집은 API 키 없이 `make microstructure-collect DURATION=3600`으로 시작하고,
`make microstructure-report`로 누적 상태를 확인한다. 시작·종료의 불완전 UTC일과 연결 여유를 감안한
권장 19일 캠페인(`HOURS=456`)은 현재 보수적 추정으로 약 25.2GiB가 필요하다. 먼저
`make microstructure-preflight HOURS=456 MIN_FREE_GB=25`를 실행한다. 캠페인 명령도 같은 검사를
자동으로 강제하며, 무결성·현재 프로토콜의 dual-clock 정상 5분 표본·완료 후 디스크 하한 중 하나라도
실패하면 시작하지 않는다. timing 불일치 run은 원본만 보존하고 coverage·신호·성과에서는 제외한다.
자동 상시 실행은 기본값으로 켜지 않는다.

승인 후 장기 수집은 `make microstructure-campaign-start HOURS=456 CHUNK_SECONDS=300 MIN_FREE_GB=25`로 분리 실행한다.
지원되는 macOS에서는 supervisor PID 동안 `caffeinate`를 붙이고, 파일 잠금으로 중복 캠페인을 거부한다.
`make microstructure-campaign-status`는 감독자·실제 수집기 PID와 상태 파일을 교차 확인하고,
최신 봉인 청크의 해시·프로토콜·타이밍·재연결 품질도 감사한다. 현재 청크 경과시간이 허용 범위를
넘으면 `RUNNING_CHUNK_STALE`, 최신 청크에 복구 가능한 문제가 있으면 `RUNNING_WITH_DATA_WARNING`,
무결성 문제가 있으면 `LATEST_CHUNK_AUDIT_FAILED`로 표시한다. 제외된 실패 청크와 남은 캠페인 시간을
함께 계산해 17개 완전 UTC 일 목표가 이론적으로도 불가능해지면 `COLLECTION_TARGET_UNREACHABLE`로 내린다. 중단이 필요하면
`make microstructure-campaign-stop`을 사용하며 현재 청크를 끝내고 manifest를 봉인한 뒤 종료한다.
강제 종료와 부분 gzip 삭제는 자동 수행하지 않는다.

Alpha #1의 부족한 미래 표본은 실주문과 분리된 로컬 forward dry-run으로 누적한다. Docker가 없어도
`make alpha001-forward-start`로 시작할 수 있고 `make alpha001-forward-status`에서 PID, 전략·설정
SHA-256, 전용 SQLite의 open/closed trade 수를 확인한다. 이 경로는 `dry_run=true`와 명시적인
자동실행 전용 preflight를 동시에 요구하고, Binance 자격증명 환경변수를 자식 프로세스에서 제거하며,
Telegram을 비활성화한다. 로컬 API는 임의 인증값과 `127.0.0.1:18081` loopback에만 제한한다. 60초마다
모의계좌 `total_bot`의 가격 MTM과 열린 거래의 비용 포함 `profit_abs`를 결합해, 진입·예상청산 수수료와
누적 펀딩을 포함한 보수적 순청산가치를 append-only MTM JSONL로 기록한다. 기록기 소실과
180초 이상 정체뿐 아니라 활성 manifest의 경로·전략·프로토콜 결박 이상도 상태를 비정상으로 내린다.
거래소 초기화 지연을 허용하되 로컬 API 시작 확인은 120초에서 fail-closed한다.
다음 실행부터 거래 SQLite도 프로토콜 지문별 파일을 사용한다. 같은 프로토콜의 장애 복구 재시작은
같은 DB를 이어 쓰고, 프로토콜이 달라지면 새 DB로 분리해 서로 다른 연구 조건의 거래가 섞이지 않는다.
각 MTM manifest에도 해당 거래 DB 경로를 결박하며 보고서가 둘의 일치를 다시 검사한다.
최종 보고서 생성 코드 자체도 프로토콜 SHA-256 집합에 포함해 수집 중 판정 로직 변경을 드리프트로 탐지한다. 드리프트나 entry-execution recorder 단절이 생기면 기존 cohort를 봉인·제외하고, 전략·위험 설정은 그대로 둔 별도 immutable cohort 문서에서만 새 표본을 시작한다.
종료는 `make alpha001-forward-stop`을 사용한다. dry-run
결과는 forward 증거일 뿐이며 비용 후 장기 기대값·MDD·파산확률·비용 게이트를 통과하기 전 실거래 승격 근거가 아니다.
`make alpha001-forward-report`는 누적 MTM과 전용 SQLite를 결합해 완전 UTC 월, MDD, 자연청산 표본,
PF, 가격·수수료·펀딩 손익, 2배 수수료 스트레스, 레버리지·MAE·청산 및 7일 block bootstrap을 갱신한다.
자연청산마다 진입·청산 명목가치에 편도 5bp 슬리피지를 각각 적용하고, 100% 비용 산출 가능성과
스트레스 후 양의 순손익을 별도 승격 조건으로 요구한다.
펀딩 스트레스는 유리한 펀딩 수익을 전부 제거하고 불리한 펀딩 비용을 2배로 적용하며,
이 조건에서도 순손익이 양수여야 한다.
봉인 세그먼트의 해시나 manifest 무결성이 하나라도 실패하면 성과값과 무관하게
`FAIL_CLOSED_DATA_INTEGRITY`로, 실행 프로토콜이 바뀌면 `FAIL_CLOSED_PROTOCOL_DRIFT`로 고정한다.
보고서는 각 현재-protocol 행의 순청산가치 산식도 다시 계산해 구성요소와 최종 equity가 다르면 거부한다.
12개 완전월과 자연청산 100건을 모두 확보하기 전에는 보고서가 항상 `ACCUMULATING`으로 남는다.
`futures-live-preflight`는 먼저 Binance의 서명된 읽기 전용 권한 조회를 실행한다. 키에는 IP 화이트리스트와
조회·선물 거래만 허용되어야 하며, 출금·내부/범용 이체·현물/마진·옵션·포트폴리오마진·FIX 권한 중
하나라도 켜져 있으면 실패한다. 감사 영수증에는 키·비밀·서명이 아니라 현재 키의 SHA-256 지문과
권한 boolean만 기록하고, 15분이 지나거나 현재 키와 지문이 다르면 사용할 수 없다.
이어 USD-M 계정이 원웨이·단일자산 모드인지, 전체 포지션과 일반/조건부 미체결 주문이 0인지,
현재 whitelist 5종목이 이미 `ISOLATED`·자동증거금 꺼짐·1배인지 읽기 전용으로 확인한다. 계정 감사
영수증도 15분·API 키 지문·현재 종목 목록에 결박된다.
또한 `EXECUTION_AUDIT=reports/maker-execution.json`을 필수로 받는다. 이 영수증은 Alpha #1의
`buy` 롱 진입만 대상으로 최소 100개 public-L2 shadow 주문과 30개 체결, maker 비중, 수수료,
5분 역선택, 미체결 기회비용을 전부 포함하고,
보수적 all-in execution drag가 사전등록한 10bp 진입비용 예산 안에 들어와야 `PASS`다. 이 예산은
CLI 입력이 아니라 `research/promotion-policy.json`에서만 읽는다. 원자료·현재
프로토콜 SHA-256 결박이 다르거나 30일보다 오래되면 live preflight는 거부한다.
장기 수집 중 정상 재시작이 생기면 같은 프로토콜의 봉인 세그먼트를 전부 합산한다. 일부 세그먼트를
누락하거나 `FAILED`/활성 manifest를 섞거나 중복 signal ID가 있으면 보고서 생성을 거부하므로,
재시작을 허용하면서도 성과가 나쁜 구간만 빼는 선택 편향은 허용하지 않는다.
영수증 종목은 live whitelist와 정확히 같아야 하고 각 종목이 최소 20진입·6체결 검사를 따로 통과해야 한다.
진입은 `price_side=same`으로 maker를 지향하지만 청산은 `other`, stop은 market으로 즉시성을 우선한다.
`make shadow-maker-probe SYMBOL=ETHUSDT SIDE=buy QTY=...`처럼 종목을 지정할 수 있고, 체결이
추정된 경우 기본 300초를 더 관측해 `mid_after_5m`을 기록한다.
Alpha #1 forward 관리자는 Freqtrade의 실제 초기 롱 주문시도 로그를 1초 간격으로 감시하고,
발견 즉시 동일 stake(최대 50 USDT)뿐 아니라 로그에 기록된 실제 limit price까지 그대로 재현해
공개-L2 shadow probe를 시작한다. 재조회 시 그 가격이 이미 ask를 가로질렀다면 maker로 꾸미지 않고
가시 호가를 소진한 taker 체결과 taker 수수료로 기록한다. 100레벨 밖의 가격은 빈 queue로 가정하지
않고 표본을 실패시킨다. 이 신호연결 표본만
실거래 승격 보고서에 사용할 수 있다.
`make shadow-entry-campaign-start`는 실행 인프라 확인용으로 실제 주문이나 API 키 없이 화이트리스트 5종목을 병렬로
각 20회씩, 50 USDT·진입대기 300초·체결후 관찰 300초로 고정해 관찰한다.
공개 REST 요청 가중치가 Freqtrade와 경쟁하지 않도록 5종목의 aggregate-trade 폴링은 15초로 제한하고,
Binance 418/429 응답은 재시도해서 차단을 늘리지 않고 즉시 실패 처리한다.
`make shadow-entry-campaign-status`에서 종목별 주문·체결·커버리지를 확인하며,
완료 원자료는 현재 프로토콜 SHA-256에 결박되지만 `calibration_only_not_live_promotion`으로 표시되어
승격 게이트에는 사용할 수 없다.
완료 후 `make execution-calibration-report CALIBRATION_MANIFEST=<manifest.json>`는 원자료 해시,
프로토콜, 고정 표본 배분과 관측 커버리지를 다시 검증해 장기 연구용 비용 참고치를 만든다. 이 보고서의
드래그에는 maker 수수료가 이미 포함되어 있어 백테스트 수수료와 이중 가산하면 안 되며, 단일 시간대의
롱 진입 표본이므로 청산·숏·stop이나 다른 시장 국면의 승격 증거가 되지 않는다.
보정 영수증은 기본적으로 manifest의 캠페인 ID를 포함한 별도 파일명으로 기록한다. 과거 사전등록에
결박된 공용 영수증을 후속 관측이 덮어쓰지 않도록 하기 위함이다.
독립 창을 예약하려면 `make shadow-entry-calibration-scheduled NOT_BEFORE=<UTC ISO-8601>`를 사용한다.
이 래퍼는 지정 시각까지 기다린 뒤 무주문 공개-L2 캠페인을 한 번 시작하고, 완료·현재 프로토콜을
확인한 경우에만 해당 manifest의 버전 영수증과 안정성 인벤토리를 생성한다. 24시간 분리 조건은
`reports/execution-calibration-stability.json`의
`next_eligible_independent_window_start_at`보다 이른 시각을 허용하지 않는 운영 규칙이며, 이 작업도
API 키·거래 endpoint·실주문을 사용하지 않는다.
그 뒤 구조·비밀값뿐 아니라 이 보고서의 최종 `PASS`, 모든 세부 게이트, 현재 프로토콜·DB 결박,
24시간 이내 MTM, 열린 거래 0건, forward 프로세스 종료를 동시에 요구한다. Compose의 API 호스트 포트도
`127.0.0.1` 또는 `::1` 바인딩만 허용한다. 하나라도 없으면 live 검토 자체가 실패한다.
또한 현재 전략·설정·보고 코드 전체의 SHA-256 집합과 정확히 결박된
`reports/alpha001-bias-audit.json`이 필요하다. `make alpha001-bias-audit`가 검사를 직접 실행하고
원본 CSV·로그의 경로와 SHA-256을 봉인하며 live preflight가 파일을 다시 해시한다. 이 영수증은
Freqtrade lookahead 검사에서 최소 20개
신호, 편향 진입·청산 0개와 recursive 검사에서 실제 `startup_candle_count=1500`의 모든 지표
최대 편차 0.10% 이하를 증명해야 한다. 과거 Experiment 0001의 결박되지 않은 PASS나 사람이 직접
쓴 상태값은 대체 증거로 인정하지 않는다. 현재 영수증은 아직 없으므로 실거래 preflight는 의도대로
닫혀 있다.
모든 장기 수집기의 핵심 상태는 `make research-health` 한 번으로 함께 확인한다. 정상은 `HEALTHY`,
복구 가능한 Alpha #7 품질 경고, Universe 재시도, 청산 텔레메트리 결측 청크는 `DEGRADED`,
프로세스·프로토콜·manifest·신선도 이상은
`ATTENTION_REQUIRED`로 반환하며 마지막 경우 종료 코드도 0이 아니다.

강제청산 데이터는 `RISK_OVERLAY_001_OBSERVED_LIQUIDATION_SHOCK`이라는 위험상태 연구로만
사전등록했다. Binance의 `!forceOrder@arr`는 종목별 1초 구간의 마지막 주문 스냅샷만 제공하므로
완전한 청산 테이프나 총 청산액으로 해석하지 않는다. `make liquidation-telemetry-start`는 주문과
API 키 없이 고정 5종목의 관측 스냅샷, 1초 mark price, 5분 open interest를 1시간 불변 청크로
수집한다. `make liquidation-telemetry-status`는 프로세스를, `make liquidation-telemetry-coverage`는
해시·프로토콜·완전 UTC일 커버리지를 검사한다. 60개 개발 완전일과 고정 게이트를 통과하기 전에는
기본 전략의 PANIC 판정이나 진입을 바꾸지 않으며, 이후에도 별도 잠금 30일 검증이 필요하다.

Alpha #7을 노트북에서 수집할 때는 `make microstructure-campaign-start HOURS=456 CHUNK_SECONDS=300`을
사용한다. 5분 청크는 절전과 네트워크 단절이 발생했을 때 제외되는 정상 구간을 제한하며, 실행 중에는
macOS의 idle/system/disk 절전을 함께 억제하고 화면 절전은 허용한다. 다만 덮개를 닫아 발생하는 절전은 보장할 수
없고 그 구간은 95% 완전일 계산에서 결측으로 남는다. 17개 완전 UTC일을 안정적으로 얻으려면 상시
전원·네트워크가 유지되는 VPS가 더 적합하다.

Alpha #7의 원래 7bp 비용 가정은 실험 판정을 위해 그대로 보존한다. 별도 플랫폼 게이트
`make alpha007-external-cost-stress`는 BTC 공개-L2 캘리브레이션에서 관측된 4.77bp 진입 드래그를
5bp로 올림해 maker 진입 5bp + taker 청산 5bp, 총 10bp로 잠금 결과를 재가격한다. 원래 PASS를
구제할 수 없고 10bp 평균·시간블록 bootstrap 하한·100ms 지연 평균 중 하나라도 0 이하이면 승격을
추가로 차단한다. 단일 시간대 캘리브레이션 자체는 승격 증거가 아니다.
17개 완전일의 터미널 결과는 `scripts/sync_alpha007_registry.py`가 모든 봉인 run을 다시 평가해
바이트 동일성을 확인하고, 보고서·분석기·동기화기·각 run manifest를 SHA-256 영수증으로 묶는다.
잠금 OOS가 원래 게이트를 통과해도 이는 shadow-maker 결과이므로 등록부에서는 `REVIEW`,
`promotion_eligible=false`다. 별도 10bp 스트레스와 실제 주문경로 드라이런 없이는 독립 PASS
sleeve 또는 실거래 근거가 될 수 없다.

주 평가가 무시하는 부분체결 포지션은 별도 `ALPHA007_PARTIAL_FILL_VETO_V1`로 처리한다. 이 정책은
잠금 OOS 전에 append-only 등록부 event로 SHA-256 사전등록되어 있으며 `make
alpha007-partial-fill-stress`가 주 보고서에 포함된 정확한 run inventory만 다시 읽는다. 미체결은 0,
양수 체결수량은 전부 실제 노출로 간주하고 3초 뒤 잔량 취소, 주문 결정 60초 뒤 실행가능 호가 청산,
왕복 10bp 비용을 적용한다. 전체·100ms 지연·long/short·시간블록 bootstrap과 부분체결 자체의
기여가 모두 고정 기준을 통과해야 한다. 이 결과는 주 PASS를 구제하거나 승격할 수 없고 오직
추가로 거부할 수만 있다. 터미널 영수증은 이 보고서·정책·평가기 해시도 함께 봉인하며, FAIL이면
상태는 `SHADOW_REVIEW_AUXILIARY_VETOED`가 된다.

Alpha #7의 실제 주문 명령 경로는 `ALPHA007_ORDER_PATH_CONFORMANCE_V1`로 별도 사전등록했다.
`quant/execution/alpha007_order_path.py`는 API 호출을 하지 않고 one-way `BOTH`, GTX LIMIT 진입,
3초 잔량 취소, 부분체결 reconciliation, 60초 뒤 reduce-only MARKET 청산, 잔여 청산 재시도 명령만
결정적으로 생성한다. 현재 정책·구현 SHA-256은 등록부 event 48에 봉인되어 있다. `make
alpha007-order-path-status`는 주 터미널 영수증, 10bp 비용, 부분체결 veto, 신선한 exchangeInfo
감사가 모두 PASS일 때만 자격증명 없는 후속 캠페인을 준비시킨다. 이 경로 자체도 실주문을 전송하지
않으며 수익성이나 실제 거래소 체결을 증명하지 않는다.

2026-10-11의 `make alpha007-exchange-filter-audit` 실측에서는 BTCUSDT `stepSize=minQty=0.001`,
`MIN_NOTIONAL=50`이고 당시 약 83,013 USDT 가격에서 최소 실행가능 주문이 약 83.01 USDT였다.
사전등록된 50 USDT 주문은 수량이 0으로 절삭되어 현재 판정은 `FAIL`이다. 이를 통과시키려고 기존
실험의 주문금액을 임의로 키우지 않는다. 주 가설이 최종적으로 살아남는 경우에만 실행가능 주문 크기를
처음부터 고정한 별도 후속 가설로 조사해야 한다.

Alpha #10은 Binance 공개 USD-M metrics의 OI·가격·펀딩·실현변동성을 결합한 사전등록형
혼잡 롱 청산위험 숏 가설이다. 2021-01-01~2024-12-31의 1,461개 일별 아카이브를 게시자
SHA-256으로 검증했다. 성과 확인 전 발견한 원본의 완전 동일 중복만 제거하고, 결손은 보간하지
않은 채 해당 불완전 피처를 신호 부적격으로 처리했다. 개발구간 95거래에서 기본 비용 후
복리수익률 -46.43%, PF 0.42, MDD 49.41%, 3% 급락 정밀도 lift 1.14로 사전 게이트를 실패했다.
가격손익부터 음수였으므로 비용 조정으로 복구할 후보가 아니며, 잠긴 OOS는 열지 않았다.
결과는 `make alpha010-sync`로 append-only 레지스트리에 반영한다.

Alpha #11은 2026년 quarter-hour 효과 연구를 실행 가능한 비중복 구조로 바꾼 forward-only
BTCUSDT 실험이다. 00:15·12:15 UTC의 첫 10초 taker 수량 불균형 부호로 1배 long/short를 정하고
12시간 보유한다. 기본 왕복 taker 수수료 10bp, 왕복 슬리피지 4bp, 실제 펀딩과 4% 보호 stop을
모두 포함한다. 등록 전 자료는 사용할 수 없으며 첫 적격일은 2026-10-09 UTC다. 60개 완전일과
자연청산 100건의 개발표본이 모든 게이트를 통과해야만 다음 60일 잠금 검증을 열 수 있다.
원자료는 Alpha #7의 시간별 재접속 스트림이 아니라 `make alpha011-download START=... END=...`로
받는 Binance 공식 일별 aggTrades와 게시자 CHECKSUM만 인정한다. 일상 갱신은
`make alpha011-update`가 등록일 이후 어제까지를 자동 계산하고 기존 파일도 다시 검증한다.
각 UTC 일자의 공식 `/fapi/v1/fundingRate` 응답도 함께 정규화·SHA-256 봉인한다.
펀딩은 실행 진입가 대비 펀딩 시점 mark notional로 계산하며, 동일 시각의
`Regular`·`Special` 요율은 각 행의 mark price로 모두 반영한다.
완료된 월의 Binance 게시자 CHECKSUM 월별 fundingRate 아카이브가 나오면 supervisor가
일별 REST `Regular` 이벤트의 시각·요율을 자동 대조한다. 수동 재검증은
`make alpha011-funding-reconcile MANIFEST=... MONTH=YYYY-MM`로 실행한다.
`make alpha011-scheduler-start`는 독립 supervisor를 시작해 6시간마다 갱신을 재시도한다.
가장 오래된 결손일부터 게시된 연속 구간만 먼저 따라잡고, 첫 미게시일에서 멈춘다.
따라서 최신일이 아직 404여도 이미 게시된 과거일은 누락하지 않는다. 상태는
`make alpha011-scheduler-status`, 로그는
`user_data/logs/alpha011-scheduler.log`에서 확인한다.
61번째 아카이브로 60일차 최종 청산까지 재현되면 고정 분석기를 돌린 뒤
`scripts/sync_alpha011_registry.py`가 같은 manifest로 전체 JSON을 다시 재생한다.
재생 결과가 바이트 단위로 같을 때만 보고서·manifest·프로토콜·등록부 이벤트를
하나의 SHA-256 영수증으로 묶는다. 개발 PASS는 잠금 검증 전이므로 `REVIEW`일 뿐이며,
성과 계산이 완료되면 개발 수집은 멈춘다. 모든 개발 게이트와 source-replay 영수증이 PASS인
경우에만 supervisor가 `make alpha011-validation-open`과 동일한 절차로 다음 UTC 자정부터
60개 완전일의 검증창을 한 번 잠근다. 검증 코드는 잠금 시점의 SHA-256에 포함되며,
마지막 자연청산을 위한 61번째 아카이브가 봉인되기 전에는 성과를 계산하지 않는다.
수동 재현은 `make alpha011-validation MANIFEST=...`로 실행한다. 검증 PASS도 곧바로 실거래
승격이 아니라 포트폴리오·비용·파산위험·실행 사전점검을 받아야 하는 sleeve `REVIEW`다.
터미널 검증 결과는 `make alpha011-validation-sync`가 잠금·manifest·프로토콜로 완전히
재생한 뒤에만 gzip 원문 증거와 append-only 등록부 이벤트 및 영수증으로 봉인한다.
`make alpha011-status`는 이 영수증 전에는 적격일·봉인 파일·결손 일자만 표시하고,
영수증 후에는 터미널 개발 판정과 source binding을 같이 표시한다.

Alpha #13은 Alpha #9의 실패 후 관찰된 가격손익을 새 메커니즘으로 해석한 후속 실험이다.
따라서 독립 발견으로 꾸미지 않고 Experiment 0009의 자식, 동일 계열 두 번째 trial,
`post_hoc_hypothesis=true`로 등록했다. 과거 Alpha #9 구간은 발견용일 뿐 어떤 승격 표본에도
포함하지 않는다. 첫 적격 결정은 전체 7일 입력이 사전등록 이후인 2026-10-19 04:00 UTC다.
매주 7일 누적 펀딩이 가장 낮은 종목을 롱, 가장 높은 종목을 숏하되 격차가 25bp 이상일 때만
각 20% 명목가치·1배로 다음 05:00 UTC 봉에 진입하고 7일 보유한다. 수수료·슬리피지·실제 펀딩,
10% 개별 stop, 1시간 순청산 MTM과 급격한 갭 청산을 모두 평가한다.

`make alpha013-scheduler-start`는 6시간마다 완전 UTC일을 확인한다. 새 날이 게시되면 5종목의
Binance USD-M 1시간 선물가격·마크가격 ZIP과 게시자 CHECKSUM을 봉인하고, 공개 REST 펀딩을
같이 저장한 뒤 `make alpha013-forward-report`와 같은 전진평가를 갱신한다. 월이 끝나 공식
fundingRate 아카이브가 나오면 시각·요율을 자동 대조하며, 수동 검사는
`make alpha013-funding-reconcile MONTH=YYYY-MM`이다. 대조 전 펀딩은 잠정값이므로 개발 게이트를
통과할 수 없다. 개발 30 적격주·60개 leg와 비용/분할기간/MDD/집중도 게이트를 모두 통과하기 전
전진 보고서는 평가한 모든 UTC일의 `manifest.json` SHA-256 목록과 그 순서화된
inventory 해시, 현재 프로토콜 해시를 함께 저장한다. 상태 검사는 이를 봉인 원자료에서
다시 계산하며, 일수가 같더라도 날짜·manifest·프로토콜이 다르면 구형 보고서로 처리한다.
각 적격주가 닫히면 `scripts/sync_alpha013_registry.py`가 정확한 보고서 바이트를 gzip으로
불변 보관하고 SHA-256에 결박된 압축 스냅샷만 append-only 등록부에 추가한다.
개발 PASS는 잠금 검증이 아니므로 등록부에서도 `REVIEW`이며 `promotion_eligible=false`다.
개발 30개 적격 주가 모두 통과하면 sealed-source 재생 영수증이 먼저 생성되고, 그 영수증에서만
다음 월요일 04:00 UTC의 26개 적격 주 검증창을 한 번 열 수 있다. 검증 중에는 손익을 공개하지
않고 닫힌 적격 주 수만 표시한다. `make alpha013-validation-open`,
`make alpha013-validation`, `make alpha013-validation-sync`가 각각 잠금·평가·원자료 재생 및
append-only 봉인을 담당하며, 검증 PASS도 `promotion_eligible=false`인 sleeve 결과다.
개발 또는 검증 종료 시 최신 월의 공식 fundingRate 아카이브만 아직 미발행이고 나머지 모든
게이트가 통과했다면 이를 전략 실패로 기록하지 않는다. 표본 수집을 멈춘 채
`WAITING_FUNDING_RECONCILIATION`으로 유지하고, 동일한 고정 표본을 월별 게시자 자료와 대조한
뒤에만 최종 PASS/FAIL을 봉인한다.
일간 아카이브는 UTC일 종료 후 24시간을 게시 유예로 둔다. 유예 안의 404는
`WAITING_PUBLISHER_ARCHIVE`, 유예를 넘긴 미게시는 `DOWNLOAD_REQUIRED`로 분리해
공식 게시 지연을 장애로 오판하지 않으면서 기한 후에는 실패 폐쇄한다.
매주 04:00 UTC 의사결정에는 **05:00 UTC 진입 전** 로컬에서 취득한 펀딩 입력 영수증이
반드시 있어야 한다. `make alpha013-decision-availability-audit`는 과거 REST 재조회로
이를 소급 보완하는 일을 허용하지 않는다. 영수증 하나라도 없으면
`CAUSAL_AVAILABILITY_VETO`가 되고, `make alpha013-causal-veto-sync`가 그 감사 보고서
바이트를 별도 gzip 증거물로 보관한 뒤 append-only 등록부에 **비손익 인과성 실패**로
기록한다. 이는 신호·비용·펀딩 PnL 실패가 아니며, 해당 실험의 전진 표본을 승격 근거로
사용할 수 없다는 뜻이다. 이 상태에서는 Alpha #13 수집 스케줄러도
`TERMINAL_CAUSAL_AVAILABILITY_VETO`로 종료되고 재시작을 거부한다.
잠금 검증 26주는 열지 않고, 이 sleeve 단독 또는 결합 포트폴리오가 현재 비용 후 기대값·자본보전 게이트를
통과하기 전에는 실거래로 연결하지 않는다.

Alpha #14는 논문의 분기물 basis factor가 Binance 무기한선물에 이전되는지만
검사한 개발 전용 진단이다. Alpha #9·#13과 역학적으로 중복되므로 독립 Alpha가
아닌 동일 `cross_sectional_funding_dispersion` 계열의 3번째 trial로 등록했다.
2022–2023년 728개 일간 스프레드에서 비용 전 상대가격 PnL부터 -241.02 USDT,
순수익률 -48.22%, PF 0.68, 최대 순청산 MTM 낙폭 53.52%로 `FAIL`이다. 펀딩
+39.87 USDT도 반전시키지 못했다. 역방향·임계값·시간·종목을 재탐색하지 않고
역사적 검증을 열지 않은 채 종료했다. 재현은 `make alpha014-development`, 등록부 동기화는
`make alpha014-sync`이다.

다음 최우선 후보인 상위 30종목 분 단위 교차예측은 아직 Experiment 0015가 아니다.
`make alpha015-readiness`가 180일 연속 point-in-time universe, 현재 프로토콜 무결성,
조기 실험 미등록을 검사한다. 진행 일수는 `reports/alpha015-readiness.json`에서 확인하며,
충족 전에는 수익 자료 다운로드·모델 적합·Experiment 등록을 모두 금지한다. 180일 충족 후에도 준비상태는 사전등록과
자료 획득만 허용하며, 실거래를 허용하지 않는다.

`make alpha015-activation-lock`은 180일을 단순 날짜 배열로 신뢰하지 않는다. 형성일
`D0…D179`의 원본 snapshot·manifest·프로토콜·eligible-symbol SHA-256을 다시 검사하고,
평가일 `D1…D180` 각각을 정확한 전일 membership에 결합한다. 그 결과의 개발 120일과
잠금 검증 60일 영수증은 최초 1회만 생성되며, 원본 재생과 다르면 실패 폐쇄한다. 현재
준비상태와 이 무결성은 `make research-health`의 `alpha015_candidate`에도 포함된다.
그 뒤에도 수익 자료 권한은 바로 열리지 않는다. `make alpha015-register`가 현재 봉인된
원문·방법론·실행비용·120/60 달력·activation lock을 정확히 재해시한 단일 사전등록
영수증을 append-only 등록부에 원자적으로 연결해야만 개발자료 획득이 허용된다. 현재처럼
180일 전에는 이 명령이 등록부와 파일을 전혀 변경하지 않고 실패 폐쇄한다.

수익 자료를 보지 않은 상태에서 인과 커널과 자료 경계는 먼저 고정했다. 각 결정은
직전 720분만 쓰고, 페널티는 앞 600분 적합·뒤 120분 순방향 검증의 1분 수익 MSE로만
선택한다. 포트폴리오 PnL로 페널티를 고르는 것은 금지한다. 공식 accepted manuscript를
SHA-256으로 봉인한 뒤 pre-data amendment에서 30종목을 5분위로 나눠 상위 6개 롱·하위
6개 숏, 10분 UTC 격자로 고정했다. 완결된 `t`분 자료로 결정하고 `t+1` 경계 전 체결을
허용하지 않으며 10분 뒤 다음 목표로 직접 전환하고 하루 마지막 목표만 전량 청산한다. 비용은 실현 L1
회전율에 기본 편도 올인 5bp(수수료 포함), 스트레스 10bp를 부과한다. Binance ZIP은
게시자 CHECKSUM과 정확한 CSV 이름을 검증한 뒤에만 봉인할 수 있고, 개발 로더에는
첫 120일만 노출된다. 마지막 60일은 개발 게이트 PASS 영수증 없이는 접근할 수 없다.
각 평가일에는 전일 membership에 포함된 모든 종목의 전일·당일 1분봉, 당일 aggTrades,
당일 funding history가 정확히 있어야 하며 중복·누락·추가 파일은 전부 거부한다.
aggTrades는 파일 내부뿐 아니라 연속 평가일의 UTC 자정 경계에서도 ID 연속성을 검사한다.
펀딩은 허용된 종목·날짜별 공개 API 응답을 canonical JSON으로 봉인하고, 보유구간
`[entry, exit)`의 각 이벤트마다 `-weight × fundingRate`를 손익에 반영한다. 잠금 검증이
정당하게 열린 뒤에는 180일 전체를 Binance 월별 `fundingRate` ZIP 및 게시자 CHECKSUM과
대조해야 하며, 이 대조 전 결과는 최종 PASS나 승격에 사용할 수 없다.
중간 10분 경계에서는 포트폴리오를 인위적으로 현금화한 뒤 재진입하지 않고 검증된 경계가격으로
시가평가한 실제 보유비중에서 새 목표로 직접 순변경한다. 첫 목표는 현금에서 진입하고 마지막
시가평가 보유분만 현금으로 청산하므로,
실제 체결되지 않는 가상 왕복비용을 만들지 않으면서 모든 실현 회전율에는 비용을 부과한다.
1분봉 수익은 해당 봉이 완전히 닫혀 관측 가능한 UTC 경계에 라벨링한다. 진입·청산 기준가는
각 경계 이후 최초 aggTrade이고, 어느 한 종목이라도 60초 안에 거래가 없으면 그 UTC 일 전체를
거부한다. 따라서 `t+1` 진입은 예측 대상이던 `t→t+1` 1분을 의도적으로 제외하며, 이 실험은
그 순위 신호가 이후 10분에도 지속되는지를 검증한다.
University of Bristol OAI-PMH가 제공한 CC BY accepted manuscript는
`research/papers/alpha015-primary-source-manifest.json`에 출처·파일 SHA-256·검토 페이지를
기록했다. 논문의 λ 선택은 재현하기에 불충분하고 4bp 비용 가정도 현재 체결증거가 아니므로,
각각 독립 순방향-MSE 규칙과 현재 측정 비용을 유지한다. 따라서 정확 복제가 아니라 원문에
정렬된 보수적 전이 실험이며, 5분 보조 결과나 폐기한 13분·decile을 사후 추가 trial로 열지 않는다.

Alpha #14 이후 78개 추가 자료를 8개 축으로 재검토했지만 Experiment 0016은 열지 않았다.
특히 비용인식 BTC XGBoost 논문은 Binance USD-M 표본을 2017년부터 사용했다고 적었으나,
Binance 공식 기록상 선물 출시와 첫 BTCUSDT 무기한선물은 2019년 9월이어서 그 수익률을
복제 근거로 채택할 수 없다. 대신 다수 시도 중 우연한 승자를 걸러내는 플랫폼 개선을 먼저
적용했다. `make selection-bias-audit`는 모든 일별 trial 열을 봉인하고 DSR 확률 95% 이상,
8분할 CSCV PBO 10% 이하를 검사한다. V16 포트폴리오 승격은 각 sleeve와 결합 포트폴리오가
이 영수증을 모두 제출하지 않으면 실패한다. 자기신고 플래그만으로는 통과할 수 없고, 잠금
표본 전에 닫힌 trial inventory의 시각·열 순서·설정 해시가 CSV와 일치해야 한다. 상세 후보 판정은
`research/alpha-candidate-screening-20261010-v2.json`에 기록돼 있다.

현재 V20에서 미래 결합 포트폴리오는 00:00 UTC 기준 365일 연속 MTM 자산가치도
필요하다. 7일·28일 연속 블록 부트스트랩의 보수적 envelope로 파산·말기손실·
비용 후 연간 양의 수익 시나리오·95% 최대낙폭을 검사한다. 7일만으로 수주 손실 군집을 누락하지
않도록 한 시나리오 게이트이며, 실제 MTM 최대낙폭·청산 0건 게이트를 대체하지
않는다. 원본과 결과는 승격 시 재해시·재계산되며, 현재 Alpha #1 동결 프로토콜에는
아무 변경이 없다. 확률 판정은 95% 단측 Wilson 상·하한을 쓰므로 Monte Carlo
점추정치가 경계선을 우연히 통과하는 문제도 보수적으로 처리한다. 사용법은
`PORTFOLIO_PROMOTION.md`에 있다.
추가로 최악 1일·7일·28일 구간을 3·4·2회 반복한 결정론적 경로의 낙폭도 10% 이하여야
한다. 이 조건은 Monte Carlo에서 드물게 추출되는 최악 군집 경로를 누락하지 않게 한다.
수수료 2배·슬리피지 2배·불리한 펀딩을 동시에 적용한 일별 자산곡선도 별도로 재생한다.
최종 손익이 양수여도 중간 파산 또는 최대낙폭 10% 초과가 발생하면 실패한다.
일별 sleeve 순 PnL contribution matrix도 추가로 봉인한다. 매일 sleeve 합계가 원본 MTM
수익률을 재현해야 하며, 상관계수·공통 관측수·수익기여 집중도는 이 원본에서
재계산된다.
최종 readiness에서는 봉인된 모든 입력으로 승격 판정 전체를 다시 실행하므로,
승격 영수증의 상태나 check만 직접 고쳐서는 라이브 준비 상태로 올라갈 수 없다.
보고서의 잠금 시작·종료도 MTM 원본 경계와 재대조하고, sleeve·기여도·회계·경계 강제종료가
모두 같은 구간을 가리켜야 하므로 사후에 시작일만 바꾸는 우회도 허용하지 않는다.
각 sleeve의 일별 순손익 원본도 초기 포트폴리오 자산 대비 비율로 별도 봉인한다. 이 원본의
합은 매일의 MTM 자산 변화와 일치하고, 기여도 열은 해당 sleeve 손익을 전일 MTM 자산으로
나눈 값과 일치해야 한다. 개별 sleeve 보고서도 자기 열의 SHA-256을 보유하므로 합계만 맞춘
가짜 저상관 기여도 분해는 승격에 사용할 수 없다.
초기 배분비중과 sleeve 순손익으로 각 sleeve의 자기자금 자산곡선과 일수익률도 다시 만든다.
자산이 0 이하가 되면 실패하며, 재구성한 수익률 SHA-256이 해당 sleeve DSR/PBO 영수증의
선택된 trial 열 해시와 같아야 한다. 결합 포트폴리오의 선택된 trial도 실제 MTM 일수익률
해시와 같아야 하므로 다른 좋은 계열로 선택편향 검사만 통과시키는 방식은 차단된다.
V16의 쌍별 상관계수는 이 자기자금 sleeve 일수익률에서 계산하고, 양의 손익 집중도는 원천
sleeve 순손익에서 계산한다. 전일 포트폴리오 자산이라는 공통 분모의 시간변화로 상관이나
승자 집중도가 유리하게 왜곡되는 것을 허용하지 않는다.
포트폴리오 manifest에는 포트폴리오와 각 sleeve의 `selection_bias_family_id` 및
`selection_bias_inventory_id`도 잠금 표본 전에 고정한다. 봉인된 DSR/PBO inventory의 두 ID가
manifest와 다르면, 수익률 해시가 같더라도 사후 inventory 교체로 간주해 승격하지 않는다.
비용 후 장기 기대값·PF·자연거래수·MAE·레버리지·청산·비용 스트레스도 MTM·거래원장·
일별 최대 노출 원본에서 재생되며 결과 JSON의 자기신고 값으로 통과할 수 없다.
거래별 진입·청산 명목가에 사전등록한 역할별 수수료와 올인 체결비용 하한을 적용하고,
gross PnL·수수료·펀딩·슬리피지를 일별 회계 원장과 항목별로 대조한다.
거래별 `레버리지 × MAE`도 20% 이하여야 하므로 일별 MTM 사이의 큰 장중 손실 후 회복으로
낙폭 기준을 우회할 수 없다.
동시에 열린 거래들의 명목가도 시간순으로 재구성해, 일별 최대 gross/net 레버리지 원장이
실제 거래 중첩에서 계산한 보수적 하한보다 작으면 승격을 차단한다.
자연거래에서 제외하는 `force_exit`은 잠금 표본 종료시각과 정확히 일치해야 하므로 손실 거래의
사후 재분류로 PF를 부풀릴 수 없다.
거래 단위 PF와 별개로 MTM 일별 회계 손익 PF도 1.20 이상이어야 하므로 거래 병합·분할에
민감한 단일 지표만으로 승격하지 않는다.
