# 멀티전략 포트폴리오 승격 게이트

현재 라이브 preflight는 Alpha #1의 롱 진입만을 대상으로 한다. 그 증거를 숏·양방향·스캘핑
전략에 재사용하면 매수와 매도, 진입과 청산, 지정가와 시장가의 체결비용 차이를 숨기게 된다.

`quant/research/portfolio_promotion.py`는 미래 포트폴리오 manifest의 각 전략을 다음 범위로
자동 전개한다.

```text
strategy × symbol × long/short × entry/exit/protective_stop
```

각 범위는 실제 전략 신호에 연결된 관측 30건 이상이어야 한다. maker 지정가는 체결 10건 이상,
공격적 지정가와 시장가는 관측 전부에 대한 체결·슬리피지 자료가 있어야 한다. 평균뿐 아니라
95백분위 총비용도 사전등록한 해당 범위의 비용 예산보다 작아야 한다. 범위 하나가 빠지거나
Alpha #1의 buy 자료를 short entry 또는 long exit에 재사용하면 fail-closed한다.

범위별 요약 JSON은 직접 신뢰하지 않는다. 원본 CSV 헤더는 정확히
`observation_id,signal_id,market_data_segment_id,observed_at,evidence_window_end_at,candidate,symbol,position_side,order_role,action_side,order_type,trigger_kind,probe_attempted,filled,cost_observed,all_in_drag_bps,limit_price,order_qty,queue_ahead_qty,fee_bps,first_depth_update_id,last_depth_update_id,first_agg_trade_id,last_agg_trade_id`이다.
각 행은 실제 전략 신호 한 건이며 관측·신호 ID는 전체 세그먼트에서 중복될 수 없다. 각
관측은 사용한 Binance depth update와 aggTrade ID 범위를 포함한다. probe 누락, 비용 누락,
비 maker 주문의 미체결, MTM 표본 밖 관측, manifest 밖 범위가 하나라도 있으면 실패한다.
평균·95백분위 총비용과 범위별 PASS는 이 CSV에서 다시 계산한다.

CSV는 단독 제출할 수 없다. `SEALED` observation manifest가 각 세그먼트의 상대경로·행 수·
최초/최종 시각·봉인 시각·SHA-256과 직전 세그먼트 SHA-256을 순서대로 보유해야 한다.
manifest는 포트폴리오 manifest·승격 정책·현재 감사 프로토콜 해시에도 결박된다. 세그먼트
교체, 순서 변경, 경로 탈출, 사후 행 추가는 승격과 최종 readiness에서 재검출된다. 이는
로컬 자료의 사후 변경을 탐지하는 장치이며 Binance가 해당 자료의 진위를 서명했다는 뜻은 아니다.

각 관측 세그먼트는 Binance 공개 수집기의 `manifest.json` 경로와 SHA-256도 포함해야 한다.
감사기는 그 manifest에 봉인된 depth 및 `agg_trades` 원본 파일을 다시 해시하고 압축 JSONL을
읽는다. 관측의 심볼과 시각이 해당 수집 구간에 속하고, 기록한 depth update ID 범위와
aggTrade ID 범위가 `observed_at`부터 `evidence_window_end_at`까지 로컬에 수신된 원본 이벤트의
연속 구간 안에 실제로 존재해야 한다. 존재하지 않는 ID나 관측 시간 밖 ID를
써 넣거나 다른 심볼·다른 시간의 원본을 재사용하면 실패한다. 최종 readiness는 원시 파일까지
전부 다시 해시한다.

`filled`와 `all_in_drag_bps`도 기록값을 신뢰하지 않는다. 감사기는 현재 수집기·로컬 호가장
프로토콜 해시가 일치하는 봉인 `book_features`에서 정확한 시작·종료 update ID의 호가를 찾는다.
maker 주문은 시작 touch 가격과 표시 대기열을 고정한 뒤 해당 창의 aggTrade로 보수적인
완전체결 여부를 다시 계산한다. 공격적 지정가와 시장가는 시작 반대편 touch의 표시 수량을
넘는 주문을 거부한다. 사전등록한 역할별 `fee_bps`, 체결가격 대비 시작 mid의 signed
slippage, 종료 mid의 불리한 markout 또는 미체결 opportunity cost로 행별 총비용을 다시
계산하며 CSV 주장과 수치가 다르면 실패한다. 이 공개시장 replay는 실제 주문확인서가 아니므로
숨은 유동성·자기 주문의 거래소 큐 위치·시장충격을 증명하지 않는다.
봉인 manifest를 거치지 않아 `raw replay` 표식이 없는 행은 표본수·체결수·비용 통계에 포함되지
않고 해당 범위가 실패한다.

