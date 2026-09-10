"""
Asset type detector -- flags whether an asset's own price history looks like
NATIVE, continuously-traded crypto, or a TOKENIZED WRAPPER around something
that only has real price discovery part of the day (like NVDAX/TSLAX/AAPLX
tracking NASDAQ hours).

WHY THIS EXISTS: real backtesting (not theory) showed Unyil 2.0, Guardian,
and Usro all fail structurally on three different tokenized stocks (NVDAX,
TSLAX, AAPLX) regardless of whether the underlying company was rallying,
choppy, or calm -- while all three strategies work reasonably on BTC and
ETH. The mechanism identified: a tokenized wrapper trades 24/7 but its
REAL price only updates during the underlying market's actual hours
(~6.5h/day for NASDAQ). That leaves long recurring stretches with little
or no genuine price movement, which breaks the trend/volatility
assumptions every strategy here depends on.

This scans for that specific signature directly in the data, using no
asset-specific knowledge (no hardcoded "NASDAQ hours") -- just: does
volatility cluster strongly by hour-of-day, recurring day after day.

Run: python3 asset_type_detector.py --candles data/nvdaxidr_15m.csv
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone

from backtest import load_candles


def classify_asset(candles, ratio_threshold=5.0, flat_threshold=0.30):
    """
    Reusable classification, so other tools (like backtest.py) can check an
    asset's type without shelling out to the CLI. Returns a dict with the
    classification label and the underlying stats, so a caller can decide
    what to do with it (warn, block, just log) rather than this function
    making that decision itself.
    """
    if len(candles) < 96 * 7:
        return {"label": "UNKNOWN", "reason": "less than a week of data"}

    hourly_moves = defaultdict(list)
    hourly_flat_count = defaultdict(int)
    hourly_total_count = defaultdict(int)

    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1]["close"]
        hour = datetime.fromtimestamp(c["ts"], tz=timezone.utc).hour

        move_pct = abs(c["close"] - prev_close) / prev_close * 100 if prev_close else 0.0
        hourly_moves[hour].append(move_pct)

        is_flat = (c["open"] == c["high"] == c["low"] == c["close"])
        hourly_flat_count[hour] += 1 if is_flat else 0
        hourly_total_count[hour] += 1

    hourly_avg_move = {h: sum(v) / len(v) for h, v in hourly_moves.items() if v}
    hourly_flat_frac = {h: hourly_flat_count[h] / hourly_total_count[h]
                         for h in hourly_total_count if hourly_total_count[h] > 0}

    nonzero_moves = [v for v in hourly_avg_move.values() if v > 0]
    max_move = max(hourly_avg_move.values()) if hourly_avg_move else 0
    min_move = min(nonzero_moves) if nonzero_moves else 0
    ratio = (max_move / min_move) if min_move > 0 else float("inf")

    quiet_hours = sorted(h for h, frac in hourly_flat_frac.items() if frac >= flat_threshold)
    is_wrapper = ratio >= ratio_threshold or len(quiet_hours) >= 6

    return {
        "label": "WRAPPER" if is_wrapper else "NATIVE",
        "ratio": ratio,
        "quiet_hours": quiet_hours,
        "hourly_avg_move": hourly_avg_move,
        "hourly_flat_frac": hourly_flat_frac,
    }


def detect_session_start_hour(candles, flat_threshold=0.30, smoothing_window=3):
    """
    Finds where real trading most plausibly begins each day. Uses a
    SMOOTHED, multi-hour window rather than a single hour-to-hour drop --
    real (noisy) data can show a bigger single-point jump from ordinary
    overnight noise than from the actual session transition (confirmed
    empirically on real NVDAX 60-minute data, where the biggest single
    drop was hour 23's, purely from overnight fluctuation, while the
    genuine multi-hour active dip sat at hours 12-14).

    Approach: for each hour, average flat_frac over the next
    `smoothing_window` hours (its "forward window"). The session start is
    the hour whose forward window has the lowest average flat_frac,
    provided that's meaningfully lower than the 24-hour average -- i.e.
    look for the most sustained active stretch, not the single sharpest
    (and noisiest) transition.
    """
    result = classify_asset(candles, flat_threshold=flat_threshold)
    if result["label"] != "WRAPPER":
        return None
    flat_frac = result["hourly_flat_frac"]
    if not flat_frac:
        return None

    overall_avg = sum(flat_frac.get(h, 0.0) for h in range(24)) / 24
    best_hour, best_window_avg = None, overall_avg

    for h in range(24):
        window_hours = [(h + i) % 24 for i in range(smoothing_window)]
        window_avg = sum(flat_frac.get(wh, 0.0) for wh in window_hours) / smoothing_window
        if window_avg < best_window_avg:
            best_window_avg = window_avg
            best_hour = h

    return best_hour


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--ratio-threshold", type=float, default=5.0,
                   help="max/min hourly volatility ratio above which we flag a wrapper pattern")
    p.add_argument("--flat-threshold", type=float, default=0.30,
                   help="fraction of exactly-flat bars in an hour above which that hour is 'quiet'")
    args = p.parse_args()

    candles = load_candles(args.candles)
    if len(candles) < 96 * 7:
        raise SystemExit("need at least a week of data for a reliable read")

    result = classify_asset(candles, args.ratio_threshold, args.flat_threshold)
    hourly_avg_move = result["hourly_avg_move"]
    hourly_flat_frac = result["hourly_flat_frac"]
    quiet_hours = result["quiet_hours"]
    ratio = result["ratio"]

    print(f"Analyzed {len(candles):,} bars ({(candles[-1]['ts']-candles[0]['ts'])/86400:.0f} days)")
    print()
    print(f"{'hour (UTC)':>10} {'avg move %':>12} {'flat bar %':>12}")
    print("-" * 38)
    for h in range(24):
        move = hourly_avg_move.get(h, 0.0)
        flat = hourly_flat_frac.get(h, 0.0) * 100
        marker = " <- quiet" if h in quiet_hours else ""
        print(f"{h:>10} {move:>11.4f}% {flat:>11.1f}%{marker}")

    print()
    print(f"Max/min hourly volatility ratio: {ratio:.1f}x")
    print(f"Quiet hours detected (>={args.flat_threshold:.0%} flat bars): "
          f"{len(quiet_hours)} of 24  {quiet_hours if quiet_hours else ''}")
    print()
    print("=" * 60)
    if result["label"] == "WRAPPER":
        print("  CLASSIFICATION: looks like a TOKENIZED WRAPPER")
        print(f"  (part-time real price discovery, {len(quiet_hours)} quiet hours/day)")
        print("  Recommendation per current evidence: consider Usro over")
        print("  Unyil 2.0 -- all three NATIVE_CRYPTO-family strategies have")
        print("  shown structural weakness on this pattern, Usro least severely.")
        print("  No dedicated WRAPPER-family strategy exists yet.")
    else:
        print("  CLASSIFICATION: looks like NATIVE continuous trading")
        print("  Recommendation per current evidence: Unyil 2.0 as the default.")
    print("=" * 60)
    print("\n(Heuristic, calibrated against 5 known real assets -- BTC, ETH,")
    print(" NVDAX, TSLAX, AAPLX. Not a substitute for actually backtesting")
    print(" a new asset once this flags it.)")


if __name__ == "__main__":
    main()
