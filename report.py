"""Metrics and output for a replay run."""

import csv
import math
from datetime import datetime, timezone
from typing import Any, Dict, List


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def compute_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    cfg = result["cfg"]
    curve = result["equity_curve"]
    fills = result["fills"]

    if not curve:
        return {}

    start_eq = cfg.starting_idr
    end_eq = curve[-1][1]

    peak = -math.inf
    max_dd = 0.0
    for _, eq, _ in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - eq / peak)

    # Buy and hold benchmark, measured from the first bar the bot could ACT on.
    # Measuring from bar 0 credits (or penalises) the strategy for price drift
    # during the warmup window, when it was structurally unable to trade. On
    # the first real dataset that drift was +1.43% against a measured edge of
    # +1.57% -- i.e. it was almost the entire result.
    start_idx = min(cfg.warmup_bars, len(result["candles"]) - 1)
    first_px = result["candles"][start_idx]["close"]
    last_px = result["candles"][-1]["close"]
    bh_return = (last_px / first_px) - 1.0

    sells = [f for f in fills if f.side == "sell"]
    buys = [f for f in fills if f.side == "buy"]
    total_fees = sum(f.fee_idr for f in fills)

    # Per-cycle P&L, matched by lot_id rather than by guessing from the
    # reason string -- this works for any strategy's exit wording.
    buy_price_by_lot = {f.lot_id: f.price for f in buys if f.lot_id is not None}
    wins = sum(1 for f in sells
               if f.lot_id in buy_price_by_lot and f.price > buy_price_by_lot[f.lot_id])

    span_days = (curve[-1][0] - curve[0][0]) / 86400 if len(curve) > 1 else 0

    return {
        "start_equity": start_eq,
        "end_equity": end_eq,
        "return_pct": (end_eq / start_eq) - 1.0,
        "buy_hold_pct": bh_return,
        "max_drawdown_pct": max_dd,
        "true_max_drawdown_pct": result["state"].get("true_max_dd", max_dd),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "n_wins": wins,
        "total_fees_idr": total_fees,
        "fees_as_pct_of_capital": total_fees / start_eq if start_eq else 0,
        "span_days": span_days,
        "halted": result["state"]["halted"],
        "open_lots": len(result["state"]["lots"]),
        "first_ts": curve[0][0],
        "last_ts": curve[-1][0],
    }


