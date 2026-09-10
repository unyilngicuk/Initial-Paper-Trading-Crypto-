"""
Loss analysis -- learn from what went wrong, without letting the bot
rewrite its own rules.

DESIGN DECISION worth understanding before extending this:

It would be easy to make the bot auto-adjust its parameters after losses
(tighten the stop after a stop-out, widen the buffer after a whipsaw).
That is deliberately NOT what this does, because it is live curve-fitting:
reacting to whatever happened most recently, with a sample far too small
to distinguish signal from noise. A strategy making ~10-70 trades over two
years cannot statistically justify self-modification, and the same failure
mode already destroyed this project's original grid strategy.

Instead this classifies every loss by root cause, aggregates the pattern,
and reports it to a human. Changing the strategy stays a human decision
made against evidence -- which is how every improvement in this project
was actually found.

Loss categories:
  STOP_LOSS       - hit the hard stop; a genuine adverse move
  WHIPSAW         - exited on trend break, then the trend re-entered
                    shortly after; the exit was premature noise
  TREND_REVERSAL  - exited on trend break and the trend genuinely stayed
                    broken; the strategy did its job
  PERIOD_CLOSE    - closed by the calendar (profit protection), not by a
                    signal; a loss here is a timing artifact
  FEE_DRAG        - the price move was actually favourable or flat, but
                    costs turned it negative

Run: python3 loss_analysis.py --candles data/btcidr_15m_2y.csv --venue indodax_maker
"""

import argparse
from collections import Counter
from datetime import datetime, timezone

from backtest import load_candles, run
from config import Config

REENTRY_WINDOW_BARS = 96 * 3   # 3 days: re-entry inside this = whipsaw


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def classify(trades, cfg):
    """Assign a root cause to each completed round trip."""
    for i, t in enumerate(trades):
        reason = t["exit_reason"].lower()

        if "stop loss" in reason:
            t["cause"] = "STOP_LOSS"
        elif "period close" in reason:
            t["cause"] = "PERIOD_CLOSE"
        elif "trend break" in reason:
            # Whipsaw if the very next entry happened soon after this exit.
            nxt = trades[i + 1] if i + 1 < len(trades) else None
            if nxt and (nxt["entry_ts"] - t["exit_ts"]) <= REENTRY_WINDOW_BARS * 900:
                t["cause"] = "WHIPSAW"
            else:
                t["cause"] = "TREND_REVERSAL"
        else:
            t["cause"] = "OTHER"

        # Override: if the raw price move was non-negative but the net was a
        # loss, costs were the actual cause.
        if t["gross_pct"] >= 0 and t["net_idr"] < 0:
            t["cause"] = "FEE_DRAG"
    return trades


