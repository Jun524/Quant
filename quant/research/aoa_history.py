"""Reconstruct auditable behavior statistics from the supplied BitMEX history.

The source contains execution rows, not one row per trading decision.  This
module therefore separates fills, orders, position episodes, funding, wallet
ledger entries, and forced liquidations before calculating any statistics.
"""

from __future__ import annotations

import bisect
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import subprocess
from urllib.parse import urlencode


ZERO_ORDER_ID = "00000000-0000-0000-0000-000000000000"


def _float(value: str | float | int | None) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _quantile(values: list[float], percentile: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    location = (len(ordered) - 1) * percentile
    low = int(location)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (location - low)


def _distribution(values: list[float]) -> dict:
    return {
        "median": _quantile(values, 0.50),
        "p90": _quantile(values, 0.90),
        "p99": _quantile(values, 0.99),
    }


def reconstruct_xbtusd_episodes(
    fills: list[dict], *, dust_tolerance_contracts: float = 1.0
) -> tuple[list[dict], float]:
    """Return flat-to-flat XBTUSD episodes using the official inverse payoff.

    Sorting individual executions is essential because several orders may be
    live and partially fill at the same time.  Aggregating an order before
    position reconstruction can change the position path.
    """
    ordered = sorted(fills, key=lambda item: (item["time"], item["exec_id"]))
    position = 0.0
    average_inverse_price = 0.0
    current: dict | None = None
    episodes: list[dict] = []

    for fill in ordered:
        direction = 1 if fill["side"] == "Buy" else -1
        remaining = fill["qty"]
        fee_per_contract = fill["commission_xbt"] / fill["qty"] if fill["qty"] else 0.0
        while remaining > 1e-9:
            if abs(position) < 1e-9:
                # The export frequently leaves a one-contract residual between
                # otherwise distinct decisions. Treating that dust as a new
                # episode creates artificial scale-up ratios.
                if remaining <= dust_tolerance_contracts:
                    remaining = 0.0
                    continue
                position = direction * remaining
                average_inverse_price = 1.0 / fill["price"]
                current = {
                    "side": "long" if direction > 0 else "short",
                    "start": fill["time"],
                    "end": None,
                    "initial_entry_price": fill["price"],
                    "initial_order_id": fill["order_id"],
                    "initial_order_qty": remaining,
                    "peak_qty": remaining,
                    "entry_order_ids": {fill["order_id"]},
                    "exit_order_ids": set(),
                    "favorable_add_orders": 0,
                    "adverse_add_orders": 0,
                    "gross_pnl_xbt": 0.0,
                    "commission_xbt": fee_per_contract * remaining,
                    "maker_entry_qty": remaining if fill["liquidity"] == "AddedLiquidity" else 0.0,
                    "taker_entry_qty": remaining if fill["liquidity"] == "RemovedLiquidity" else 0.0,
                    "maker_exit_qty": 0.0,
                    "taker_exit_qty": 0.0,
                    "liquidated": fill["liquidation"],
                }
                remaining = 0.0
                continue

            assert current is not None
            if position * direction > 0:
                old_quantity = abs(position)
                is_new_order = fill["order_id"] not in current["entry_order_ids"]
                if is_new_order:
                    average_price = 1.0 / average_inverse_price
                    favorable = (fill["price"] - average_price) * (1 if position > 0 else -1) > 0
                    current["favorable_add_orders" if favorable else "adverse_add_orders"] += 1
                current["entry_order_ids"].add(fill["order_id"])
                if fill["order_id"] == current["initial_order_id"]:
                    current["initial_order_qty"] += remaining
                average_inverse_price = (
                    average_inverse_price * old_quantity + remaining / fill["price"]
                ) / (old_quantity + remaining)
                position += direction * remaining
                current["peak_qty"] = max(current["peak_qty"], abs(position))
                current["commission_xbt"] += fee_per_contract * remaining
                current["maker_entry_qty"] += remaining if fill["liquidity"] == "AddedLiquidity" else 0.0
                current["taker_entry_qty"] += remaining if fill["liquidity"] == "RemovedLiquidity" else 0.0
                current["liquidated"] = current["liquidated"] or fill["liquidation"]
                remaining = 0.0
                continue

            close_quantity = min(abs(position), remaining)
            current["gross_pnl_xbt"] += close_quantity * (
                average_inverse_price - 1.0 / fill["price"]
            ) * (1 if position > 0 else -1)
            current["commission_xbt"] += fee_per_contract * close_quantity
            current["exit_order_ids"].add(fill["order_id"])
            current["maker_exit_qty"] += close_quantity if fill["liquidity"] == "AddedLiquidity" else 0.0
            current["taker_exit_qty"] += close_quantity if fill["liquidity"] == "RemovedLiquidity" else 0.0
            current["liquidated"] = current["liquidated"] or fill["liquidation"]
            position += direction * close_quantity
            remaining -= close_quantity
            if abs(position) <= dust_tolerance_contracts:
                position = 0.0
                average_inverse_price = 0.0
                current["end"] = fill["time"]
                current["duration_minutes"] = (current["end"] - current["start"]).total_seconds() / 60.0
                current["net_pnl_xbt"] = current["gross_pnl_xbt"] - current["commission_xbt"]
                # Scale-independent approximation used only for chronological
                # behavior-model labels.  Exact account return is unknowable
                # because collateral and leverage are absent from the export.
                current["net_return_proxy"] = (
                    current["net_pnl_xbt"] * current["initial_entry_price"] / current["peak_qty"]
                    if current["peak_qty"]
                    else 0.0
                )
                current["entry_orders"] = len(current.pop("entry_order_ids"))
                current["exit_orders"] = len(current.pop("exit_order_ids"))
                current.pop("initial_order_id")
                episodes.append(current)
                current = None

    return episodes, position


def summarize_episodes(episodes: list[dict]) -> dict:
    if not episodes:
        return {"episodes": 0}
    profits = [item["net_pnl_xbt"] for item in episodes if item["net_pnl_xbt"] > 0]
    losses = [-item["net_pnl_xbt"] for item in episodes if item["net_pnl_xbt"] < 0]
    entry_total = sum(item["maker_entry_qty"] + item["taker_entry_qty"] for item in episodes)
    exit_total = sum(item["maker_exit_qty"] + item["taker_exit_qty"] for item in episodes)
    winner_durations = [item["duration_minutes"] for item in episodes if item["net_pnl_xbt"] > 0]
    loser_durations = [item["duration_minutes"] for item in episodes if item["net_pnl_xbt"] < 0]
    def proxy_group(items: list[dict]) -> dict:
        values = [float(item.get("net_return_proxy") or 0.0) for item in items]
        profits = [value for value in values if value > 0]
        losses = [-value for value in values if value < 0]
        return {
            "episodes": len(items),
            "win_rate": len(profits) / len(items) if items else None,
            "mean_return_proxy": statistics.mean(values) if values else None,
            "median_return_proxy": statistics.median(values) if values else None,
            "profit_factor_proxy": sum(profits) / sum(losses) if losses else None,
        }

    def maker_share(item: dict) -> float:
        quantity = item["maker_entry_qty"] + item["taker_entry_qty"]
        return item["maker_entry_qty"] / quantity if quantity else 0.0

    segments = {
        "entry_maker_share": {
            "below_50pct": proxy_group([item for item in episodes if maker_share(item) < 0.50]),
            "50_to_90pct": proxy_group([item for item in episodes if 0.50 <= maker_share(item) < 0.90]),
            "at_least_90pct": proxy_group([item for item in episodes if maker_share(item) >= 0.90]),
        },
        "entry_order_count": {
            "one": proxy_group([item for item in episodes if item["entry_orders"] == 1]),
            "two_to_three": proxy_group([item for item in episodes if 2 <= item["entry_orders"] <= 3]),
            "four_to_nine": proxy_group([item for item in episodes if 4 <= item["entry_orders"] <= 9]),
            "ten_or_more": proxy_group([item for item in episodes if item["entry_orders"] >= 10]),
        },
        "position_adjustment": {
            "no_adverse_add": proxy_group([item for item in episodes if item["adverse_add_orders"] == 0]),
            "has_adverse_add": proxy_group([item for item in episodes if item["adverse_add_orders"] > 0]),
            "no_favorable_add": proxy_group([item for item in episodes if item["favorable_add_orders"] == 0]),
            "has_favorable_add": proxy_group([item for item in episodes if item["favorable_add_orders"] > 0]),
        },
        "holding_time": {
            "up_to_30m": proxy_group([item for item in episodes if item["duration_minutes"] <= 30]),
            "30m_to_4h": proxy_group([item for item in episodes if 30 < item["duration_minutes"] <= 240]),
            "over_4h": proxy_group([item for item in episodes if item["duration_minutes"] > 240]),
        },
    }
    return {
        "episodes": len(episodes),
        "wins": len(profits),
        "win_rate": len(profits) / len(episodes),
        "gross_pnl_xbt": sum(item["gross_pnl_xbt"] for item in episodes),
        "commission_xbt": sum(item["commission_xbt"] for item in episodes),
        "net_pnl_xbt": sum(item["net_pnl_xbt"] for item in episodes),
        "profit_factor": sum(profits) / sum(losses) if losses else None,
        "median_net_pnl_xbt": statistics.median(item["net_pnl_xbt"] for item in episodes),
        "largest_winner_share": max(profits) / sum(profits) if profits else None,
        "duration_minutes": _distribution([item["duration_minutes"] for item in episodes]),
        "winner_duration_median": statistics.median(winner_durations) if winner_durations else None,
        "loser_duration_median": statistics.median(loser_durations) if loser_durations else None,
        "peak_to_initial_order": _distribution(
            [item["peak_qty"] / item["initial_order_qty"] for item in episodes if item["initial_order_qty"]]
        ),
        "entry_orders": _distribution([float(item["entry_orders"]) for item in episodes]),
        "exit_orders": _distribution([float(item["exit_orders"]) for item in episodes]),
        "favorable_add_orders": sum(item["favorable_add_orders"] for item in episodes),
        "adverse_add_orders": sum(item["adverse_add_orders"] for item in episodes),
        "entry_maker_share": sum(item["maker_entry_qty"] for item in episodes) / entry_total if entry_total else None,
        "exit_maker_share": sum(item["maker_exit_qty"] for item in episodes) / exit_total if exit_total else None,
        "liquidated_episodes": sum(bool(item["liquidated"]) for item in episodes),
        "behavior_segments": segments,
    }


def scan_executions(paths: list[Path]) -> tuple[dict, list[dict], list[dict]]:
    counters = {key: Counter() for key in ("symbol", "exec_type", "order_type", "liquidity", "text")}
    orders: dict[tuple[str, str], dict] = {}
    xbt_fills: list[dict] = []
    liquidations: list[dict] = []
    funding_xbt = 0.0
    row_count = 0
    trade_rows = 0

    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                row_count += 1
                for key, source_key in (
                    ("symbol", "symbol"),
                    ("exec_type", "exectype"),
                    ("order_type", "ordtype"),
                    ("liquidity", "lastliquidityind"),
                    ("text", "text"),
                ):
                    counters[key][row[source_key] or "<NA>"] += 1
                if row["exectype"] == "Funding":
                    funding_xbt += _float(row["execcomm"]) / 1e8
                    continue
                if row["exectype"] != "Trade":
                    continue
                trade_rows += 1
                event = {
                    "exec_id": row["execid"],
                    "order_id": row["orderid"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "qty": _float(row["lastqty"]),
                    "price": _float(row["lastpx"]),
                    "commission_xbt": _float(row["execcomm"]) / 1e8,
                    "liquidity": row["lastliquidityind"],
                    "time": datetime.fromisoformat(row["transacttime"]),
                    "liquidation": row["text"] == "Liquidation",
                }
                if event["liquidation"]:
                    liquidations.append(event)
                else:
                    key = (row["symbol"], row["orderid"])
                    order = orders.setdefault(
                        key,
                        {
                            "symbol": row["symbol"],
                            "order_type": row["ordtype"],
                            "exec_inst": row["execinst"],
                            "qty": 0.0,
                            "maker_qty": 0.0,
                            "taker_qty": 0.0,
                            "fills": 0,
                            "first": event["time"],
                            "last": event["time"],
                        },
                    )
                    order["qty"] += event["qty"]
                    order["maker_qty"] += event["qty"] if event["liquidity"] == "AddedLiquidity" else 0.0
                    order["taker_qty"] += event["qty"] if event["liquidity"] == "RemovedLiquidity" else 0.0
                    order["fills"] += 1
                    order["first"] = min(order["first"], event["time"])
                    order["last"] = max(order["last"], event["time"])
                if row["symbol"] == "XBTUSD":
                    xbt_fills.append(event)

    order_values = list(orders.values())
    total_quantity = sum(item["qty"] for item in order_values)
    summary = {
        "execution_rows": row_count,
        "trade_fill_rows": trade_rows,
        "ordinary_orders": len(order_values),
        "fills_per_order": _distribution([float(item["fills"]) for item in order_values]),
        "fill_duration_seconds": _distribution(
            [(item["last"] - item["first"]).total_seconds() for item in order_values]
        ),
        "maker_quantity_share": sum(item["maker_qty"] for item in order_values) / total_quantity,
        "taker_quantity_share": sum(item["taker_qty"] for item in order_values) / total_quantity,
        "post_only_orders": sum("ParticipateDoNotInitiate" in item["exec_inst"] for item in order_values),
        "order_types": dict(Counter(item["order_type"] for item in order_values)),
        "funding_net_xbt": -funding_xbt,
        "counts": {key: dict(value.most_common()) for key, value in counters.items()},
        "liquidations": {
            "execution_rows": len(liquidations),
            "distinct_dates": len({item["time"].date() for item in liquidations}),
            "by_symbol": dict(Counter(item["symbol"] for item in liquidations)),
        },
    }
    return summary, xbt_fills, liquidations


def scan_wallet(path: Path) -> dict:
    by_symbol: dict[str, dict] = defaultdict(lambda: {"rows": 0, "pnl_xbt": 0.0, "wins": 0, "losses": 0})
    by_year: dict[str, dict] = defaultdict(lambda: {"pnl_xbt": 0.0, "deposits_xbt": 0.0, "withdrawals_xbt": 0.0})
    valid_rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if not row["date"]:
                continue
            amount = _float(row["amount"]) / 1e8
            balance = _float(row["walletbalance"]) / 1e8
            valid_rows.append({"date": row["date"], "type": row["transacttype"], "amount": amount, "balance": balance})
            year = row["date"][:4]
            if row["transacttype"] == "RealisedPNL":
                item = by_symbol[row["address"] or "<NA>"]
                item["rows"] += 1
                item["pnl_xbt"] += amount
                item["wins"] += amount > 0
                item["losses"] += amount < 0
                by_year[year]["pnl_xbt"] += amount
            elif row["transacttype"] == "Deposit":
                by_year[year]["deposits_xbt"] += amount
            elif row["transacttype"] == "Withdrawal":
                by_year[year]["withdrawals_xbt"] += amount

    mismatches = 0
    previous = None
    for row in valid_rows:
        if previous is not None and not math.isclose(previous + row["amount"], row["balance"], abs_tol=1e-8):
            mismatches += 1
        previous = row["balance"]
    return {
        "valid_rows": len(valid_rows),
        "realized_pnl_xbt": sum(item["pnl_xbt"] for item in by_symbol.values()),
        "deposits_xbt": sum(item["deposits_xbt"] for item in by_year.values()),
        "withdrawals_xbt": sum(item["withdrawals_xbt"] for item in by_year.values()),
        "last_reported_wallet_balance_xbt": valid_rows[-1]["balance"] if valid_rows else None,
        "balance_reconciliation_mismatch_rows": mismatches,
        "balance_reconciliation_passed": mismatches == 0,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda item: abs(item[1]["pnl_xbt"]), reverse=True)),
        "by_year": dict(by_year),
    }


