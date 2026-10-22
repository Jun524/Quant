# Binance Futures 운영 가이드

## 현재 상태

선물 실행 경로와 보수적 개선 후보가 준비됐습니다. 개발·검증 구간의 최소 기준은 통과했지만
잠금 검증 표본이 7건뿐이므로 설정을 실거래로 바꾸지 말고 연구와 dry-run에만 사용하세요.

안전 기본값:

- Binance USDT-M perpetual
- isolated margin
- 1배 레버리지
- 롱 전용(BTC 일봉 50일·200일 EMA가 모두 강세인 경우만; 숏 기준선은 개발 구간에서 탈락)
- 포지션당 계획 손실 상한 -8% (갭·슬리피지로 실제 손실은 더 클 수 있음)
- 계좌 위험 예산 0.4% / 포지션, 최대 50 USDT × 최대 2포지션
- 청산가격과 손절 사이 `liquidation_buffer: 0.10`
- mark price 기준 거래소 내 market stoploss
- 물타기/DCA 없음
- `dry_run: true`, `initial_state: stopped`

1배 레버리지에서 전략은 전체 운용 자본의 0.4%를 포지션당 계획 위험으로 두고,
`주문금액 = 운용자본 × 0.004 ÷ 0.08`로 계산합니다. 1,000 USDT dry-run에서는 초기 약
45 USDT이며, 50 USDT를 넘지 않습니다. 손실 뒤에는 주문 금액이 줄고 이익 뒤에도 상한 이상으로
커지지 않습니다. -8% 비상 손절이 정상 체결되면 포지션당 계획 손실은 약 0.4%지만,
수수료·펀딩비·슬리피지와 급변 시 실제 손실은 이보다 클 수 있습니다.

## 명령

```bash
make futures-validate
make futures-download TIMERANGE=20210101-
make futures-backtest TIMERANGE=20220101-20260901
make futures-cost-stress TIMERANGE=20250101-20260901
make futures-daily-guard-backtest TIMERANGE=20220101-20260901
make futures-lookahead TIMERANGE=20220101-20260901
make futures-recursive TIMERANGE=20220101-20260901
make futures-dryrun-preflight
make futures-dry-run
make futures-logs
make futures-stop
```

선물 UI는 <http://127.0.0.1:8081>이며 현물 UI의 8080 포트와 분리됩니다. 데이터베이스와
로그도 `tradesv3.futures-dryrun.sqlite`, `freqtrade-futures.log`로 분리됩니다.

## 1% 일일 목표의 안전한 해석

`FuturesBtcRegimeDailyGuardLong`은 **매일 1%를 만들기 위한 전략이 아니다.** 실현손익이
한국시간 하루 기준 +1%에 도달하면 그날의 신규 진입을 차단하고, -0.8% 실현손실에 도달해도 다음
신규 진입을 차단하는 연구용 후보이다. 미달성 목표를 따라가기 위해 진입 빈도·레버리지·주문 크기를
높이지 않는다. 열려 있는 포지션은 기존 거래소 손절로 관리된다.

이 후보는 기본 전략으로 승격하지 않았다. 반드시 위의 전용 백테스트와 별도 dry-run을 거쳐,
수익 목표가 아닌 일일 손실 제한 장치로서만 평가한다.

## 비용

백테스트는 즉시 체결되는 주문을 보수적으로 taker로 보고 진입과 청산에 각각 0.05%를
적용합니다. 실제 비용은 계정 VIP 등급과 주문의 maker/taker 여부에 따라 달라집니다.

선물의 총 비용은 다음 항목의 합입니다.

1. 진입·청산 거래 수수료: 레버리지 적용 후 명목가치 기준
2. funding fee: 포지션 방향, 명목가치, 지급 시점의 funding rate에 따라 지급 또는 수취
3. bid/ask spread와 slippage
4. 강제청산 시 청산 손실과 청산 관련 비용
5. USDT 입출금·환전·네트워크 비용
6. VPS, 백업, 모니터링 비용
7. 세금과 거래내역 정리 비용

