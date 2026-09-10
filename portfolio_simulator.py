"""
Equal-weight, multi-asset portfolio simulator.

Runs each asset as its OWN independent account (no shared cash pool --
deliberately the simplest starting point: split capital evenly, buy
separately, hold separately), each managed by whichever strategy family
the evidence actually supports: NATIVE_CRYPTO (Unyil 2.0) for BTC/ETH,
WRAPPER (Gap-Fade) for TSLAX/AAPLX.

WHY THE WINDOW IS SHORTER THAN BTC/ETH'S FULL HISTORY: TSLAX and AAPLX
only exist since their April 2026 launch. Simulating "the same
portfolio, running at the same time" requires the REAL overlapping
calendar window across every included asset -- not each asset's own
full history, which would silently mix different eras.

Compared against two honest baselines over the SAME window:
  1. Equal-weight buy-and-hold across all included assets.
  2. 100% in whichever single asset performed best alone over this
     exact window (computed here, not assumed from an earlier,
     differently-dated test).

Run: python3 portfolio_simulator.py \\
       --btc data/btcidr_15m_2y.csv --eth data/ethidr_15m_2y.csv \\
       --tslax data/tslaxidr_60m.csv --aaplx data/aaplxidr_60m.csv \\
       --capital 1000000
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles
from config import Config
from engine import apply_fill, check_drawdown, verify_balance


def run_one_asset(candles, decide_fn, new_state_fn, cfg, warmup_before_bars=0):
    state = new_state_fn(cfg)
    fills = []
    equity_curve = []

    for i, candle in enumerate(candles):
        state["closes"].append(candle["close"])
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1

        check_drawdown(state, candle["close"], cfg)
        for action in decide_fn(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"

        if i >= warmup_before_bars:
            eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
            equity_curve.append((candle["ts"], eq, candle["close"]))

    return {"equity_curve": equity_curve, "fills": fills, "final_state": state}


def find_common_window(datasets):
    start_ts = max(d[0]["ts"] for d in datasets.values())
    end_ts = min(d[-1]["ts"] for d in datasets.values())
    if start_ts >= end_ts:
        raise SystemExit("no real overlapping window across the provided assets")
    return start_ts, end_ts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--btc", type=str)
    p.add_argument("--eth", type=str)
    p.add_argument("--tslax", type=str)
    p.add_argument("--aaplx", type=str)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    args = p.parse_args()

    asset_paths = {"BTC": args.btc, "ETH": args.eth, "TSLAX": args.tslax, "AAPLX": args.aaplx}
    asset_paths = {k: v for k, v in asset_paths.items() if v}
    if len(asset_paths) < 2:
        raise SystemExit("need at least 2 assets")

    all_candles = {label: load_candles(path) for label, path in asset_paths.items()}
    start_ts, end_ts = find_common_window(all_candles)
    print(f"Real overlapping window across all {len(asset_paths)} assets: "
          f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).date()}")
    print()

    import strategy_unyil2
    import strategy_gap
    from asset_type_detector import detect_session_start_hour

    n_assets = len(asset_paths)
    capital_per_asset = args.capital / n_assets

    per_asset_results = {}
    per_asset_standalone_full_capital = {}

    for label, candles in all_candles.items():
        native = label in ("BTC", "ETH")
        cfg = Config()
        cfg.use_venue(args.venue)
        cfg.coin = label.lower()

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

        window_start_idx = next(i for i, c in enumerate(candles) if c["ts"] >= start_ts)
        window_end_idx = next((i for i, c in enumerate(candles) if c["ts"] > end_ts), len(candles))
        slice_start_idx = max(0, window_start_idx - warmup_bars)
        sliced = candles[slice_start_idx:window_end_idx]
        actual_warmup = window_start_idx - slice_start_idx

        cfg.starting_idr = capital_per_asset
        per_asset_results[label] = run_one_asset(sliced, decide_fn, new_state_fn, cfg,
                                                    warmup_before_bars=actual_warmup)

        cfg_full = Config()
        cfg_full.use_venue(args.venue)
        cfg_full.coin = label.lower()
        cfg_full.starting_idr = args.capital
        if native:
            cfg_full.trend_ma_period = cfg.trend_ma_period
        else:
            cfg_full.gap_mode = "fade"
            cfg_full.orb_session_start_hour = cfg.orb_session_start_hour
        per_asset_standalone_full_capital[label] = run_one_asset(
            sliced, decide_fn, new_state_fn, cfg_full, warmup_before_bars=actual_warmup
        )

    print(f"{'Asset':<8} {'Strategy':<10} {'Weight':>8} {'Start':>14} {'End':>14} {'Return':>9}")
    print("-" * 68)
    portfolio_total_start = 0.0
    portfolio_total_end = 0.0
    for label, result in per_asset_results.items():
        curve = result["equity_curve"]
        strat_name = "Unyil 2.0" if label in ("BTC", "ETH") else "Gap-Fade"
        start_eq, end_eq = curve[0][1], curve[-1][1]
        portfolio_total_start += start_eq
        portfolio_total_end += end_eq
        print(f"{label:<8} {strat_name:<10} {1/n_assets:>7.0%} {start_eq:>14,.0f} "
              f"{end_eq:>14,.0f} {(end_eq/start_eq-1)*100:>+8.2f}%")

    print("-" * 68)
    portfolio_return = (portfolio_total_end / portfolio_total_start - 1) * 100
    print(f"{'PORTFOLIO':<8} {'(blended)':<10} {'100%':>8} {portfolio_total_start:>14,.0f} "
          f"{portfolio_total_end:>14,.0f} {portfolio_return:>+8.2f}%")

    bh_total_start = 0.0
    bh_total_end = 0.0
    for label, candles in all_candles.items():
        window_start_idx = next(i for i, c in enumerate(candles) if c["ts"] >= start_ts)
        window_end_idx = next((i for i, c in enumerate(candles) if c["ts"] > end_ts), len(candles))
        first_px = candles[window_start_idx]["close"]
        last_px = candles[window_end_idx - 1]["close"]
        bh_total_start += capital_per_asset
        bh_total_end += capital_per_asset * (last_px / first_px)
    bh_return = (bh_total_end / bh_total_start - 1) * 100

    best_label, best_return = None, -1e18
    for label, result in per_asset_standalone_full_capital.items():
        curve = result["equity_curve"]
        ret = (curve[-1][1] / curve[0][1] - 1) * 100
        if ret > best_return:
            best_return, best_label = ret, label

    print()
    print(f"Equal-weight buy & hold (all {n_assets} assets):  {bh_return:+.2f}%")
    print(f"100% in single best asset alone ({best_label}):    {best_return:+.2f}%")
    print(f"This equal-weight portfolio:                {portfolio_return:+.2f}%")
    print()
    print(f"Portfolio edge vs. equal-weight B&H: {portfolio_return - bh_return:+.2f} pts")
    print(f"Portfolio edge vs. concentrating in {best_label} alone: "
          f"{portfolio_return - best_return:+.2f} pts")


if __name__ == "__main__":
    main()
