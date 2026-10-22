# 외부 퀀트 저장소 적용 결정

이 프로젝트는 Binance USDT-M 선물의 **비용 후·인과적·재현 가능한 연구**를 목표로 한다.
따라서 규모가 큰 프레임워크를 겹쳐 설치하기보다, 현재의 Freqtrade 실행 경로와
source-bound 원자료/등록부/승격 게이트를 유지한 채 검증 가능한 요소만 흡수한다.
모든 항목은 주문 권한을 주지 않는다.

| 저장소 | 채택한 요소 | 현재 결정 |
|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 신호를 거래 규칙 전에 검증하는 연구 순서, Rank IC, ICIR, quantile spread, 신호 자기상관·회전율 | **채택 완료**. `quant/research/signal_diagnostics.py`와 `scripts/signal_diagnostics_report.py`가 의존성 없이 동일 목적의 진단을 수행한다. 이 결과는 비용 후 체결 검증 전의 후보 선별일 뿐 PASS 근거가 아니다. |
| [OpenBB](https://github.com/openbq-org/OpenBB) | 공급자 교체가 가능한 데이터 접근 계층, 분석용 데이터 탐색 | **보류**. Binance 가격·펀딩·order book 성과 근거는 제3자 변환값이 아니라 거래소 원자료와 해시된 receipt를 사용한다. OpenBB는 나중에 거시·주식·온체인 보조 가설 탐색이 필요할 때만 research-only adapter로 분리한다. |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | 다중 출처 상태판, 예약 실행, 설명 가능한 리포트 | **부분 채택**. `research-health`, `platform-scoreboard`, append-only evidence와 scheduler 상태가 이 역할을 맡는다. LLM 뉴스 요약은 가설 문서화에는 쓸 수 있어도 진입·청산을 결정하거나 과거 데이터를 덮어쓰면 안 된다. |
| [vn.py](https://github.com/vnpy/vnpy) | event/order 상태를 독립적으로 추적하는 실행 계층 설계 | **부분 채택**. `bounded_notional`, 거래소 필터 audit, entry-evidence recorder와 maker/shadow 실행 진단으로 반영한다. 현 단계에서 두 실행 엔진을 동시에 운용하면 주문 상태의 진실 원천이 둘이 되므로 vn.py gateway는 도입하지 않는다. |
| [AKShare](https://github.com/akfamily/akshare) | 폭넓은 금융 데이터 catalogue | **미도입**. 중국·주식 중심 데이터는 현재 Binance 선물 가설의 원가격·펀딩·체결 증거를 대체하지 못한다. 별도 자산군 연구를 사전등록할 때만 검토한다. |
| [Zipline](https://github.com/quantopian/zipline) | event-driven backtest의 개념 | **참조만**. 저장소가 명시한 지원 Python 범위가 현재 연구 환경과 맞지 않고, 주식 중심 모델은 perpetual futures의 funding·거래소 필터·청산 위험을 포괄하지 않는다. |
| [ABU](https://github.com/bbfamily/abu) | 교육용 전략·분석 아이디어 | **참조만**. GPL-3.0 코드와 이 프로젝트의 소스 결합을 피하며, 코드를 복사하거나 파생 구현하지 않는다. |

## 고정된 적용 규칙

1. 신규 외부 데이터는 공급자 이름, 원시 응답/파일 SHA-256, 수집 시각, 허용된 결정 시각을 receipt에 남긴다.
2. 거래소 실행·성과 평가는 Binance 원자료 재생이 가능한 경우에만 승격 증거로 인정한다.
3. LLM·대시보드·외부 라이브러리의 분석 출력은 후보 생성용이다. 주문 방향·주문 금액·레버리지·승격 판정을 직접 바꾸지 못한다.
4. 새 라이브러리를 추가하기 전에 라이선스, Python 호환성, futures funding/수수료/필터 모델, 현재 증거 사슬과의 충돌을 검토한다.
5. 실행 엔진은 하나만 유지한다. Freqtrade가 dry-run/실거래 주문의 단일 진실 원천이며, 모든 live 권한은 별도의 PASS receipt와 명시적 승인 전까지 `CLOSED`다.

## 다음에 가치가 큰 확장 순서

1. Qlib식 신호 진단을 Alpha #15의 point-in-time Universe가 충분히 쌓인 뒤 새 횡단면 가설에 적용한다.
2. Alpha #7의 shadow maker 표본을 계속 수집해 backtest 비용 가정을 실제 호가·체결 품질로 교정한다.
3. 두 개 이상의 독립 sleeve가 비용 후 게이트를 통과한 경우에만, daily_stock_analysis식 읽기 전용 의사결정 대시보드에 portfolio MTM·노출·중단 사유를 표시한다.
4. OpenBB/AKShare 같은 보조 데이터는 위 단계와 독립된 사전등록 가설이 생긴 경우에만 adapter로 추가한다. 기존 실험의 데이터나 결과를 소급 변경하지 않는다.
