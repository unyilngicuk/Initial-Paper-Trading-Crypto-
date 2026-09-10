"""
Monthly performance breakdown, plus a monthly profit-withdrawal scenario.

Runs ONE continuous backtest (so the ~28-day trend filter stays properly
warmed up) and reports performance month by month. Then models what
"withdraw all profit above principal at the end of every month" would
have produced, versus letting it compound continuously.

The withdrawal model is deliberately simple and conservative: at each
month boundary, any equity above the starting principal is treated as
withdrawn to a stash; if a month ends below principal, the shortfall is
topped up from the stash (and the stash is allowed to go negative, which
is reported honestly as "you would have needed to add money").

Run: python3 monthly_breakdown.py --candles data/btcidr_15m_2y.csv --report-days 183
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
    p.add_argument("--report-days", type=int, default=183)
    p.add_argument("--warmup-days", type=int, default=35)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--trend-days", type=float)
    p.add_argument("--buffer", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--base-risk", type=float)
    p.add_argument("--min-risk", type=float)
    p.add_argument("--losses-before-shrink", type=int)
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
        raise SystemExit("no candles in window -- check --report-days/--warmup-days")

    result = run(candles, cfg)
    curve = [(ts, eq, px) for ts, eq, px in result["equity_curve"] if ts >= report_start_ts]
    fills = result["fills"]
    if not curve:
        raise SystemExit("no data in reporting window after warmup")

    print(f"Continuous run: {_fmt_date(candles[0]['ts'])} -> {_fmt_date(candles[-1]['ts'])}")
    print(f"  ({args.warmup_days} days warmup, then {args.report_days} days reported)")
    print(f"Config: trend {cfg.trend_ma_period}-bar, buffer {cfg.trend_buffer_pct:.1%}, "
          f"base risk {cfg.base_risk_frac:.0%}, floor {cfg.min_risk_frac:.0%}")
    print(f"Starting capital: Rp {args.capital:,.0f}\n")

    month_seconds = 30 * 86400
    rows = []
    m_start_ts, m_start_eq, m_start_px = curve[0]
    month_num = 1
    for i, (ts, eq, px) in enumerate(curve):
        is_last = (i == len(curve) - 1)
        if ts >= m_start_ts + month_seconds or is_last:
            trades = sum(1 for f in fills if m_start_ts <= f.ts <= ts)
            rows.append({
                "month": month_num,
                "start": _fmt_date(m_start_ts),
                "end": _fmt_date(ts),
                "start_eq": m_start_eq,
                "end_eq": eq,
                "ret": (eq / m_start_eq - 1) * 100,
                "bh": (px / m_start_px - 1) * 100,
                "trades": trades,
            })
            month_num += 1
            m_start_ts, m_start_eq, m_start_px = ts, eq, px

    print("=== CONTINUOUS (compounding, no withdrawals) ===")
    print(f"{'mo':>3} {'start':<11} {'end':<11} {'return':>9} {'buy&hold':>10} {'trades':>7} {'end equity':>15}")
    print("-" * 76)
    for r in rows:
        print(f"{r['month']:>3} {r['start']:<11} {r['end']:<11} {r['ret']:>8.2f}% "
              f"{r['bh']:>9.2f}% {r['trades']:>7} {r['end_eq']:>15,.0f}")

    total_ret = (curve[-1][1] / curve[0][1] - 1) * 100
    total_bh = (curve[-1][2] / curve[0][2] - 1) * 100
    print("-" * 76)
    print(f"Total: strategy {total_ret:+.2f}%  |  buy&hold {total_bh:+.2f}%  |  "
          f"edge {total_ret - total_bh:+.2f} pts")
    print(f"Winning months: {sum(1 for r in rows if r['ret'] > 0)}/{len(rows)}")
    print(f"Final equity (compounded): Rp {curve[-1][1]:,.0f}")

    # --- monthly withdrawal scenario ---
    # Each month restarts from the same principal; profit is banked, losses
    # are topped up from the bank. Uses each month's measured return.
    print(f"\n=== MONTHLY WITHDRAWAL (reset to Rp {args.capital:,.0f} each month) ===")
    print(f"{'mo':>3} {'return':>9} {'profit/loss':>16} {'bank after':>16}")
    print("-" * 50)
    bank = 0.0
    for r in rows:
        pnl = args.capital * (r["ret"] / 100)
        bank += pnl
        print(f"{r['month']:>3} {r['ret']:>8.2f}% {pnl:>15,.0f} {bank:>15,.0f}")
    print("-" * 50)
    total_wealth = args.capital + bank
    print(f"Principal kept intact:  Rp {args.capital:>15,.0f}")
    print(f"Banked from withdrawals: Rp {bank:>15,.0f}")
    print(f"TOTAL WEALTH:            Rp {total_wealth:>15,.0f}")
    if bank < 0:
        print(f"  NOTE: bank went negative -- you would have needed to add "
              f"Rp {-bank:,.0f} of outside money to keep topping the principal back up.")

    print(f"\n=== COMPARISON ===")
    print(f"  Continuous compounding: Rp {curve[-1][1]:,.0f}")
    print(f"  Monthly withdrawal:     Rp {total_wealth:,.0f}")
    diff = total_wealth - curve[-1][1]
    better = "withdrawal" if diff > 0 else "compounding"
    print(f"  Difference: Rp {abs(diff):,.0f} in favour of {better}")


if __name__ == "__main__":
    main()
