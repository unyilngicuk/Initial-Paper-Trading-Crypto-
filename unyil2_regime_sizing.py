"""
Unyil 2.0 + regime-aware sizing -- ONE pre-committed experiment.

This does NOT switch strategies. Unyil 2.0's own entry/exit/stop logic
runs completely unchanged, the whole time. The only thing this adds:
when regime_manager.py confirms high-confidence (5/5) Bearish, new
entries get sized down by cfg.regime_bearish5_risk_multiplier. Every
other bar, the multiplier is 1.0 -- no effect at all.

WHY THIS, NOT ANOTHER ENGINE SWITCH: the regime-switching experiment
found that switching into Guardian on EVERY Bearish confirmation
underperformed running Unyil 2.0 continuously; restricting that switch
to only 5/5 Bearish closed most (not all) of the gap. This asks a
narrower question with the same restriction: does shrinking Unyil 2.0's
OWN size on that same narrow signal help, without an engine swap at all.

PRE-COMMITTED SUCCESS BAR (decided before running this):
  - Return within ~5 percentage points of the +91.76% single-strategy
    baseline (real 2-year data), AND
  - Max drawdown measurably improved from the 15.17% baseline.
Short of both = a recorded failure. No further threshold tuning after
seeing the result.

Run: python3 unyil2_regime_sizing.py --candles data/btcidr_15m_2y.csv --capital 1000000
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
    cut_bars = 0

    print("Classifying regime across the full period...")
    raw = classify_all(candles)
    confirmed = apply_persistence(raw)

    for i, candle in enumerate(candles):
        is_high_confidence_bearish = (
            confirmed[i]["confirmed"] == "BEARISH"
            and confirmed[i]["confirmed_key"] == "BEARISH_5"
        )
        state["regime_risk_multiplier"] = (
            cfg.regime_bearish5_risk_multiplier if is_high_confidence_bearish else 1.0
        )
        if is_high_confidence_bearish:
            cut_bars += 1

        state["closes"].append(candle["close"])
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1

        check_drawdown(state, candle["close"], cfg)
        for action in decide(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"

        eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
        equity_curve.append((candle["ts"], eq, candle["close"]))

    return {"final_state": state, "fills": fills, "equity_curve": equity_curve,
            "cut_bars": cut_bars}


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
    print("  Unyil 2.0's own logic is UNCHANGED. Only new-entry size is cut")
    print("  on high-confidence (5/5) Bearish confirmation.")
    print()
    print("  SUCCESS BAR: return within ~5 pts of +91.76% baseline AND")
    print("  max drawdown measurably improved from the 15.17% baseline.")
    print("  Short of both = recorded failure, no further tuning.")
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
    print(f"Bars with size cut active: {result['cut_bars']:,} "
          f"({result['cut_bars']/len(candles)*100:.1f}% of the period)")
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