def fetch_bitmex_xbtusd_hourly(start: datetime, end: datetime) -> list[dict]:
    bars: list[dict] = []
    cursor = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    while cursor < end:
        query = urlencode(
            {
                "binSize": "1h",
                "partial": "false",
                "symbol": "XBTUSD",
                "count": 1000,
                "reverse": "false",
                "startTime": cursor.isoformat().replace("+00:00", "Z"),
                "endTime": end.isoformat().replace("+00:00", "Z"),
            }
        )
        command = ["curl", "-fsS", "--max-time", "30", f"https://www.bitmex.com/api/v1/trade/bucketed?{query}"]
        page = json.loads(subprocess.check_output(command, text=True))
        if not page:
            break
        bars.extend(page)
        last = datetime.fromisoformat(page[-1]["timestamp"].replace("Z", "+00:00"))
        if last < cursor:
            raise RuntimeError("BitMEX hourly pagination did not advance")
        cursor = last + timedelta(hours=1)
        if len(page) < 1000:
            break
    return [item for _, item in sorted({item["timestamp"]: item for item in bars}.items())]


def market_context(episodes: list[dict], bars: list[dict]) -> dict:
    timestamps = [datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None) for item in bars]
    closes = [_float(item["close"]) for item in bars]
    volumes = [_float(item["volume"]) for item in bars]
    ema_200: list[float] = []
    alpha = 2.0 / 201.0
    current = closes[0]
    for close in closes:
        current = alpha * close + (1.0 - alpha) * current
        ema_200.append(current)

    observations: list[dict] = []
    for episode in episodes:
        index = bisect.bisect_left(timestamps, episode["start"]) - 1
        if index < 200 or index >= len(bars):
            continue
        side = 1 if episode["side"] == "long" else -1
        returns = {hours: closes[index] / closes[index - hours] - 1.0 for hours in (1, 4, 24, 168)}
        prior_high = max(_float(item["high"]) for item in bars[index - 24 : index])
        prior_low = min(_float(item["low"]) for item in bars[index - 24 : index])
        observations.append(
            {
                "net_pnl_xbt": episode["net_pnl_xbt"],
                "aligned_1h": side * returns[1] > 0,
                "aligned_4h": side * returns[4] > 0,
                "aligned_24h": side * returns[24] > 0,
                "aligned_168h": side * returns[168] > 0,
                "ema200_aligned": side * (closes[index] - ema_200[index]) > 0,
                "breakout_aligned": closes[index] >= prior_high if side > 0 else closes[index] <= prior_low,
                "volume_ratio": volumes[index] / statistics.median(volumes[index - 24 : index]),
            }
        )

    def group(items: list[dict]) -> dict:
        profits = [item["net_pnl_xbt"] for item in items if item["net_pnl_xbt"] > 0]
        losses = [-item["net_pnl_xbt"] for item in items if item["net_pnl_xbt"] < 0]
        return {
            "episodes": len(items),
            "win_rate": len(profits) / len(items) if items else None,
            "net_pnl_xbt": sum(item["net_pnl_xbt"] for item in items),
            "profit_factor": sum(profits) / sum(losses) if losses else None,
            "largest_winner_share": max(profits) / sum(profits) if profits else None,
        }

    groups = {"all": observations}
    for key in ("aligned_1h", "aligned_4h", "aligned_24h", "aligned_168h", "ema200_aligned", "breakout_aligned"):
        groups[key] = [item for item in observations if item[key]]
    groups["aligned_24h_with_1h_pullback"] = [
        item for item in observations if item["aligned_24h"] and not item["aligned_1h"]
    ]
    return {
        "source": "BitMEX public XBTUSD 1h trade/bucketed API",
        "bars": len(bars),
        "episodes_joined": len(observations),
        "entry_volume_ratio_median": statistics.median(item["volume_ratio"] for item in observations),
        "groups": {name: group(items) for name, items in groups.items()},
    }


