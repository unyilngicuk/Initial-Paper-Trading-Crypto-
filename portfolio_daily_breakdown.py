"""
Daily performance for the 4-asset equal-weight portfolio (BTC/ETH via
Unyil 2.0, TSLAX/AAPLX via Gap-Fade), over the last N days of the real
overlapping window, with a bi-weekly (14-day) rollup at the end.

Each asset is bucketed to its own last-value-per-calendar-day equity
(handles BTC/ETH's 15-min bars and TSLAX/AAPLX's 60-min bars correctly
without forcing them onto identical bar indices), then summed across
all four assets for that day to get the portfolio's daily total.

Run: python3 portfolio_daily_breakdown.py \\
       --btc data/btcidr_15m_2y.csv --eth data/ethidr_15m_2y.csv \\
       --tslax data/tslaxidr_60m.csv --aaplx data/aaplxidr_60m.csv \\
       --capital 1000000 --report-days 60 --rollup-days 14
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles
from config import Config
from engine import apply_fill, check_drawdown, verify_balance


def run_one_asset_daily(candles, decide_fn, new_state_fn, cfg, report_start_ts):
    state = new_state_fn(cfg)
    fills = []
    daily_eq = {}
    daily_px = {}

    for candle in candles:
        state["closes"].append(candle["close"])
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1

        check_drawdown(state, candle["close"], cfg)
        for action in decide_fn(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed: {err}"

        if candle["ts"] >= report_start_ts:
            day = datetime.fromtimestamp(candle["ts"], tz=timezone.utc).date()
            eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
            daily_eq[day] = eq
            daily_px[day] = candle["close"]

    return daily_eq, daily_px


def _value_on_or_before(series, day, fallback):
    if day in series:
        return series[day]
    past = [d for d in series if d <= day]
    return series[max(past)] if past else fallback


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--btc", type=str)
    p.add_argument("--eth", type=str)
    p.add_argument("--tslax", type=str)
    p.add_argument("--aaplx", type=str)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--report-days", type=int, default=60)
    p.add_argument("--rollup-days", type=int, default=14)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    args = p.parse_args()

    asset_paths = {"BTC": args.btc, "ETH": args.eth, "TSLAX": args.tslax, "AAPLX": args.aaplx}
    asset_paths = {k: v for k, v in asset_paths.items() if v}
    all_candles = {label: load_candles(path) for label, path in asset_paths.items()}

    overlap_start = max(d[0]["ts"] for d in all_candles.values())
    overlap_end = min(d[-1]["ts"] for d in all_candles.values())
    report_start_ts = overlap_end - args.report_days * 86400
    if report_start_ts < overlap_start:
        report_start_ts = overlap_start
        print(f"  (only {(overlap_end-overlap_start)/86400:.0f} days of real overlap exist -- "
              f"using the full window instead of {args.report_days})")

    print(f"Reporting window: {datetime.fromtimestamp(report_start_ts, tz=timezone.utc).date()} "
          f"-> {datetime.fromtimestamp(overlap_end, tz=timezone.utc).date()}")
    print(f"Rp {args.capital:,.0f} total, 25% per asset\n")

    import strategy_unyil2
    import strategy_gap
    from asset_type_detector import detect_session_start_hour

    n_assets = len(asset_paths)
    capital_per_asset = args.capital / n_assets
    per_asset_daily_eq = {}
    per_asset_daily_px = {}

    for label, candles in all_candles.items():
        native = label in ("BTC", "ETH")
        cfg = Config()
        cfg.use_venue(args.venue)
        cfg.coin = label.lower()
        cfg.starting_idr = capital_per_asset

        if native:
            decide_fn, new_state_fn = strategy_unyil2.decide, strategy_unyil2.new_state
            warmup_bars = cfg.trend_ma_period
        else:
            decide_fn, new_state_fn = strategy_gap.decide, strategy_gap.new_state
            cfg.gap_mode = "fade"
            detected = detect_session_start_hour(candles)
            if detected is not None:
                cfg.orb_session_start_hour = detected
            warmup_bars = 0

        window_start_idx = next(i for i, c in enumerate(candles) if c["ts"] >= report_start_ts)
        window_end_idx = next((i for i, c in enumerate(candles) if c["ts"] > overlap_end), len(candles))
        slice_start_idx = max(0, window_start_idx - warmup_bars - 10)
        sliced = candles[slice_start_idx:window_end_idx]

        daily_eq, daily_px = run_one_asset_daily(sliced, decide_fn, new_state_fn, cfg, report_start_ts)
        per_asset_daily_eq[label] = daily_eq
        per_asset_daily_px[label] = daily_px

    all_days = sorted(set().union(*[d.keys() for d in per_asset_daily_eq.values()]))

    rows = []
    prev_portfolio_eq = args.capital
    prev_bh_eq = args.capital
    for day in all_days:
        portfolio_eq = sum(
            _value_on_or_before(per_asset_daily_eq[label], day, capital_per_asset)
            for label in asset_paths
        )
        bh_eq = 0.0
        for label in asset_paths:
            px_series = per_asset_daily_px[label]
            if not px_series:
                bh_eq += capital_per_asset
                continue
            first_day = min(px_series.keys())
            cur_price = _value_on_or_before(px_series, day, px_series[first_day])
            bh_eq += capital_per_asset * (cur_price / px_series[first_day])

        daily_ret = (portfolio_eq / prev_portfolio_eq - 1) * 100
        daily_bh_ret = (bh_eq / prev_bh_eq - 1) * 100
        rows.append({"date": day, "eq": portfolio_eq, "ret": daily_ret,
                     "bh_eq": bh_eq, "bh_ret": daily_bh_ret})
        prev_portfolio_eq = portfolio_eq
        prev_bh_eq = bh_eq

    print(f"{'date':<12} {'portfolio':>14} {'day ret':>9} {'buy&hold':>14} {'day ret':>9}")
    print("-" * 62)
    for r in rows:
        print(f"{str(r['date']):<12} {r['eq']:>14,.0f} {r['ret']:>8.2f}% "
              f"{r['bh_eq']:>14,.0f} {r['bh_ret']:>8.2f}%")

    total_ret = (rows[-1]["eq"] / args.capital - 1) * 100
    total_bh_ret = (rows[-1]["bh_eq"] / args.capital - 1) * 100
    print("-" * 62)
    print(f"Total over {len(rows)} days: portfolio {total_ret:+.2f}%  |  "
          f"buy&hold {total_bh_ret:+.2f}%  |  edge {total_ret-total_bh_ret:+.2f} pts")

    print(f"\n=== {args.rollup_days}-DAY ROLLUP ===")
    print(f"{'period':<25} {'portfolio ret':>14} {'buy&hold ret':>14}")
    print("-" * 56)
    for i in range(0, len(rows), args.rollup_days):
        chunk = rows[i:i + args.rollup_days]
        start_eq = args.capital if i == 0 else rows[i - 1]["eq"]
        start_bh = args.capital if i == 0 else rows[i - 1]["bh_eq"]
        chunk_ret = (chunk[-1]["eq"] / start_eq - 1) * 100
        chunk_bh = (chunk[-1]["bh_eq"] / start_bh - 1) * 100
        label = f"{chunk[0]['date']} to {chunk[-1]['date']}"
        print(f"{label:<25} {chunk_ret:>13.2f}% {chunk_bh:>13.2f}%")


if __name__ == "__main__":
    main()
