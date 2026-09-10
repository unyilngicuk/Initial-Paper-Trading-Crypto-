"""
Daily performance breakdown, with a weekly rollup at the end.

Same methodology as weekly_breakdown.py: one continuous run (so the trend
filter stays properly warmed up), reported day by day over a short recent
window, then grouped into weeks so each week's daily story is visible
before it collapses into a single weekly number.

Run: python3 daily_breakdown.py --candles data/ethidr_15m_2y.csv --report-days 28 --coin eth
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles, run
from config import Config


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--report-days", type=int, default=28)
    p.add_argument("--warmup-days", type=int, default=35)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--coin", type=str, default="btc")
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital
    cfg.coin = args.coin.lower()

    all_candles = load_candles(args.candles)
    if not all_candles:
        raise SystemExit("no candles in input file")

    max_ts = max(c["ts"] for c in all_candles)
    report_start_ts = max_ts - args.report_days * 86400
    data_start_ts = report_start_ts - args.warmup_days * 86400
    candles = [c for c in all_candles if c["ts"] >= data_start_ts]
    if not candles:
        raise SystemExit("no candles in window -- check --report-days/--warmup-days")

    result = run(candles, cfg)
    curve = [(ts, eq, px) for ts, eq, px in result["equity_curve"] if ts >= report_start_ts]
    fills = result["fills"]
    if not curve:
        raise SystemExit("no data in reporting window after warmup")

    print(f"Continuous run: {_fmt_date(candles[0]['ts'])} -> {_fmt_date(candles[-1]['ts'])}")
    print(f"  ({args.warmup_days} days warmup, then {args.report_days} days reported)")
    print(f"Asset: {cfg.coin.upper()}/{cfg.quote}")
    print(f"Starting capital: Rp {args.capital:,.0f}\n")

    day_seconds = 86400
    rows = []
    d_start_ts, d_start_eq, d_start_px = curve[0]
    day_num = 1
    for i, (ts, eq, px) in enumerate(curve):
        is_last = (i == len(curve) - 1)
        if ts >= d_start_ts + day_seconds or is_last:
            trades = sum(1 for f in fills if d_start_ts <= f.ts <= ts)
            rows.append({
                "day": day_num, "date": _fmt_date(ts),
                "start_eq": d_start_eq, "end_eq": eq,
                "ret": (eq / d_start_eq - 1) * 100,
                "bh": (px / d_start_px - 1) * 100,
                "trades": trades,
            })
            day_num += 1
            d_start_ts, d_start_eq, d_start_px = ts, eq, px

    print(f"{'day':>3} {'date':<12} {'return':>9} {'buy&hold':>10} {'trades':>7} {'end equity':>15}")
    print("-" * 65)
    for r in rows:
        print(f"{r['day']:>3} {r['date']:<12} {r['ret']:>8.2f}% "
              f"{r['bh']:>9.2f}% {r['trades']:>7} {r['end_eq']:>15,.0f}")

    total_ret = (curve[-1][1] / curve[0][1] - 1) * 100
    total_bh = (curve[-1][2] / curve[0][2] - 1) * 100
    winning_days = sum(1 for r in rows if r["ret"] > 0)
    print("-" * 65)
    print(f"Total over {len(rows)} days: strategy {total_ret:+.2f}%  |  "
          f"buy&hold {total_bh:+.2f}%  |  edge {total_ret - total_bh:+.2f} pts")
    print(f"Winning days: {winning_days}/{len(rows)}")
    print(f"Final equity: Rp {curve[-1][1]:,.0f}")

    print(f"\n=== WEEKLY ROLLUP (built from the {len(rows)} days above) ===")
    print(f"{'wk':>3} {'days':<25} {'return':>9} {'buy&hold':>10} {'trades':>7} {'end equity':>15}")
    print("-" * 78)
    week_num = 1
    for i in range(0, len(rows), 7):
        chunk = rows[i:i + 7]
        wk_start_eq = chunk[0]["start_eq"]
        wk_end_eq = chunk[-1]["end_eq"]
        wk_ret = (wk_end_eq / wk_start_eq - 1) * 100
        bh_factor = 1.0
        for r in chunk:
            bh_factor *= (1 + r["bh"] / 100)
        wk_bh = (bh_factor - 1) * 100
        wk_trades = sum(r["trades"] for r in chunk)
        date_range = f"{chunk[0]['date']} to {chunk[-1]['date']}"
        print(f"{week_num:>3} {date_range:<25} {wk_ret:>8.2f}% {wk_bh:>9.2f}% "
              f"{wk_trades:>7} {wk_end_eq:>15,.0f}")
        week_num += 1


if __name__ == "__main__":
    main()