def build_report(execution: dict, wallet: dict, episode_summary: dict, context: dict | None) -> str:
    liquidation = execution["liquidations"]
    context_text = "시장 문맥 대조를 실행하지 않음"
    if context:
        aligned = context["groups"]["aligned_24h"]
        pullback = context["groups"]["aligned_24h_with_1h_pullback"]
        context_text = (
            f"24시간 방향 일치 {aligned['episodes']}개(PF {aligned['profit_factor']:.2f}), "
            f"그중 1시간 되돌림 {pullback['episodes']}개(PF {pullback['profit_factor']:.2f})"
        )
    segments = episode_summary["behavior_segments"]
    maker_low = segments["entry_maker_share"]["below_50pct"]
    maker_high = segments["entry_maker_share"]["at_least_90pct"]
    few_orders = segments["entry_order_count"]["two_to_three"]
    many_orders = segments["entry_order_count"]["ten_or_more"]
    no_adverse = segments["position_adjustment"]["no_adverse_add"]
    adverse = segments["position_adjustment"]["has_adverse_add"]
    return f"""# 워뇨띠 공개 체결 내역 분석

## 데이터 범위와 품질

- 실행 행: {execution['execution_rows']:,}개, 실제 거래 체결 행: {execution['trade_fill_rows']:,}개
- 일반 주문: {execution['ordinary_orders']:,}개. 한 주문의 부분 체결 중앙값은 {execution['fills_per_order']['median']:.0f}개다.
- 지갑 원장의 실현손익 합계는 {wallet['realized_pnl_xbt']:.2f} XBT지만, 잔액 재계산 불일치가 {wallet['balance_reconciliation_mismatch_rows']:,}행이라 정확한 수익률·낙폭 곡선으로 사용하지 않는다.
- 강제청산 표시 체결은 {liquidation['execution_rows']}행, {liquidation['distinct_dates']}일에 존재한다.

## 반복 행동

- 일반 주문 체결 수량의 메이커 비중은 {execution['maker_quantity_share']:.1%}다. 시장가보다 지정가·부분 체결 관리가 핵심이다.
- XBTUSD 경제적 flat-to-flat(1계약 잔여 무시) 에피소드는 {episode_summary['episodes']:,}개, 중앙 보유시간은 {episode_summary['duration_minutes']['median']:.1f}분이다.
- 승리 에피소드 중앙 보유시간은 {episode_summary['winner_duration_median']:.1f}분, 손실은 {episode_summary['loser_duration_median']:.1f}분이다.
- 진입과 청산 모두 여러 주문으로 나눴고, 추가 진입은 유리한 방향 {episode_summary['favorable_add_orders']:,}회와 불리한 방향 {episode_summary['adverse_add_orders']:,}회가 모두 존재한다.
- 시장 문맥: {context_text}. 이는 원인 증명이 아니라 관측된 상관관계다.

## 행동 구간 진단

계좌 담보가 없어 실제 수익률을 계산할 수 없으므로 아래 PF는 `net_return_proxy` 기준이다. 포지션
크기의 영향을 줄인 비교값일 뿐이며 인과효과가 아니다.

| 관측 구간 | 에피소드 | 승률 | Proxy PF |
|---|---:|---:|---:|
| 진입 maker < 50% | {maker_low['episodes']:,} | {maker_low['win_rate']:.1%} | {maker_low['profit_factor_proxy']:.2f} |
| 진입 maker >= 90% | {maker_high['episodes']:,} | {maker_high['win_rate']:.1%} | {maker_high['profit_factor_proxy']:.2f} |
| 진입 주문 2~3개 | {few_orders['episodes']:,} | {few_orders['win_rate']:.1%} | {few_orders['profit_factor_proxy']:.2f} |
| 진입 주문 10개 이상 | {many_orders['episodes']:,} | {many_orders['win_rate']:.1%} | {many_orders['profit_factor_proxy']:.2f} |
| 불리한 추가진입 없음 | {no_adverse['episodes']:,} | {no_adverse['win_rate']:.1%} | {no_adverse['profit_factor_proxy']:.2f} |
| 불리한 추가진입 있음 | {adverse['episodes']:,} | {adverse['win_rate']:.1%} | {adverse['profit_factor_proxy']:.2f} |

높은 maker 비중은 좋은 신호라서가 아니라 체결되기 쉬운 시장 상태를 선택한 결과일 수 있다.
반대로 10회 이상 진입과 불리한 증액 구간의 PF 저하는 무제한 물타기를 복제하지 않을 근거를
강화한다. 따라서 시스템은 포지션 조정을 꺼둔 채, maker 효과는 실제 미체결까지 포함해 별도로
검증한다.

## 퀀트로 흡수할 요소

1. 24시간 방향 필터와 짧은 되돌림 진입을 독립 가설로 검증한다.
2. maker-first 주문을 사용하되 미체결과 대기 중 가격 이동을 비용으로 기록한다.
3. 진입·청산을 분할하되 총 위험예산은 첫 주문 전에 고정한다.
4. 승리보다 오래 지속되는 손실 포지션에는 시간 제한과 강제 축소 규칙을 둔다.
5. 펀딩·수수료·maker rebate를 가격손익과 분리한다.

## 복제하지 않을 요소

1. 강제청산과 무제한 물타기는 금지한다.
2. 공개 체결만으로 레버리지나 정확한 재량 신호를 추정하지 않는다.
3. 과거 BitMEX 역계약의 수익 규모를 Binance USDT-M 예상 수익률로 변환하지 않는다.
4. 이 자료에서 유도한 조건은 전부 개발 데이터로 간주하며, 새로운 미래 OOS 없이는 승격하지 않는다.
"""
