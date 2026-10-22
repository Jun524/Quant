# Alpha #1 — forward dry-run 누적 보고서

현재 판정은 **ACCUMULATING**이다. 이 보고서는 실거래 수익을 주장하지 않는다.

## 수집 상태

- MTM 관측: 8
- MTM 연속성: True
- 최대 관측 간격: 60.019305
- 완전 UTC 일 경계: 0
- 완전 월: 0 / 12
- 최신 MTM 수익률: 0.0
- MTM 최대낙폭: 0.0

## 거래 상태

- 전체 거래: 0
- 자연청산: 0 / 100
- 강제청산 제외 건수: 0
- 비용 후 폐쇄손익: 0
- 거래 수수료: 0
- 펀딩 PnL: 0
- 제거한 유리한 펀딩: 0
- 불리한 펀딩 비용: 0
- 펀딩 스트레스 후 손익: 0
- 2배 수수료 스트레스 손익: 0
- 슬리피지 스트레스: 편도 5.0bp
- 모델 슬리피지 비용: 0.0
- 슬리피지 스트레스 후 손익: 0.0

## 승격 게이트

- [ ] complete_month_sample: 0 (required 12)
- [ ] organic_trade_sample: 0 (required 100)
- [ ] geometric_monthly_return: None (required 0.1)
- [ ] positive_month_ratio: None (required 0.6)
- [ ] worst_month_loss: None (required >= -0.1)
- [x] max_drawdown: 0.0 (required <= 0.2)
- [ ] profit_factor: None (required 1.2)
- [x] mark_to_market_equity_available: mark_to_market (required mark_to_market)
- [x] locked_forward_sample: locked_forward (required locked_forward)
- [x] sample_preregistered: True (required True)
- [x] no_parameter_changes_after_sample_start: 0 (required 0)
- [x] preregistration_precedes_sample: {'registered_at_utc': '2026-10-09T11:32:49Z', 'sample_started_at_utc': '2026-10-09T16:26:08.632659Z'} (required registered_at_utc < sample_started_at_utc)
- [ ] trade_excursion_evidence_complete: 0.0 (required 1.0)
- [ ] maximum_leverage: None (required <= 3.0)
- [ ] maximum_trade_adverse_excursion: None (required <= 0.5)
- [ ] bootstrap_probability_of_ruin: None (required <= 0.01)
- [ ] bootstrap_probability_terminal_loss: None (required <= 0.1)
- [ ] bootstrap_probability_reaching_annual_target: None (required >= 0.5)
- [ ] bootstrap_maximum_drawdown_p95: None (required <= 0.2)
- [x] liquidation_exits: 0 (required 0)
- [ ] double_cost_net_positive: 0.0 (required > 0)
- [ ] slippage_evidence_complete: 0.0 (required 1.0)
- [ ] slippage_stress_net_positive: 0.0 (required > 0)
- [ ] funding_evidence_complete: 0.0 (required 1.0)
- [ ] adverse_funding_stress_net_positive: 0.0 (required > 0 after removing favorable funding and doubling adverse funding)

Dry-run execution is forward evidence, not proof of live fills or future returns.