레버리지는 기대수익뿐 아니라 수수료, 펀딩비, 슬리피지의 영향을 담보금 대비 확대합니다.

## 계정·API 원칙

- 선물 봇 전용 Binance 서브계정과 API 키를 사용합니다.
- 같은 선물 계정에서 다른 봇이나 수동 포지션을 운용하지 않습니다.
- API 키에는 읽기와 Futures 거래만 허용합니다. 출금·내부/범용 이체·현물/마진·옵션·
  포트폴리오마진·FIX 권한은 모두 끕니다.
- 서버 고정 IP만 API 화이트리스트에 허용합니다. 실거래 사전점검은 IP 제한이 없으면 실패합니다.
- `.env`의 `BINANCE_FUTURES_API_KEY`, `BINANCE_FUTURES_API_SECRET`만 사용합니다.
- 현물 키와 선물 키를 재사용하지 않습니다.

Freqtrade는 레버리지 계정을 단독으로 제어한다고 가정하므로, 계정에 수동 포지션이나 다른 봇이
있으면 청산가격과 잔고 계산이 틀어질 수 있습니다.

## 실거래 배포 준비물

이 저장소의 기본 명령은 절대로 실거래를 시작하지 않습니다. 실거래 구성은 템플릿으로만 제공되며,
아래 단계는 12개 완전월·자연청산 100건의 미래 dry-run 검증과 별개로 모두 통과해야 합니다.

1. `cp .env.live.example .env.live` 후 전용 선물 서브계정 API 키와 길고 서로 다른 API 비밀값을 입력합니다.
2. `cp user_data/config.futures.live.example.json user_data/config.futures.live.json`
   및 `cp docker-compose.live.example.yml docker-compose.live.yml`을 실행합니다.
3. Binance UI에서 계정을 원웨이·단일자산 모드로 두고 whitelist 5종목을 격리마진·자동증거금
   꺼짐·1배로 설정합니다. 같은 계정의 수동 포지션과 일반/조건부 미체결 주문을 모두 정리합니다.
4. `make futures-live-preflight`를 실행합니다. 먼저 Binance의 서명된 읽기 전용 엔드포인트만 호출해
   API 최소권한과 실제 계정 상태를 확인하고, 15분짜리 키 지문 결박 영수증을 남깁니다. 이어 설정의
   1배·격리마진·2포지션 이하·손절 완충·API 비밀값·중지 상태와 연구 승격 증거를 확인합니다.
   어떤 호출도 주문을 생성·변경·취소하지 않습니다.
5. 통과 후에도 컨테이너는 `STOPPED` 상태로 시작합니다. 사용자가 UI에서 시작을 결정하기 전에는
   주문이 제출되지 않습니다.
6. 재부팅 복구, API 권한 거부, 인터넷 단절, 거래소 stoploss 주문 확인을 dry-run에서 각각 연습한
   뒤에만 극소액 1배 운용을 별도로 검토합니다.

`docker-compose.live.yml`, `.env.live`, `config.futures.live.json`은 `.gitignore`에 포함됩니다.
API 키에는 출금 권한을 절대로 주지 마세요.

## 실거래 승격 조건

최소한 다음 조건을 모두 만족하기 전에는 `dry_run`을 해제하지 않습니다.

1. 개발 구간과 잠금 검증 구간 모두 비용 후 양의 기대값
2. Profit Factor 1.2 이상을 여러 기간과 종목에서 재현
3. lookahead/recursive analysis 통과
4. 수수료와 슬리피지를 두 배로 높여도 감당 가능한 결과
5. 최소 12개 완전월·자연청산 100건의 미래 dry-run에서 백테스트와 유사한 신호 및 체결
6. API 장애, stoploss 실패, 서버 재부팅 복구 테스트
7. 극소액 1배 운용으로 장기간 재현되기 전에는 레버리지 상향 금지
