"""
Runs ONE continuous backtest (so the strategy stays properly warmed up)
and reports performance broken down week by week, rather than restarting
the strategy every week -- which would leave it permanently starved of
the ~28 days of history its trend filter needs, and trivially show 0%
every single week.

Uses at least `warmup_days` of history BEFORE the reporting window starts,
purely so the strategy has real context when the reporting window begins --
those warmup days are not included in the weekly report itself.

Run: python3 weekly_breakdown.py --candles data/btcidr_15m_2y.csv --report-days 90
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
    p.add_argument("--report-days", type=int, default=90)
    p.add_argument("--warmup-days", type=int, default=35,
                   help="extra history before the report window, for the trend filter to warm up on")
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--trend-days", type=float, help="trend SMA window in days")
    p.add_argument("--buffer", type=float, help="trend_buffer_pct")
    p.add_argument("--stop", type=float, help="stop_loss_pct")
    p.add_argument("--base-risk", type=float, help="base_risk_frac")
    p.add_argument("--min-risk", type=float, help="min_risk_frac")
    p.add_argument("--losses-before-shrink", type=int, help="consecutive losses before sizing shrinks")
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital
    if args.trend_days:
        cfg.trend_ma_period = round(args.trend_days * 96)
        cfg.history_window = max(cfg.history_window, cfg.trend_ma_period + 10)
        cfg.warmup_bars = max(cfg.warmup_bars, cfg.trend_ma_period)
    if args.buffer is not None:
        cfg.trend_buffer_pct = args.buffer
    if args.stop is not None:
        cfg.stop_loss_pct = args.stop
    if args.base_risk is not None:
        cfg.base_risk_frac = args.base_risk
    if args.min_risk is not None:
        cfg.min_risk_frac = args.min_risk
    if args.losses_before_shrink is not None:
        cfg.losses_before_shrink = args.losses_before_shrink

    all_candles = load_candles(args.candles)
    if not all_candles:
        raise SystemExit("no candles in input file")

    max_ts = max(c["ts"] for c in all_candles)
    report_start_ts = max_ts - args.report_days * 86400
    data_start_ts = report_start_ts - args.warmup_days * 86400

    candles = [c for c in all_candles if c["ts"] >= data_start_ts]
    if not candles:
        raise SystemExit("no candles in the requested window -- check --report-days/--warmup-days")

    result = run(candles, cfg)
    curve = result["equity_curve"]  # list of (ts, equity, price)
    fills = result["fills"]

    # Only report weeks within the actual reporting window, not the warmup buffer.
    report_curve = [(ts, eq, px) for ts, eq, px in curve if ts >= report_start_ts]
    if not report_curve:
        raise SystemExit("no data in the reporting window after warmup -- widen --report-days")

    print(f"Continuous run: {_fmt_date(candles[0]['ts'])} -> {_fmt_date(candles[-1]['ts'])}")
    print(f"  ({args.warmup_days} days warmup, then {args.report_days} days reported below)")
    print(f"Starting capital: Rp {args.capital:,.0f}\n")

    week_seconds = 7 * 86400
    week_start_ts = report_curve[0][0]
    week_start_eq = report_curve[0][1]
    week_start_px = report_curve[0][2]

    rows = []
    week_num = 1
    i = 0
    n = len(report_curve)
    while i < n:
        ts, eq, px = report_curve[i]
        week_end_boundary = week_start_ts + week_seconds
        is_last = (i == n - 1)
        if ts >= week_end_boundary or is_last:
            week_return = (eq / week_start_eq - 1) * 100
            bh_return = (px / week_start_px - 1) * 100
            trades_in_week = sum(1 for f in fills if week_start_ts <= f.ts < ts + 1)
            rows.append({
                "week": week_num,
                "start": _fmt_date(week_start_ts),
                "end": _fmt_date(ts),
                "start_eq": week_start_eq,
                "end_eq": eq,
                "return_pct": week_return,
                "bh_pct": bh_return,
                "trades": trades_in_week,
            })
            week_num += 1
            week_start_ts = ts
            week_start_eq = eq
            week_start_px = px
        i += 1

    print(f"{'wk':>3} {'start':<11} {'end':<11} {'return':>9} {'buy&hold':>10} {'trades':>7} {'end equity':>15}")
    print("-" * 75)
    for r in rows:
        print(f"{r['week']:>3} {r['start']:<11} {r['end']:<11} {r['return_pct']:>8.2f}% "
              f"{r['bh_pct']:>9.2f}% {r['trades']:>7} {r['end_eq']:>15,.0f}")

    total_return = (report_curve[-1][1] / report_curve[0][1] - 1) * 100
    total_bh = (report_curve[-1][2] / report_curve[0][2] - 1) * 100
    winning_weeks = sum(1 for r in rows if r["return_pct"] > 0)
    print("-" * 75)
    print(f"Total over {len(rows)} weeks: strategy {total_return:+.2f}%  |  "
          f"buy&hold {total_bh:+.2f}%  |  edge {total_return - total_bh:+.2f} pts")
    print(f"Winning weeks: {winning_weeks}/{len(rows)}")


if __name__ == "__main__":
    main()
