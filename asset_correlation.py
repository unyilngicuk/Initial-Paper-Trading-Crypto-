"""
Correlation matrix between multiple assets' daily returns -- for genuine
diversification decisions. An asset that moves in lockstep with one
already in the portfolio adds less real protection than one that moves
more independently, regardless of how different the underlying
companies seem.

Run: python3 asset_correlation.py --candles data/tslaxidr_60m.csv data/aaplxidr_60m.csv data/googlxidr_60m.csv --labels TSLAX AAPLX GOOGLX
"""

import argparse
from datetime import datetime, timezone

from backtest import load_candles


def _daily_closes(candles):
    """Last close of each UTC calendar day -- a simple, robust daily series
    regardless of each asset's own quiet-hours pattern."""
    by_day = {}
    for c in candles:
        day = datetime.fromtimestamp(c["ts"], tz=timezone.utc).date()
        by_day[day] = c["close"]  # last one seen for that day wins
    return by_day


def _daily_returns(by_day):
    days = sorted(by_day.keys())
    rets = {}
    for i in range(1, len(days)):
        prev, cur = by_day[days[i - 1]], by_day[days[i]]
        rets[days[i]] = (cur / prev) - 1.0
    return rets


def _correlation(a, b):
    common_days = sorted(set(a.keys()) & set(b.keys()))
    if len(common_days) < 10:
        return None, len(common_days)
    xa = [a[d] for d in common_days]
    xb = [b[d] for d in common_days]
    n = len(xa)
    mean_a = sum(xa) / n
    mean_b = sum(xb) / n
    cov = sum((xa[i] - mean_a) * (xb[i] - mean_b) for i in range(n)) / n
    std_a = (sum((x - mean_a) ** 2 for x in xa) / n) ** 0.5
    std_b = (sum((x - mean_b) ** 2 for x in xb) / n) ** 0.5
    if std_a == 0 or std_b == 0:
        return None, n
    return cov / (std_a * std_b), n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", nargs="+", required=True)
    p.add_argument("--labels", nargs="+", required=True,
                   help="one label per --candles file, same order")
    args = p.parse_args()

    if len(args.candles) != len(args.labels):
        raise SystemExit("need exactly one label per candles file")

    returns_by_asset = {}
    for path, label in zip(args.candles, args.labels):
        candles = load_candles(path)
        returns_by_asset[label] = _daily_returns(_daily_closes(candles))
        print(f"{label}: {len(returns_by_asset[label])} daily return observations")

    print()
    print("Correlation of daily returns (1.0 = moves identically, "
          "0.0 = unrelated, negative = tends to move opposite):")
    print()
    labels = args.labels
    col_w = max(8, max(len(l) for l in labels) + 1)
    print(" " * col_w + "".join(f"{l:>{col_w}}" for l in labels))
    for l1 in labels:
        row = f"{l1:<{col_w}}"
        for l2 in labels:
            if l1 == l2:
                row += f"{'--':>{col_w}}"
                continue
            corr, n = _correlation(returns_by_asset[l1], returns_by_asset[l2])
            row += f"{corr:>{col_w}.2f}" if corr is not None else f"{'n/a':>{col_w}}"
        print(row)

    print()
    print("Lower correlation with an asset already in the portfolio means more")
    print("genuine diversification benefit from adding it -- not just a different")
    print("company name.")


if __name__ == "__main__":
    main()
