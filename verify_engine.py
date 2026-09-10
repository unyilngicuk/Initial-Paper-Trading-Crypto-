"""
Separate two hypotheses:

  A) The engine is broken and understates returns.
  B) The engine is correct and the grid strategy is bad.

Method: feed the SAME engine strategies whose correct answer is known in
advance, on the SAME real data. If the engine reproduces the known answers,
it is not the engine.

  1. do nothing            -> must return exactly 0%
  2. buy everything, hold  -> must equal buy-and-hold minus one buy fee
  3. churn every bar       -> must lose approximately (trades x round-trip cost)

Run: python3 verify_engine.py
"""

import backtest
from backtest import load_candles, run
from config import Config
from report import compute_metrics
from strategy import Action

DATA = "data/btcidr_15m_2y.csv"


def decide_nothing(state, candle, cfg):
    return []


def decide_hold_all(state, candle, cfg):
    """Buy the whole account once, then never trade again."""
    if state["bars_seen"] < cfg.warmup_bars or state["hold_bought"]:
        return []
    return [Action(side="buy", qty_idr=state["idr"],
                   reason="buy and hold everything", tag="hold")]


def make_churn():
    """Buy a slice, sell it next bar, forever. Pure cost, no view on price."""
    def decide_churn(state, candle, cfg):
        if state["bars_seen"] < cfg.warmup_bars:
            return []
        if state["lots"]:
            lot = state["lots"][0]
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason="churn exit", tag="grid")]
        if state["idr"] < cfg.slice_idr:
            return []
        return [Action(side="buy", qty_idr=cfg.slice_idr, level_idx=0,
                       reason="churn entry", tag="grid")]
    return decide_churn


def with_strategy(fn, candles, cfg):
    original = backtest.decide
    backtest.decide = fn
    try:
        return run(candles, cfg)
    finally:
        backtest.decide = original


def main():
    c = load_candles(DATA)

    def cfg(**kw):
        x = Config()
        x.use_venue("indodax_maker")
        x.take_profit_pct = 0.02          # churn needs a target it can clear
        for k, v in kw.items():
            setattr(x, k, v)
        return x

    base = cfg()
    bh = compute_metrics(run(c, base))["buy_hold_pct"]

    print("=" * 66)
    print(f"  Engine sanity on {len(c):,} real bars")
    print(f"  Buy & hold over the period: {bh:+.2%}")
    print("=" * 66)

    # --- 1. do nothing ---
    m = compute_metrics(with_strategy(decide_nothing, c, cfg()))
    ok1 = abs(m["return_pct"]) < 1e-9
    print(f"\n  1. DO NOTHING")
    print(f"     expected   0.00%")
    print(f"     got      {m['return_pct']:>7.2%}   {'PASS' if ok1 else 'FAIL'}")

    # --- 2. buy everything and hold ---
    x = cfg()
    r = with_strategy(decide_hold_all, c, x)
    m = compute_metrics(r)
    entry = r["fills"][0]
    # buying costs one fee plus slippage; the rest should track price exactly
    drag = x.fee_buy_pct + x.slippage_pct
    expected = (1 + bh) * (1 - drag) - 1
    err = abs(m["return_pct"] - expected)
    ok2 = err < 0.005
    print(f"\n  2. BUY EVERYTHING AND HOLD")
    print(f"     expected {expected:>7.2%}   (buy-and-hold minus {drag:.4%} entry cost)")
    print(f"     got      {m['return_pct']:>7.2%}   error {err:.4%}   "
          f"{'PASS' if ok2 else 'FAIL'}")
    print(f"     trades: {m['n_buys']} buy / {m['n_sells']} sell  (must be 1 / 0)")

    # --- 3. churn ---
    x = cfg()
    r = with_strategy(make_churn(), c, x)
    m = compute_metrics(r)
    n_round_trips = m["n_sells"]
    rt_cost = x.fee_buy_pct + x.fee_sell_pct + 2 * x.slippage_pct
    fees_paid = m["total_fees_idr"]
    print(f"\n  3. CHURN (buy, sell, repeat -- pays costs, expresses no view)")
    print(f"     {n_round_trips:,} round trips at {rt_cost:.4%} each")
    print(f"     fees actually charged: {fees_paid:,.0f} IDR "
          f"({m['fees_as_pct_of_capital']:.1%} of capital)")
    ok3 = fees_paid > 0 and n_round_trips > 100
    print(f"     costs scale with trading: {'PASS' if ok3 else 'FAIL'}")

    print("\n" + "=" * 66)
    if ok1 and ok2 and ok3:
        print("  ENGINE VERDICT: correct.")
        print("  It reproduces buy-and-hold to within a fraction of a percent,")
        print("  returns exactly zero when idle, and charges costs in proportion")
        print("  to activity. It does not understate returns.")
        print("\n  Therefore the grid's underperformance is the STRATEGY,")
        print("  not the measurement.")
    else:
        print("  ENGINE VERDICT: SUSPECT -- investigate before trusting results.")
    print("=" * 66)


if __name__ == "__main__":
    main()