def build_trades(result):
    """Pair buys and sells by lot_id into completed round trips."""
    buys = {f.lot_id: f for f in result["fills"] if f.side == "buy" and f.lot_id is not None}
    trades = []
    for f in result["fills"]:
        if f.side != "sell" or f.lot_id not in buys:
            continue
        b = buys[f.lot_id]
        gross_pct = (f.price / b.price - 1) * 100
        net_idr = (f.gross_idr - f.fee_idr) - b.gross_idr
        trades.append({
            "entry_ts": b.ts, "exit_ts": f.ts,
            "entry_price": b.price, "exit_price": f.price,
            "gross_pct": gross_pct,
            "net_idr": net_idr,
            "fees_idr": b.fee_idr + f.fee_idr,
            "held_days": (f.ts - b.ts) / 86400,
            "exit_reason": f.reason,
        })
    trades.sort(key=lambda t: t["entry_ts"])
    return trades


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--show-all", action="store_true", help="list every losing trade, not just a summary")
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital

    candles = load_candles(args.candles)
    result = run(candles, cfg)
    trades = classify(build_trades(result), cfg)

    if not trades:
        raise SystemExit("no completed round trips to analyse")

    losers = [t for t in trades if t["net_idr"] < 0]
    winners = [t for t in trades if t["net_idr"] >= 0]

    print("=" * 78)
    print(f"  LOSS ANALYSIS -- {len(trades)} completed round trips  [{cfg.mode}]")
    print("=" * 78)
    print(f"  Winners: {len(winners)}   Losers: {len(losers)}   "
          f"Win rate: {len(winners)/len(trades)*100:.1f}%")
    if winners:
        print(f"  Avg winner: {sum(t['gross_pct'] for t in winners)/len(winners):+.2f}%   "
              f"total Rp {sum(t['net_idr'] for t in winners):,.0f}")
    if losers:
        print(f"  Avg loser:  {sum(t['gross_pct'] for t in losers)/len(losers):+.2f}%   "
              f"total Rp {sum(t['net_idr'] for t in losers):,.0f}")

    print("\n" + "-" * 78)
    print("  WHY THE LOSSES HAPPENED")
    print("-" * 78)
    causes = Counter(t["cause"] for t in losers)
    total_loss = sum(t["net_idr"] for t in losers) or 1
    for cause, n in causes.most_common():
        cost = sum(t["net_idr"] for t in losers if t["cause"] == cause)
        share = cost / total_loss * 100
        print(f"  {cause:<15} {n:>3} trades   Rp {cost:>12,.0f}   {share:>5.1f}% of all losses")

    print("\n" + "-" * 78)
    print("  WHAT THIS SUGGESTS (for a human to decide on -- the bot does not self-adjust)")
    print("-" * 78)

    whipsaw_cost = sum(t["net_idr"] for t in losers if t["cause"] == "WHIPSAW")
    fee_cost = sum(t["net_idr"] for t in losers if t["cause"] == "FEE_DRAG")
    stop_cost = sum(t["net_idr"] for t in losers if t["cause"] == "STOP_LOSS")
    period_cost = sum(t["net_idr"] for t in losers if t["cause"] == "PERIOD_CLOSE")
    total_fees = sum(t["fees_idr"] for t in trades)

    if abs(whipsaw_cost) > abs(total_loss) * 0.3:
        print(f"  * WHIPSAW is your biggest problem (Rp {whipsaw_cost:,.0f}).")
        print(f"    Consider: a wider trend buffer, or a cooldown before re-entry.")
    if abs(fee_cost) > abs(total_loss) * 0.2:
        print(f"  * FEE DRAG turned otherwise-flat trades into losses (Rp {fee_cost:,.0f}).")
        print(f"    Consider: a minimum-edge gate so small moves don't get traded at all.")
    if abs(stop_cost) > abs(total_loss) * 0.3:
        print(f"  * STOP-LOSSES dominate (Rp {stop_cost:,.0f}) -- real adverse moves, not noise.")
        print(f"    Consider: this may be the stop working correctly. Check if a wider")
        print(f"    stop would have recovered, or just deepened the losses.")
    if abs(period_cost) > abs(total_loss) * 0.15:
        print(f"  * PERIOD-CLOSE losses (Rp {period_cost:,.0f}) are calendar artifacts --")
        print(f"    positions forced shut by the profit-protection schedule, not by a signal.")
    print(f"  * Total fees across all trades: Rp {total_fees:,.0f} "
          f"({total_fees/args.capital*100:.1f}% of starting capital).")

    if args.show_all and losers:
        print("\n" + "-" * 78)
        print("  EVERY LOSING TRADE")
        print("-" * 78)
        print(f"  {'entry':<17} {'exit':<17} {'held':>7} {'gross':>8} {'net IDR':>12}  cause")
        for t in losers:
            print(f"  {_fmt(t['entry_ts']):<17} {_fmt(t['exit_ts']):<17} "
                  f"{t['held_days']:>6.1f}d {t['gross_pct']:>7.2f}% "
                  f"{t['net_idr']:>12,.0f}  {t['cause']}")

    print("=" * 78)


if __name__ == "__main__":
    main()
