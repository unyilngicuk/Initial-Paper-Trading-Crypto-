"""
WRAPPER-family switching: Usro (trending) + ORB (flat/declining).

ONE pre-committed experiment, same discipline as the native-crypto
switching investigation (see spec Section 11d).

MOTIVATION, from real data: ORB beat buy-and-hold on TSLAX during its
decline (+9.45 edge) but lost badly to buy-and-hold on NVDAX and AAPLX
during their rallies (-19.33, -26.21 edge). This tests whether
switching between ORB and Usro, based on a simple trend read, captures
the better of the two depending on conditions.

WHY NOT regime_manager.py: that classifier (SMA/ADX/ATR on continuous
15-min native-crypto data) was never validated for wrapper assets'
sparse, thin-liquidity pattern. Instead: a daily close vs. N-days-ago
close, computed on THIS asset's own real 60-minute data.

WHY 60-MINUTE BARS: this is where ORB's actual signal was found -- 15
minute bars were too sparse to reveal it.

WHY USRO'S TREND PERIOD IS RESCALED: usro_trend_period (480) means "5
trading days" at its native 15-minute calibration. Run unchanged on
60-minute bars, the same bar count means 20 days -- a silently
different, unintended lookback. cfg.usro_trend_period_60m (120 = 480/4)
preserves the original real-time meaning.

SWITCHING MECHANICS, learned directly from the native-crypto experiment
that got this wrong the first time: a switch only takes effect while
FLAT. Whoever is currently holding a position manages its own exit,
undisturbed, regardless of what the trend signal does in the meantime.

PRE-COMMITTED SUCCESS BAR (decided before running): the switching
return must exceed BOTH running Usro alone AND running ORB alone on the
same asset -- not just beat the worse of the two. Short of that = a
recorded negative result, not a starting point for further tuning.

Run: python3 wrapper_switch_simulation.py --candles data/nvdaxidr_60m.csv --capital 1000000
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles
from config import Config
from engine import apply_fill, check_drawdown, verify_balance

BARS_PER_DAY_60M = 24


def _trend_signal(closes, lookback_bars, smoothing_bars):
    """
    Smoothed, not single-point: averages `smoothing_bars` at the recent
    end and at the lookback point, rather than comparing two individual
    closes. Real bug found during testing: single-point comparison
    whipsawed violently on thin wrapper liquidity (AAPLX flipped within
    one hour, TSLAX switched 7 times in a single day, for a signal
    meant to represent a 5-day trend).
    """
    needed = lookback_bars + smoothing_bars
    if len(closes) < needed:
        return "UP"
    recent_avg = sum(closes[-smoothing_bars:]) / smoothing_bars
    past_window = closes[-lookback_bars - smoothing_bars:-lookback_bars]
    past_avg = sum(past_window) / smoothing_bars
    return "UP" if recent_avg >= past_avg else "DOWN"


def run_switching(candles, cfg):
    import strategy_usro
    import strategy_orb
    from asset_type_detector import detect_session_start_hour

    if cfg.orb_session_start_hour is None:
        detected = detect_session_start_hour(candles)
        if detected is not None:
            cfg.orb_session_start_hour = detected
            print(f"  (auto-detected ORB session start hour: {detected}:00 UTC)")

    usro_cfg = Config(**{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()})
    usro_cfg.usro_trend_period = cfg.usro_trend_period_60m

    closes_all = [c["close"] for c in candles]
    lookback_bars = cfg.wrapper_trend_lookback_days * BARS_PER_DAY_60M
    smoothing_bars = cfg.wrapper_trend_smoothing_bars
    min_bars_between = cfg.wrapper_min_bars_between_switches

    active_name = "usro"
    state = strategy_usro.new_state(usro_cfg)
    decide_fn = strategy_usro.decide

    fills = []
    switch_log = []
    equity_curve = []
    bars_since_last_switch = min_bars_between  # allow an initial switch immediately

    for i, candle in enumerate(candles):
        signal = _trend_signal(closes_all[:i + 1], lookback_bars, smoothing_bars)
        target_name = "usro" if signal == "UP" else "orb"
        bars_since_last_switch += 1

        can_switch = (not state["lots"] and target_name != active_name
                      and bars_since_last_switch >= min_bars_between)

        if can_switch:
            bars_since_last_switch = 0
            carried_idr = state["idr"]
            carried_principal = state["principal"]
            carried_reserve = state["profit_reserve"]
            carried_period_start = state["period_start_ts"]

            switch_log.append({"ts": candle["ts"], "from": active_name,
                               "to": target_name, "signal": signal,
                               "idr_at_switch": carried_idr})

            active_name = target_name
            if active_name == "usro":
                decide_fn = strategy_usro.decide
                state = strategy_usro.new_state(usro_cfg)
                start = max(0, i - usro_cfg.history_window + 1)
                state["closes"] = list(closes_all[start:i + 1])
                state["bars_seen"] = len(state["closes"])
            else:
                decide_fn = strategy_orb.decide
                state = strategy_orb.new_state(cfg)
                state["bars_seen"] = 10 ** 6
            state["idr"] = carried_idr
            state["principal"] = carried_principal
            state["profit_reserve"] = carried_reserve
            state["period_start_ts"] = carried_period_start
        elif active_name == "usro":
            state["closes"].append(candle["close"])
            if len(state["closes"]) > usro_cfg.history_window:
                state["closes"] = state["closes"][-usro_cfg.history_window:]
            state["bars_seen"] += 1

        active_cfg = usro_cfg if active_name == "usro" else cfg
        check_drawdown(state, candle["close"], active_cfg)
        for action in decide_fn(state, candle, active_cfg):
            apply_fill(state, action, candle, active_cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"

        eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
        equity_curve.append((candle["ts"], eq, candle["close"]))

    return {"final_state": state, "fills": fills, "switch_log": switch_log,
            "equity_curve": equity_curve, "active_at_end": active_name}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--coin", type=str, default="wrapper")
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital
    cfg.coin = args.coin.lower()
    cfg.orb_range_bars = 1

    candles = load_candles(args.candles)

    print("=" * 70)
    print("  PRE-COMMITTED EXPERIMENT")
    print("=" * 70)
    print("  Success bar: switching return must exceed BOTH Usro alone AND")
    print("  ORB alone on this asset -- not just the worse of the two.")
    print("  Short of that = recorded negative result, no further tuning.")
    print("=" * 70)
    print()

    result = run_switching(candles, cfg)
    curve = result["equity_curve"]
    fills = result["fills"]

    start_eq = args.capital
    end_eq = curve[-1][1]
    hold_final = (args.capital / candles[0]["close"]) * candles[-1]["close"]

    print(f"SWITCHING PORTFOLIO ({len(result['switch_log'])} switches)")
    print("-" * 70)
    print(f"  Start equity:  Rp {start_eq:,.0f}")
    print(f"  End equity:    Rp {end_eq:,.0f}")
    print(f"  Return:        {(end_eq/start_eq - 1)*100:+.2f}%")
    print(f"  Buy & hold:    {(hold_final/start_eq - 1)*100:+.2f}%")
    print(f"  Edge vs B&H:   {(end_eq/start_eq - hold_final/start_eq)*100:+.2f} pts")
    print(f"  Active strategy at end: {result['active_at_end']}")
    print(f"  Total trades: {len([f for f in fills if f.side=='buy'])}")

    print(f"\n  Switch log:")
    for s in result["switch_log"]:
        dt = datetime.fromtimestamp(s["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"    {dt}  {s['from']:>6} -> {s['to']:<6}  (signal: {s['signal']}, "
              f"equity Rp {s['idr_at_switch']:,.0f})")


if __name__ == "__main__":
    main()
