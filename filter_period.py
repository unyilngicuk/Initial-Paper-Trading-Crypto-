"""
Filters an existing candles CSV down to a specific window, so a strategy
can be tested on a shorter, specific period without re-fetching data.

By default takes the most recent N days. Use --offset-days to look further
back -- e.g. --days 183 --offset-days 183 gives you the 6-month window
BEFORE the most recent 6 months, for testing sequential periods.

Run: python3 filter_period.py --candles data/btcidr_15m_2y.csv --days 183 --out data/btcidr_15m_6mo.csv
     python3 filter_period.py --candles data/btcidr_15m_2y.csv --days 183 --offset-days 183 --out data/btcidr_15m_6mo_prior.csv
"""

import argparse
import csv
from datetime import datetime, timezone


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--offset-days", type=int, default=0,
                   help="skip this many days from the most recent end before starting the window")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    with open(args.candles, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise SystemExit("no rows in input file")

    max_ts = max(int(float(r["ts"])) for r in rows)
    window_end = max_ts - args.offset_days * 86400
    window_start = window_end - args.days * 86400

    filtered = [r for r in rows if window_start <= int(float(r["ts"])) <= window_end]
    filtered.sort(key=lambda r: int(float(r["ts"])))

    if not filtered:
        raise SystemExit("no rows in the requested window -- check --days/--offset-days")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(filtered)

    start_dt = datetime.fromtimestamp(int(float(filtered[0]["ts"])), tz=timezone.utc)
    end_dt = datetime.fromtimestamp(int(float(filtered[-1]["ts"])), tz=timezone.utc)
    actual_days = (int(float(filtered[-1]["ts"])) - int(float(filtered[0]["ts"]))) / 86400

    print(f"Filtered {len(filtered):,} bars ({actual_days:.0f} days)")
    print(f"  {start_dt} -> {end_dt}")
    print(f"  written to {args.out}")


if __name__ == "__main__":
    main()
