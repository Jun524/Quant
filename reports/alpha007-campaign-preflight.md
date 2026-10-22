# Alpha #7 — 수집 캠페인 사전점검

현재 판정은 **READY**이다. 이는 수집 자원과 표본 무결성 판정이며 전략 수익성 PASS가 아니다.

- 캠페인: 456시간(19.0일)
- 예상 저장공간: 25.21GiB
- 현재 여유 공간: 256.23GiB
- 완료 후 예상 여유 공간: 231.02GiB
- 필수 잔여 하한: 25.00GiB
- 저장량 안전계수: 1.50배

## 검사

- PASS `manifest_integrity`
- PASS `healthy_current_protocol_five_minute_sample`
- PASS `storage_rate_available`
- PASS `disk_reserve_after_estimate`

## 표본 run

- `20261006T102458Z-cc4637dd`: 15초, timing=false, current_protocol=false, healthy=false, 0.16MiB
- `20261006T111154Z-3be3909c`: 30초, timing=true, current_protocol=false, healthy=false, 0.20MiB
- `20261006T164019Z-e8fcbb7e`: 300초, timing=true, current_protocol=false, healthy=true, 1.70MiB
- `20261006T165934Z-45d4cf6d`: 300초, timing=false, current_protocol=false, healthy=false, 1.46MiB
- `20261006T174742Z-102aca69`: 5초, timing=true, current_protocol=false, healthy=false, 0.03MiB
- `20261006T174900Z-2aceb210`: 300초, timing=true, current_protocol=true, healthy=true, 1.86MiB
- `20261007T042208Z-983f5d43`: 3600초, timing=true, current_protocol=true, healthy=true, 16.75MiB

백그라운드 실행 명령은 `make microstructure-campaign-start HOURS=456 MIN_FREE_GB=25`이다.
`make microstructure-campaign-status`로 상태를 확인하고, `make microstructure-campaign-stop`은 현재
청크의 manifest를 봉인한 뒤 정상 종료를 요청한다. 캠페인은 시작 전에 같은 검사를 다시 수행하고 각
청크 전에 실제 남은 공간을 확인한다. 공개
depth·aggTrade만 저장하며 API 키를 읽거나 주문을 제출하지 않는다.