```bash
make portfolio-execution-audit \
  PORTFOLIO_MANIFEST=research/portfolio-001.json \
  PORTFOLIO_EXECUTION_OBSERVATION_MANIFEST=reports/portfolio-001-execution-observations.manifest.json \
  PORTFOLIO_EQUITY=reports/portfolio-001-daily-mtm.csv \
  PORTFOLIO_EXECUTION_AUDIT=reports/portfolio-001-execution.json
```

manifest·결합 성과 보고서·체결 감사는 모두 `research/portfolio-promotion-policy.json`의
SHA-256에 결박된다. 정책이 바뀌면 기존 증거는 자동으로 무효가 되며 새 정책으로 다시 수집해야 한다.

V5부터는 좋은 결과만 고른 선택편향도 별도 차단한다. 각 sleeve와 결합 포트폴리오는 동일한
일별 관측축에 모든 시도(파라미터·가중치·전략 변형 포함)를 열로 둔 trial matrix를 제출해야 한다.
`make selection-bias-audit`는 선택된 열이 실제 matrix와 일치하는지 확인하고, 열 순서와 수익값을
SHA-256으로 봉인한 뒤 Deflated Sharpe Ratio(확률 95% 이상)와 8분할 CSCV PBO(10% 이하)를
계산한다. 단 하나의 사전등록 trial은 PBO만 면제되며 DSR과 252개 관측 기준은 그대로 적용된다.

```bash
make selection-bias-audit \
  TRIAL_RETURNS=reports/portfolio-001-daily-trials.csv \
  TRIAL_INVENTORY=research/portfolio-001-trial-inventory.json \
  SELECTION_BIAS_OUTPUT=reports/portfolio-001-selection-bias.json
```

CSV 첫 열은 UTC offset이 있는 `timestamp`, 나머지 열 이름은 중복 없는 trial ID여야 한다.
inventory는 [template](research/selection-bias-trial-inventory.template.json)을 복사해 잠금 표본
시작 전에 닫아야 한다. 선택 시각과 모든 trial의 등록 시각이 첫 CSV 관측보다 앞서야 하고,
trial ID·순서·개수는 CSV 열과 정확히 같아야 한다. 각 configuration과 사전등록한 선택 규칙의
SHA-256도 필요하다. inventory와 CSV는 승격 영수증에서 다시 해시되므로 사후 추가·교체하면
무효가 된다. 그래도 애초에 기록하지 않은 비공개 trial을 통계식만으로 발견할 수는 없으므로
append-only 실험 등록부와 함께 사용한다.

V16의 결합 포트폴리오는 수수료·슬리피지·펀딩을 반영한 매일 00:00 UTC
mark-to-market 자산가치도 366일 이상 제출한다. 365개 일간수익률을 7일과
28일 연속 블록으로 각각 5,000회 재표본한다. 파산 확률·말기 손실 확률·95%
최대낙폭은 두 시나리오 중 더 나쁜 값, 연 목표 도달 확률은 더 낮은 값을 쓴다.
확률 게이트는 5,000회 시뮬레이션의 점추정치를 그대로 믿지 않는다. 파산·말기 손실은
95% 단측 Wilson 상한이 기준 이하여야 하고, 연 목표 도달은 95% 단측 Wilson 하한이
기준 이상이어야 한다. 경계선의 우연한 통과를 막기 위한 보수적 처리다.
그와 별개로 관측 표본의 최악 1일을 3회, 최악 7일 연속 구간을 4회, 최악 28일
연속 구간을 2회 반복하는 결정론적 tail-recurrence 스트레스도 계산한다. 세 경로 중
하나라도 MTM 낙폭이 20%를 넘으면 실패한다. 이는 심각한 손실 군집이 Monte Carlo에서
우연히 뽑히지 않아 통과하는 빈틈을 막는다.
7일 블록만 쓰면 스윙 전략의 수주 간 손실 군집을 과소평가할 수 있기 때문이다. 이
부트스트랩은 예측이나 수익 보장이 아니며, 실제 최대낙폭 20% 이하와 청산 0건
조건을 대체하지 않는다. 이 게이트는 미래 포트폴리오에만 적용되며 현재 수집 중인
Alpha #1의 동결 프로토콜은 변경하지 않는다.

