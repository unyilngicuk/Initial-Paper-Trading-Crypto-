"""
Prints every fill (buy and sell) from a backtest run, with date, price,
and the strategy's stated reason -- to diagnose WHY a strategy under- or
over-performed, not just by how much.

Run: python3 trade_log.py --candles data/btcidr_15m_2y.csv --venue indodax_maker
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles, run
from config import Config


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--coin", type=str, default="btc")
    p.add_argument("--gap-mode", type=str, default=None, choices=["go", "fade"])
    p.add_argument("--gap-min-pct", type=float, default=None)
    p.add_argument("--gap-fade-ema", action="store_true")
    p.add_argument("--orb-session-hour", type=int, default=None)
    p.add_argument("--orb-range-bars", type=int, default=None)
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.coin = args.coin.lower()
    if args.gap_mode is not None:
        cfg.gap_mode = args.gap_mode
    if args.gap_min_pct is not None:
        cfg.gap_min_pct = args.gap_min_pct
    if args.gap_fade_ema:
        cfg.gap_fade_ema_enabled = True
    if args.orb_session_hour is not None:
        cfg.orb_session_start_hour = args.orb_session_hour
    if args.orb_range_bars is not None:
        cfg.orb_range_bars = args.orb_range_bars

    candles = load_candles(args.candles)

    try:
        import strategy as _active_strategy_module
        strategy_family = getattr(_active_strategy_module, "STRATEGY_FAMILY", None)
        if strategy_family == "WRAPPER" and cfg.orb_session_start_hour is None:
            from asset_type_detector import detect_session_start_hour
            detected = detect_session_start_hour(candles)
            if detected is not None:
                cfg.orb_session_start_hour = detected
                print(f"  (auto-detected session start hour: {detected}:00 UTC)")
    except Exception:
        pass

    result = run(candles, cfg)

    print(f"{'date':<17} {'side':<5} {'price':>14} {'reason'}")
    print("-" * 80)

    entry_price_by_lot = {}
    total_pnl_pct = []

    for f in result["fills"]:
        if f.side == "buy" and f.lot_id is not None:
            entry_price_by_lot[f.lot_id] = f.price

        pnl_note = ""
        if f.side == "sell" and f.lot_id in entry_price_by_lot:
            entry = entry_price_by_lot[f.lot_id]
            pnl_pct = (f.price / entry - 1.0) * 100
            total_pnl_pct.append(pnl_pct)
            pnl_note = f"  ({pnl_pct:+.2f}% vs entry)"

        print(f"{_fmt(f.ts):<17} {f.side:<5} {f.price:>14,.0f}  {f.reason}{pnl_note}")

    print("-" * 80)
    n_trades = len(total_pnl_pct)
    if n_trades:
        wins = sum(1 for p in total_pnl_pct if p > 0)
        losses = n_trades - wins
        avg_win = sum(p for p in total_pnl_pct if p > 0) / wins if wins else 0
        avg_loss = sum(p for p in total_pnl_pct if p <= 0) / losses if losses else 0
        stop_losses = sum(1 for f in result["fills"] if "stop loss" in f.reason)
        trend_breaks = sum(1 for f in result["fills"] if "trend break" in f.reason)
        trailing_stops = sum(1 for f in result["fills"] if "trailing stop" in f.reason)
        period_closes = sum(1 for f in result["fills"] if "period close" in f.reason)
        print(f"Completed round trips: {n_trades}")
        print(f"  Wins: {wins}  (avg {avg_win:+.2f}%)")
        print(f"  Losses: {losses}  (avg {avg_loss:+.2f}%)")
        print(f"  Exits via stop-loss: {stop_losses}")
        print(f"  Exits via trend break: {trend_breaks}")
        print(f"  Exits via trailing stop: {trailing_stops}")
        print(f"  Exits via period close (profit protection): {period_closes}")


if __name__ == "__main__":
    main()
