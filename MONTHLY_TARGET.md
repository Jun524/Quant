# 비용 후 월 10% 연구 목표

월 10%를 12개월 복리로 유지하면 연 수익률은 약 213.8%다. 따라서 이 수치는 약속이나 주문
크기 설정값이 아니라 전략군 전체가 입증해야 하는 공격적 연구 목표로 취급한다.

원금 100만원이라면 한 달 목표 순이익은 10만원이고, 출금 없이 12개월 연속 달성할 때의 이론상
잔액은 약 313.8만원이다. 현재 통과 전략이 없으므로 이는 기대수익이나 예측값이 아니다.

## 승격 기준

- 완전한 미래 월 12개 이상
- 자연 청산 거래 100건 이상
- 비용 후 기하 월수익률 10% 이상
- 양수 월 비율 60% 이상
- 최악 월 -10% 이상
- 최대낙폭 20% 이하
- 7일 block bootstrap 1년 파산확률 1% 이하
- 7일 block bootstrap 1년 손실확률 10% 이하
- 7일 block bootstrap에서 월 10% 복리의 연간 환산 목표 도달확률 50% 이상
- bootstrap 최대낙폭 95백분위 20% 이하
- 개별 거래 최대 레버리지 3배 이하
- 레버리지 반영 MAE 50% 이하, MAE 자료 100% 존재
- 강제청산 0건
- Profit Factor 1.20 이상
- 수수료 2배 스트레스에서도 순수익 양수
- 현재 전략·설정·보고 코드 SHA-256에 결박된 lookahead/recursive 감사 영수증 PASS
- 실제 maker 체결률·미체결 기회비용·역선택 반영 후 순 edge 양수
- 청산손익이 아닌 mark-to-market equity 자료 존재
- 사전등록 후 시작한 잠금 미래 표본이며 표본 시작 후 파라미터 변경 0건

`quant/research/monthly_target.py`가 이 조건을 기계적으로 평가한다. Freqtrade의 월별 값은
청산손익 기반이므로 최종 승격에서는 일별 mark-to-market equity로 다시 평가해야 한다.
`quant/research/risk.py`는 7일 연속 블록을 재표본화해 5,000개의 1년 경로를 만들지만, 청산손익만
사용한 결과는 장중 미실현 손실을 누락하므로 예비 진단이다. mark-to-market 자료 없이는 게이트가
절대 `PASS`가 되지 않는다.

완전월 개수만으로는 사전등록 후 수집한 미래 자료임을 증명할 수 없다. 따라서 일반 과거
백테스트는 성과가 좋아도 진단용 `REVIEW`를 넘을 수 없고, `research/promotion-policy.json`에
사전 고정한 `locked_forward` 출처만 실거래 승격 `PASS`를 받을 수 있다.

체결 게이트는 Freqtrade가 실제로 초기 롱 주문 생성을 시도했다는 로컬 로그 이벤트에 즉시 연결된
`buy`/`price_side=same` shadow probe만 대상으로 하며,
로그의 stake와 limit price를 그대로 재현하며, 감지 시점에 가격이 시장가 가능 상태면 taker 체결과
taker 수수료로 분류한다. 청산은 `price_side=other`, 보호 stop은 market으로 유지한다. 최소 100개 가상 진입·30개 체결,
maker 비중 70% 이상, 실효 수수료 2.8bp 이하,
체결 5분 후 역선택과 미체결 기회비용 100% 커버리지를 요구한다. 수수료·슬리피지·역선택·
미체결을 모두 합친 보수적 실행 drag가 사전등록한 진입비용 예산 10bp보다 작아야 한다. 이 값은
보고서 CLI로 늘릴 수 없다. 각 가상 진입은 실제 요청 stake와 같은 크기(최대 50 USDT),
대기 300초, 체결 후 markout 300초로 고정한다. 무작위 시점의 주기적 probe는 캘리브레이션일 뿐
승격 표본으로 인정하지 않는다.
영수증은 원자료
SHA-256·현재 프로토콜·생성시각에 결박되며 30일을 넘기면 폐기된다. 공개 L2 shadow는 실제 주문
체결의 보증이 아니며, 라이브 승격 전 보수적 실행 가능성 필수 검증으로만 사용한다.
동일 프로토콜에서 재시작된 모든 봉인 실행 세그먼트를 빠짐없이 합쳐야 하며, 일부 누락·실패
manifest·중복 signal ID가 있으면 fail-closed한다.
또한 영수증의 종목 집합이 현재 live whitelist와 정확히 같아야 하며, 종목별로도 최소 20진입·6체결과
모든 실행비용 검사를 따로 통과해야 한다.

MTM 자료는 CSV 또는 JSONL의 `timestamp,equity,equity_type` 필드를 사용한다. 모든 행은
`equity_type=mark_to_market`, `equity=지갑잔고+미실현손익(수수료·펀딩 반영 후)`여야 하고,
`timestamp`는 매일 빠짐없는 `00:00:00 UTC`여야 한다. 첫날과 마지막 날도 평가한 Freqtrade
결과의 `backtest_start`·`backtest_end`와 정확히 일치하지 않으면 거부한다.

```csv
timestamp,equity,equity_type
2025-01-01T00:00:00Z,1000.00,mark_to_market
2025-01-02T00:00:00Z,997.25,mark_to_market
```

