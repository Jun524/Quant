# Point-in-time Universe 축적 상태

현재 판정은 **ACCUMULATING**이다.

2026-10-06 UTC에 Binance USDT-M 전체 875개 계약을 처음 시점 고정으로 저장했고, 거래대금 상위
50개 계약에는 호가 깊이·미결제약정·펀딩을 함께 기록했다. 로컬 연속 1시간봉과 유동성 게이트를 모두
통과한 계약은 BTC, ETH, BNB, SOL, XRP 5개다. 나머지 종목을 오늘의 생존·유동성 정보로 과거
백테스트에 소급 편입하지 않는다.

동적 Universe의 최종 OOS 사용 조건은 전체시장 스냅샷 180개 연속 UTC일과 최신 자료 나이 1일
이하다. 현재는 1/180일이다. 이 게이트는 종목 선정의 미래정보 편향을 줄일 뿐 전략 edge를 증명하지
않는다. 기계 판정은 `reports/universe-accumulation.json`에 저장한다.