V16은 결합 비용 스트레스의 최종 손익만 보지 않는다. 동일한 일별 회계 원장을 시간순으로
`수수료 2배 + 슬리피지 2배 + 수익 펀딩 제거/지급 펀딩 2배` 조건에서 다시 누적한다.
도중 자산이 0 이하가 되거나 이 스트레스 자산곡선의 최대낙폭이 20%를 넘으면, 최종 손익이
양수로 회복했더라도 승격에 실패한다. 따라서 후기의 큰 승자가 앞선 비용성 파산을 가리는
경로 의존성 오류를 허용하지 않는다.

```bash
make portfolio-risk-audit \
  PORTFOLIO_EQUITY=reports/portfolio-001-daily-mtm.csv \
  PORTFOLIO_RISK_OUTPUT=reports/portfolio-001-risk.json
```

자산가치 CSV의 헤더는 정확히 `timestamp,equity,equity_type`이고, 모든 행의
`equity_type`은 `mark_to_market`이어야 한다. 중간 날짜·일중 타임스탬프·거래 종료 잔고를
섞으면 실패한다. 승격 단계에서 원본 CSV를 다시 읽고 결과를 전부 재계산하므로,
영수증 JSON의 숫자만 고쳐서는 통과할 수 없다.
연속 관측의 의존성을 보존하는 블록 재표본의 근거는
[Künsch (1989)](https://doi.org/10.1214/aos/1176347265)와
[Politis·Romano (1994)](https://doi.org/10.1080/01621459.1994.10476870)이다.
단, 7일·28일은 이 플랫폼의 사전등록 시나리오이지 논문이 최적으로 보장한 값은 아니다.
Monte Carlo 이벤트 확률의 표본오차 보정은
[Wilson (1927)](https://doi.org/10.1080/01621459.1927.10502953)의 score interval을 쓴다.

상관계수와 sleeve 수익기여 집중도도 보고서의 숫자를 믿지 않는다. 별도 CSV의 헤더는
`timestamp,<manifest의 sleeve 순서>`여야 하고, 각 값은 전일 전체 포트폴리오 MTM
자산 대비 해당 sleeve의 배분 후 비용 차감 일일 PnL 기여도이다. 매일 행 합계가 원본
포트폴리오 MTM 수익률과 1e-12 오차 이내로 일치해야 한다. 이 원본에서 공통
관측수, 모든 sleeve 쌍의 일별 상관계수, 양의 sleeve-day PnL 기여 비중을 재계산한다.
상수열로 상관계수가 정의되지 않거나 날짜·후보·순서·MTM 합계가 틀리면 실패한다.

월 10% 게이트도 결과 JSON을 직접 믿지 않는다. MTM 원본에서 12개 완전월·기하
월수익률·양의 월 비율·최악월·MDD를 재계산하고, 폐쇄 거래 원장에서 자연거래수·
PF·MAE·청산·수수료·펀딩·슬리피지를 재계산한다. 자연거래는 `force_exit`과
`liquidation`을 제외한 폐쇄 거래다. 순거래 PnL은
`gross_price_pnl - trading_fees + funding_pnl - modeled_slippage`로 고정한다.
거래 개시와 종료 시각이 둘 다 포트폴리오 MTM 표본 경계 안에 있지 않으면, 표본 밖
거래로 거래수나 PF를 부풀린 것으로 보고 실패한다.
전체 폐쇄 거래원장의 순손익 합계는 최종 MTM 자산에서 최초 MTM 자산을 뺀 값과 전일
자산 대비 1e-9 이내로 일치해야 한다. 사전 고정된 표본 종료 시 열린 포지션은 결정론적으로
평가·종료해 `force_exit`으로 기록할 수 있지만, 이 거래는 전체 회계 대조에만 포함되고
자연거래 수와 PF에서는 제외된다. 따라서 손익 총액을 원장에서 누락하거나 추가해 PF를
부풀리면 MTM 총액 대조가 실패한다. 거래 분할 자체의 진위는 별도의 주문·체결 원본 결박이
담당하며, 총액 대조만으로 거래 이벤트의 실재성을 증명한다고 해석하지 않는다.
`force_exit`으로 분류한 거래는 반드시 잠금 MTM 표본의 정확한 종료시각에 닫혀야 한다.
표본 중간의 손실 거래를 `force_exit`으로 바꿔 자연거래·PF에서 제외하면 즉시 실패한다.
거래별 PF와 별도로, MTM과 대조된 일별 회계 순손익에서도 PF 1.20 이상을 요구한다.
손실일이 전혀 없다면 일별 총이익이 양수인 경우만 허용한다. 따라서 서로 다른 거래의 손익을
한 거래로 합치거나 분리해 거래 PF를 바꾸더라도 일별 손익 품질 검사를 우회할 수 없다.

V16 폐쇄 거래 CSV에는 `entry_notional`, `exit_notional`, `exit_order_role`도 필수다.
정확한 열 순서는 [`research/portfolio-trades.template.csv`](research/portfolio-trades.template.csv)를
사용한다.
`exit_order_role`은 일반·강제 청산이면 `exit`, 손절·트레일링·청산이면
`protective_stop`이어야 한다. 각 거래의 수수료는 manifest에 사전등록한 진입·청산 역할별
`fee_bps` 하한 이상이어야 하고, 수수료와 모델 슬리피지의 합은 같은 역할의
`maximum_all_in_drag_bps`를 양쪽 명목가에 적용한 금액 이상이어야 한다. 또한 거래 원장과
일별 회계 원장의 gross PnL·수수료·펀딩·슬리피지 네 항목을 각각 대조한다. 따라서 순손익만
맞춘 채 수수료를 gross PnL로 옮기거나 체결 감사보다 싼 비용을 성과에 쓰면 실패한다.

일별 MTM 사이의 장중 위험도 별도로 제한한다. 모든 거래의
`maximum_leverage × maximum_adverse_excursion` 최댓값은 20% 이하여야 한다. 청산 기록이
0건이거나 거래가 종가 기준으로 회복했더라도 장중 레버리지 조정 손실이 20%를 넘으면
승격하지 않는다. 이는 거래 원장의 MAE 증거를 사용하는 보수적 손실 한도이며, 거래소별
정확한 청산가격 보장을 의미하지 않는다.

V18부터는 이 판단을 단순한 보고서 문구에 맡기지 않는다. 봉인된 거래 원장과 일별 노출
원장에서 거래별 최대 레버리지, 레버리지 조정 MAE, 일별 gross/net 레버리지, 청산을 다시
읽어 규칙 위반 수를 계산한다. 각 원장 행의 위반 근거는 영수증에 남고, 하나의 행이 둘 이상의
규칙을 어기면 각각 별도 위반으로 센다. 이 수는 반드시 0이어야 하며, 영수증의 숫자만
수정해서는 원본 재생 검사를 통과할 수 없다.

V19부터 포트폴리오의 수익성 판정은 `combined_cost_after_expectancy_gate`다.
잠긴 표본 전체에서 비용 후 순수익이 양수여야 하고, PF·수수료·슬리피지·펀딩·MTM·낙폭
검사를 모두 통과해야 한다. 기하 월수익률이나 양의 월 비율은 보고용 진단값일 뿐, 승격
조건이 아니다. 이 변경은 정책 V19 시작 뒤 등록하는 포트폴리오에만 적용하며 기존에 잠긴
표본의 판정을 다시 해석하지 않는다.

V20부터 위험 시뮬레이션의 연간 수익 시나리오 경계도 0%다. 이는 “비용 후 연간 양의
수익일 확률”을 기술하는 값일 뿐 수익 목표나 통과 기준이 아니다. 통과 조건은 별도의
비용 후 기대값·낙폭·청산·규칙 위반 게이트로만 결정된다.

V16에서는 보고서가 주장하는 잠금 표본 시각 자체도 신뢰하지 않는다. 포트폴리오
`sample_started_at_utc`와 `sample_ended_at_utc`는 재생한 MTM 원본의 최초·최종 00:00 UTC
스냅샷과 정확히 같아야 한다. 기여도와 일별 회계 원장은 그 사이의 모든 일간수익률 날짜를
빠짐없이 덮어야 하고, 경계 `force_exit` 시각도 같은 종료시각이어야 한다. 각 sleeve의 잠금
시작·종료도 포트폴리오 구간과 정확히 같아야 한다. 따라서 과거 자료를 본 뒤 등록하고 자체
보고 시작일만 뒤로 옮기거나, 서로 다른 유리한 기간의 sleeve PASS를 한 포트폴리오로 묶으면
승격할 수 없다.

V16은 sleeve 기여도 분해도 합계만 맞는다고 인정하지 않는다. 별도
`PORTFOLIO_SLEEVE_PNL` CSV에는 각 일자의 sleeve별 순손익을 최초 포트폴리오 MTM 자산 대비
비율로 기록한다. 매일 각 열의 합은 정규화된 포트폴리오 MTM 자산 변화와 1e-12 이내로
일치해야 하며, 기여도 CSV의 각 값은 해당 sleeve 순손익을 전일 정규화 MTM 자산으로 나눈
값과 같아야 한다. 각 sleeve 보고서의 `evidence.daily_net_pnl_fraction_sha256`도 이 원본의
자기 열과 같아야 한다. 따라서 전체 수익은 그대로 둔 채 sleeve 열만 재분배해 상관계수나
수익집중도를 유리하게 꾸미는 방식은 실패한다.
헤더와 후보 순서는 [`research/portfolio-sleeve-pnl.template.csv`](research/portfolio-sleeve-pnl.template.csv)처럼
`timestamp,<manifest sleeve 순서>`여야 한다.

같은 원장에서 manifest의 초기 배분비중을 시작자본으로 삼아 sleeve별 자기자금 자산곡선과
일수익률을 순차 재구성한다. 중간 sleeve 자산이 0 이하이면 전체 포트폴리오가 살아 있어도
실패한다. 재구성한 각 수익률 계열의 SHA-256은 해당 sleeve 선택편향 영수증의
`selected_return_sha256`과 같아야 한다. 결합 포트폴리오 영수증의 선택 계열도 실제 MTM
일수익률 SHA-256과 같아야 한다. 따라서 DSR/PBO에는 좋은 별도 수익률을 넣고 승격에는 다른
거래 손익을 넣는 방식은 통과하지 못한다.

V16에서 쌍별 상관은 위에서 재구성한 자기자금 sleeve 일수익률로 계산한다. 양의 손익
집중도는 전일 포트폴리오 자산으로 나눈 기여율이 아니라 원천 sleeve 순손익으로 계산한다.
감사 결과에는 두 계산 기준 문자열도 고정해, 이전 기준이나 임의 기준의 통계를 제출하면
승격을 거부한다.

manifest 최상위와 각 sleeve에는 `selection_bias_family_id`와
`selection_bias_inventory_id`가 필수다. 두 값은 잠금 표본 시작 전 manifest에 포함되어 함께
해시되고, 제출된 봉인 trial inventory의 `family_id`와 `inventory_id`가 정확히 같아야 한다.
따라서 동일한 수익률 계열을 포함하더라도 더 적은 시도만 기록한 다른 inventory로 바꾸면
선택편향 게이트에서 거부된다. 새 manifest는
[`research/portfolio-manifest.template.json`](research/portfolio-manifest.template.json)에서 시작한다.

일별 최대 노출도 자기신고로 통과할 수 없다. 각 UTC 일자에서 열린 거래 구간을 시간순으로
sweep하고, 거래별 `max(entry_notional, exit_notional)`을 전체 보유구간 명목가로 사용한다.
노출 원장의 정확한 헤더는
[`research/portfolio-exposure.template.csv`](research/portfolio-exposure.template.csv)를 사용한다.
동시에 열린 long·short의 gross 합과 signed net 절댓값을 계산한 뒤 그날 시작·종료 MTM 중
작은 자산가치로 나눠 보수적 레버리지 하한을 만든다. 제출한 일별 gross/net 최대치가 이
하한보다 작으면 실패한다. 실제 장중 명목가 변화를 완전히 재현하는 상한은 아니지만,
겹친 포지션을 누락해 레버리지를 축소 보고하는 것은 차단한다.

스트레스는 수수료 2배, 슬리피지 2배, 불리한 펀딩
`funding_pnl - abs(funding_pnl)`, 그리고 세 가지를 동시 적용한 경로를 모두 계산한다.
매일 최대 gross 및 absolute net 레버리지 CSV는 MTM 수익률 날짜와 정확히 일치해야
하며, net이 gross보다 크거나 날짜가 비면 실패한다.

비용 스트레스의 원본은 폐쇄 거래만이 아니라 일별 MTM 회계원장이다. 헤더는 정확히
`timestamp,gross_price_pnl,trading_fees,funding_pnl,modeled_slippage`이며 각 행은 해당
00:00 UTC에 끝나는 하루의 포트폴리오 통화 기준 금액이다. 수수료와 슬리피지는 양의 비용,
펀딩은 포트폴리오 관점의 부호 있는 손익으로 기록한다. 매일
`gross_price_pnl - trading_fees + funding_pnl - modeled_slippage`가 실제 MTM 자산 증감과
전일 자산 대비 1e-9 이내로 일치해야 한다. 따라서 표본 종료 시점의 미청산 포지션이나
거래원장에 빠진 비용도 스트레스 계산에서 누락할 수 없다.

포트폴리오 자체도 최소 두 개의 독립 `PASS` sleeve, 공통 일별 관측 252일, 선택편향 게이트,
완전한 월 10% 게이트,
동시 수수료·슬리피지·펀딩 스트레스 양수, 청산 0건을 요구한다. 상관계수와 수익기여 집중도도
명시적으로 제한한다. 이 게이트는 주문 허가가 아니라 기존 계정·API·바이어스 preflight 전에
추가로 통과해야 하는 전제조건이다.

```bash
make portfolio-promotion \
  PORTFOLIO_MANIFEST=research/portfolio-001.json \
  PORTFOLIO_REPORT=reports/portfolio-001-forward.json \
  PORTFOLIO_SELECTION_BIAS_AUDIT=reports/portfolio-001-selection-bias.json \
  PORTFOLIO_RISK_AUDIT=reports/portfolio-001-risk.json \
  PORTFOLIO_CONTRIBUTIONS=reports/portfolio-001-daily-contributions.csv \
  PORTFOLIO_SLEEVE_PNL=reports/portfolio-001-daily-sleeve-pnl.csv \
  PORTFOLIO_TRADES=reports/portfolio-001-closed-trades.csv \
  PORTFOLIO_EXPOSURE=reports/portfolio-001-daily-max-exposure.csv \
  PORTFOLIO_ACCOUNTING=reports/portfolio-001-daily-accounting.csv \
  SLEEVE_REPORTS='ALPHA_A=reports/a.json ALPHA_B=reports/b.json' \
  SLEEVE_SELECTION_BIAS_AUDITS='ALPHA_A=reports/a-selection-bias.json ALPHA_B=reports/b-selection-bias.json' \
  PORTFOLIO_EXECUTION_AUDIT=reports/portfolio-001-execution.json
```

현재 독립 `PASS` 전략이 0개이므로 실제 포트폴리오 manifest는 아직 만들지 않는다. 후보를 먼저
정해놓고 과거 결과에 맞춰 가중치를 조절하는 것을 막기 위해, 최소 두 sleeve가 잠금 미래검증을
통과한 뒤 별도 사전등록한다.

`make goal-readiness`는 승격 JSON의 `status` 나 `checks`를 믿지 않는다. 모든 입력을
재해시한 뒤 `portfolio_promotion_report.py`를 임시 출력으로 다시 실행하고, 생성 시각과
입력 경로 표현을 제외한 모든 판정 필드가 기존 영수증과 같은지 확인한다. 원본을
두고 영수증만 수정하면 `STALE_OR_INVALID_EVIDENCE`로 처리된다.
