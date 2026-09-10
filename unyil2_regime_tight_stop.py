"""
Unyil 2.0 + regime-aware stop tightening -- ONE pre-committed experiment.

Does NOT switch strategies, and does NOT touch new-entry sizing (that was
tried in unyil2_regime_sizing.py and failed -- Unyil 2.0 only opens
positions above its trend, while high-confidence Bearish means price is
well below trend, so the two conditions almost never co-occur; sizing
new entries during Bearish had nothing to act on).

This targets an EXISTING open position's exit instead, which has no such
conflict: a position can be open regardless of what triggered it, so
tightening its stop when the regime turns severely bearish is a signal
that can actually engage.

Mechanism: while holding a position, if regime_manager.py confirms
high-confidence (5/5) Bearish, Unyil 2.0's own stop-loss tightens from
cfg.stop_loss_pct (15%) to cfg.regime_bearish5_tight_stop_pct (8%).
Every other bar, the normal 15% stop applies unchanged.

PRE-COMMITTED SUCCESS BAR (decided before running this):
  - Max drawdown measurably improved from the 15.17% single-strategy
    baseline (real 2-year data), AND
  - Return stays within ~5 percentage points of the +91.76% baseline.
Short of both = a recorded failure. No further threshold tuning after
seeing the result.

Run: python3 unyil2_regime_tight_stop.py --candles data/btcidr_15m_2y.csv --capital 1000000
"""

import argparse

from backtest import load_candles
from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from regime_manager import apply_persistence, classify_all
from strategy_unyil2 import decide, new_state


def run(candles, cfg):
    state = new_state(cfg)
    fills = []
    equity_curve = []
    tight_bars = 0
    tightened_exits = 0

    print("Classifying regime across the full period...")
    raw = classify_all(candles)
    confirmed = apply_persistence(raw)

    for i, candle in enumerate(candles):
        is_high_confidence_bearish = (
            confirmed[i]["confirmed"] == "BEARISH"
            and confirmed[i]["confirmed_key"] == "BEARISH_5"
        )
        state["stop_pct_override"] = (
            cfg.regime_bearish5_tight_stop_pct if is_high_confidence_bearish else None
        )
        if is_high_confidence_bearish:
            tight_bars += 1

        state["closes"].append(candle["close"])
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1

        check_drawdown(state, candle["close"], cfg)
        for action in decide(state, candle, cfg):
            if "tightened stop loss" in action.reason:
                tightened_exits += 1
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"

        eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
        equity_curve.append((candle["ts"], eq, candle["close"]))

    return {"final_state": state, "fills": fills, "equity_curve": equity_curve,
            "tight_bars": tight_bars, "tightened_exits": tightened_exits}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital

    candles = load_candles(args.candles)

    print("=" * 70)
    print("  PRE-COMMITTED EXPERIMENT -- decided before running")
    print("=" * 70)
    print("  Unyil 2.0's entries and normal exits are UNCHANGED. Only an")
    print("  OPEN position's stop-loss tightens (15% -> 8%) during a")
    print("  high-confidence (5/5) Bearish confirmation.")
    print()
    print("  SUCCESS BAR: max drawdown measurably improved from the 15.17%")
    print("  baseline AND return within ~5 pts of +91.76%. Short of both =")
    print("  recorded failure, no further tuning.")
    print("=" * 70)
    print()

    result = run(candles, cfg)
    curve = result["equity_curve"]
    state = result["final_state"]
    fills = result["fills"]

    start_eq = args.capital
    end_eq = curve[-1][1]
    hold_final = (args.capital / candles[0]["close"]) * candles[-1]["close"]

    peak = start_eq
    max_dd = 0.0
    for _, eq, _ in curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    print(f"Start equity:   Rp {start_eq:,.0f}")
    print(f"End equity:     Rp {end_eq:,.0f}")
    print(f"Return:         {(end_eq/start_eq - 1)*100:+.2f}%")
    print(f"Buy & hold:     {(hold_final/start_eq - 1)*100:+.2f}%")
    print(f"Edge vs B&H:    {(end_eq/start_eq - hold_final/start_eq)*100:+.2f} pts")
    print(f"Max drawdown:   {max_dd*100:.2f}%")
    print(f"Profit reserve: Rp {state['profit_reserve']:,.0f}")
    print(f"Bars with tightened stop active: {result['tight_bars']:,} "
          f"({result['tight_bars']/len(candles)*100:.1f}% of the period)")
    print(f"Exits actually caused by the tightened stop: {result['tightened_exits']}")
    print(f"Total trades: {len([f for f in fills if f.side=='buy'])}")

    print()
    print("=" * 70)
    print("  VERDICT AGAINST THE PRE-COMMITTED BAR")
    print("=" * 70)
    ret_pct = (end_eq/start_eq - 1) * 100
    baseline_ret = 91.76
    baseline_dd = 15.17
    ret_ok = ret_pct >= (baseline_ret - 5)
    dd_ok = max_dd * 100 < baseline_dd
    print(f"  Return within ~5 pts of baseline ({baseline_ret}%)? "
          f"{'YES' if ret_ok else 'NO'} ({ret_pct:+.2f}%)")
    print(f"  Max drawdown improved from baseline ({baseline_dd}%)? "
          f"{'YES' if dd_ok else 'NO'} ({max_dd*100:.2f}%)")
    print(f"  OVERALL: {'PASS' if (ret_ok and dd_ok) else 'FAIL'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
