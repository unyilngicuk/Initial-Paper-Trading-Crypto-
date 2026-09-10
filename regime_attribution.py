"""
Regime attribution -- breaks down a strategy's REAL trades (from the same
continuous backtest already used everywhere else) by which regime was
CONFIRMED at the moment of entry, per regime_manager.py's classification.

This deliberately does NOT run a separate "sideways-only" backtest on
discontinuous chunks of data spliced together -- that would corrupt every
strategy's own trend memory at the splice points and produce a result
that's an artifact of the splicing, not a real answer. Instead, this
reuses the one real, continuous backtest and asks: of the trades that
actually happened, how did the ones entered during each regime turn out?

Run: python3 regime_attribution.py --candles data/btcidr_15m_2y.csv --venue indodax_maker
"""

import argparse
from collections import defaultdict

from backtest import load_candles, run
from config import Config
from regime_manager import apply_persistence, classify_all


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--capital", type=float, default=1_000_000)
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital

    candles = load_candles(args.candles)
    print(f"Classifying regime across {len(candles):,} bars...")
    raw = classify_all(candles)
    entries = apply_persistence(raw)
    regime_by_ts = {candles[i]["ts"]: entries[i]["confirmed"] for i in range(len(candles))}

    print("Running the active strategy's real backtest...")
    result = run(candles, cfg)
    fills = result["fills"]

    buys = {f.lot_id: f for f in fills if f.side == "buy" and f.lot_id is not None}
    trades = []
    for f in fills:
        if f.side != "sell" or f.lot_id not in buys:
            continue
        b = buys[f.lot_id]
        net_idr = (f.gross_idr - f.fee_idr) - b.gross_idr
        regime_at_entry = regime_by_ts.get(b.ts, "UNKNOWN")
        trades.append({
            "entry_ts": b.ts, "regime": regime_at_entry,
            "gross_pct": (f.price / b.price - 1) * 100,
            "net_idr": net_idr,
        })

    if not trades:
        print("No completed round trips to attribute.")
        return

    by_regime = defaultdict(list)
    for t in trades:
        by_regime[t["regime"]].append(t)

    strategy_name = result["state"].get("strategy_name", "unknown")
    print(f"\n{'='*70}")
    print(f"  REGIME ATTRIBUTION -- {strategy_name}, {len(trades)} completed round trips")
    print(f"{'='*70}")
    print(f"{'Regime at entry':<15} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Total net IDR':>15} {'Avg %':>8}")
    print("-" * 70)
    for regime in ["BULLISH", "BEARISH", "SIDEWAYS", "UNCERTAIN", "UNKNOWN"]:
        if regime not in by_regime:
            continue
        ts = by_regime[regime]
        wins = sum(1 for t in ts if t["net_idr"] > 0)
        total_net = sum(t["net_idr"] for t in ts)
        avg_pct = sum(t["gross_pct"] for t in ts) / len(ts)
        print(f"{regime:<15} {len(ts):>7} {wins:>6} {wins/len(ts)*100:>6.0f}% "
              f"{total_net:>15,.0f} {avg_pct:>7.2f}%")
    print("=" * 70)

    sideways_trades = by_regime.get("SIDEWAYS", [])
    if sideways_trades:
        net = sum(t["net_idr"] for t in sideways_trades)
        verdict = "profitable" if net > 0 else "a net loss"
        print(f"\nSideways-entered trades were {verdict} for {strategy_name}: "
              f"{net:,.0f} IDR across {len(sideways_trades)} trades.")
    else:
        print(f"\n{strategy_name} entered ZERO trades while the regime manager confirmed "
              f"SIDEWAYS -- meaning its own entry rule already avoids sideways markets "
              f"on its own, without needing regime input.")


if __name__ == "__main__":
    main()