def print_report(result: Dict[str, Any]) -> None:
    m = compute_metrics(result)
    cfg = result["cfg"]
    if not m:
        print("No data.")
        return

    print("=" * 62)
    print(f"  REPLAY: {cfg.coin.upper()}/{cfg.quote}  {cfg.interval}")
    print(f"  {_fmt_ts(m['first_ts'])} -> {_fmt_ts(m['last_ts'])}"
          f"  ({m['span_days']:.0f} days)")
    print("=" * 62)
    strategy_name = result["state"].get("strategy_name", "rsi_grid")
    if strategy_name == "guardian":
        print(f"  strategy: Unyil Guardian (conservative capital preservation)")
        print(f"  trends: {cfg.guardian_trend_fast}-bar / {cfg.guardian_trend_slow}-bar "
              f"(both must align to enter)")
        print(f"  exposure cap {cfg.guardian_max_exposure:.0%} | stop {cfg.guardian_stop_pct:.0%} "
              f"| trail {cfg.guardian_trail_pct:.0%} | circuit breaker {cfg.max_drawdown_pct:.0%}")
    elif strategy_name == "usro":
        print(f"  strategy: Unyil Usro (bull-market specialist)")
        print(f"  trend: {cfg.usro_trend_period}-bar (fast) | "
              f"entry buffer {cfg.usro_entry_buffer_pct:.1%}")
        print(f"  exposure {cfg.usro_max_exposure:.0%} | trailing stop "
              f"{cfg.usro_trail_pct:.0%} | circuit breaker {cfg.max_drawdown_pct:.0%}")
    elif strategy_name == "unyil2":
        print(f"  strategy: Unyil 2.0 (trend {cfg.trend_ma_period}-bar SMA, "
              f"buffer {cfg.trend_buffer_pct:.1%}, stop {cfg.stop_loss_pct:.0%})")
        print(f"  sizing: base risk {cfg.base_risk_frac:.0%}, "
              f"floor {cfg.min_risk_frac:.0%} (adaptive on win/loss)")
    elif strategy_name == "orb":
        session_hour = cfg.orb_session_start_hour
        print(f"  strategy: Unyil ORB (WRAPPER family: Opening Range Breakout)")
        print(f"  session start: {session_hour}:00 UTC | range: {cfg.orb_range_bars} bars "
              f"| breakout buffer {cfg.orb_breakout_buffer_pct:.2%}")
        print(f"  exposure {cfg.orb_max_exposure:.0%} | stop {cfg.orb_stop_pct:.0%} "
              f"| trail {cfg.orb_trail_pct:.0%} | circuit breaker {cfg.max_drawdown_pct:.0%}")
    elif strategy_name == "gap":
        session_hour = cfg.orb_session_start_hour
        print(f"  strategy: Unyil GAP (WRAPPER family: Gap-{cfg.gap_mode.capitalize()})")
        print(f"  session start: {session_hour}:00 UTC | min gap {cfg.gap_min_pct:.2%} "
              f"| mode: {cfg.gap_mode}")
        print(f"  exposure {cfg.gap_max_exposure:.0%} | stop {cfg.gap_stop_pct:.0%} "
              f"| trail {cfg.gap_trail_pct:.0%} | circuit breaker {cfg.max_drawdown_pct:.0%}")
    else:
        print(f"  grid {cfg.num_levels} levels @ {cfg.spacing_pct:.2%} spacing"
              f" | TP {cfg.take_profit_pct:.2%}")
        print(f"  allocation {cfg.grid_allocation:.0%} grid / {cfg.hold_allocation:.0%} hold")
    print(f"  fee buy {cfg.fee_buy_pct:.4%} / sell {cfg.fee_sell_pct:.4%}"
          f" | slippage {cfg.slippage_pct:.3%}/side  [{cfg.mode} mode]")
    print(f"  fills: {cfg.fill_model}")
    print("-" * 62)
    print(f"  Start equity      {m['start_equity']:>18,.0f} IDR")
    print(f"  End equity        {m['end_equity']:>18,.0f} IDR")
    print(f"  Return            {m['return_pct']:>18.2%}")
    print(f"  Buy & hold        {m['buy_hold_pct']:>18.2%}")
    print(f"  Edge vs B&H       {m['return_pct'] - m['buy_hold_pct']:>18.2%}")
    print("-" * 62)
    print(f"  Max drawdown (settled)  {m['max_drawdown_pct']:>12.2%}")
    if abs(m['true_max_drawdown_pct'] - m['max_drawdown_pct']) > 0.001:
        print(f"  Max drawdown (live, pre-exit) {m['true_max_drawdown_pct']:>6.2%}"
              f"  <- what actually tripped the breaker, before any same-bar rescue exit")
    print(f"  Halted            {str(m['halted']):>18}")
    print(f"  Open lots at end  {m['open_lots']:>18}")
    print("-" * 62)
    print(f"  Buys / Sells      {m['n_buys']:>9} / {m['n_sells']:<8}")
    print(f"  Total fees paid   {m['total_fees_idr']:>18,.0f} IDR")
    print(f"  Fees as % capital {m['fees_as_pct_of_capital']:>18.2%}")
    state = result["state"]
    adaptive_log = state.get("adaptive_log") or []
    if adaptive_log:
        print("-" * 62)
        print(f"  Adaptive sizing adjustments: {len(adaptive_log)}")
        for entry in adaptive_log:
            print(f"    {entry['from']:.2f} -> {entry['to']:.2f}  ({entry['reason']})")
    if "profit_reserve" in state and state["profit_reserve"] > 0:
        print("-" * 62)
        print(f"  Profit reserve    {state['profit_reserve']:>18,.0f} IDR  (protected, never re-risked)")
        print(f"  Investable cash   {state['idr'] - state['profit_reserve']:>18,.0f} IDR")
    print("=" * 62)

    if result["events"]:
        print("\n  Events:")
        for e in result["events"][:20]:
            print(f"    {e}")
        if len(result["events"]) > 20:
            print(f"    ... and {len(result['events']) - 20} more")


def write_equity_csv(result: Dict[str, Any], path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "datetime", "equity_idr", "price"])
        for ts, eq, px in result["equity_curve"]:
            w.writerow([ts, _fmt_ts(ts), f"{eq:.2f}", f"{px:.2f}"])