```bash
make monthly-objective \
  RESULT=user_data/backtest_results/result.zip \
  MONTHLY_STRATEGY=StrategyClass \
  STRESS_RESULT=user_data/backtest_results/result-double-cost.zip \
  SLIPPAGE_STRESS_RESULT=user_data/backtest_results/result-slippage-stress.zip \
  FUNDING_STRESS_RESULT=user_data/backtest_results/result-funding-stress.zip \
  EQUITY_CURVE=research/equity/strategy-mtm.csv
```

일반 `monthly-objective`도 전진검증 보고서와 같이 슬리피지 스트레스와
유리한 펀딩 제거·불리한 펀딩 2배 스트레스 모두를 강제한다. 파일이 빠지거나
개별 거래의 슬리피지·펀딩 커버리지가 100%가 아니면 `PASS`가 나오지 않는다.

드라이런에서는 Freqtrade의 읽기 전용 `/api/v1/balance` 중 선물 담보와 미실현손익을 합산하는
`total_bot`을 1분마다 append-only JSONL로 저장한다. 암호는 명령행이나 파일에 넣지 않고 환경변수로만
전달한다. 수집기는 응답이 `Simulated balances`가 아니면 종료하므로 실계정 자료를 실수로 섞지 않는다.

```bash
export FREQTRADE_API_URL=http://127.0.0.1:8081
export FREQTRADE_API_USERNAME='로컬 API 사용자명'
export FREQTRADE_API_PASSWORD='로컬 API 암호'
make equity-record DURATION=86400

make equity-daily \
  RAW='research/equity/raw/equity-*.jsonl' \
  OUTPUT=research/equity/daily-mtm.csv \
  MAX_AGE=120
```

일별 변환기는 각 UTC 자정 **이전**의 마지막 관측치만 고르며 120초보다 오래된 값, 빠진 날짜,
중복 시각은 보간하지 않고 실패시킨다. 따라서 자정 이후 가격을 앞날 자산값에 넣는 lookahead를 막는다.

## 현재 전략군

| 계열 | 판정 | 월 10%와의 관계 |
|---|---|---|
| Alpha #1 장기 상승추세 | REVIEW | 안정적이지만 표본·수익률 부족 |
| Alpha #2 RANGE 평균회귀 | FAIL | OOS edge 없음 |
| Alpha #3 약세 숏 추세 | FAIL | 비용·펀딩이 edge 제거 |
| Alpha #4 변동성 돌파 | FAIL | 비용 후 음수 |
| 워뇨띠 행동 증류 | FAIL for target | 51개월 기하 월수익률 -0.0125% |
| Alpha #5 극단 펀딩 역추세 | FAIL | gross edge 대비 회전 비용 과다 |
| Alpha #6 횡단면 모멘텀 스윙 | FAIL | 개발 PF 1.05, 기하 월수익률 0.0715% |
| Alpha #8 월간 시계열 모멘텀 | FAIL | 개발 -6.55%, PF 0.935, MDD 27.97%; 잠금구간 미개봉 |
| Alpha #9 횡단면 펀딩 캐리 | FAIL | 총 +63.57%이나 funding-마찰 잉여 -1.43 USDT; 가격노출 수익이라 잠금구간 미개봉 |
| Alpha #10 레버리지 혼잡 숏 | FAIL | 개발 -46.43%, PF 0.42; 가격 신호부터 음수라 잠금구간 미개봉 |
| Alpha #11 quarter-hour flow | ACCUMULATING | 등록 이후 공식 aggTrades 60일·100거래 전진표본 수집 중 |
| Alpha #12 현물-무기한 캐시앤캐리 | FAIL | 개발 2 spreads, 월 기하 +0.0035%; 표본·비용 stress·목표 정렬 실패 |
| Alpha #13 펀딩 분산 가격반전 | ACCUMULATING | Alpha #9 사후가설을 부모=9로 공개하고 2026-10-19부터 순수 전진검증 |

다음 우선순위는 OHLCV로 억지로 스캘핑을 재현하는 것이 아니라 실제 호가·체결 자료를 축적해
maker 기반 단기 전략의 체결 가능성과 역선택을 검증하는 것이다. 성과가 나올 때까지 레버리지부터
높이는 방식은 허용하지 않는다.

향후 두 개 이상의 sleeve가 독립적으로 `PASS`하더라도 Alpha #1의 롱 진입 체결자료를 숏이나
청산에 재사용할 수 없다. `PORTFOLIO_PROMOTION.md`와
`quant/research/portfolio_promotion.py`가 전략×종목×방향×진입/청산/보호스톱별 체결비용 증거,
공통 252일 상관관계, 수익기여 집중도, 동시 비용·펀딩 스트레스를 별도로 요구한다. 현재는
독립 `PASS` sleeve가 없으므로 포트폴리오 가중치를 만들지 않는다.

Alpha #7은 이 단계의 첫 사전등록 후보다. 현재는 연결·시간축·프로토콜 canary만 끝나 완전 수집일 0일이며
`ACCUMULATING`이다. 최소 17개 완전 UTC일과 잠금구간 1,000신호/200체결 전에는 수익률 판정을
내리지 않는다. 개발 표본이 생기기 전에 평가기를 보수화해 모든 신호는 로컬 수신시각 이후 데이터만
사용하고, taker 청산은 중간가격이 아니라 long의 bid/short의 ask로 평가한다.

현재 예비 bootstrap에서 AOA 정책과 Alpha #6 모두 50% 자본손실 확률은 0%였지만, 연 213.8%
목표 도달 경로도 0%였다. 이는 ‘안전하게 월 10%’라는 뜻이 아니라 노출과 수익이 모두 너무 작다는
뜻이며, 레버리지를 올릴 근거가 아니다.
