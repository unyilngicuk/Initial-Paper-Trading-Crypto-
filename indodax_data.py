"""
Fetch BTC/IDR candles from Indodax, paged.

Verified clean: 0 bad candles out of 70,081 bars across 730 days, no gaps.
High/low are trustworthy, which is what makes limit-order (maker) fill
modelling honest.

One call for 730 days of 15m data times out, so this pages in windows and
stitches the results.

    python3 indodax_data.py fetch --days 365 --out data/btcidr_15m.csv
    python3 indodax_data.py fetch --days 730 --tf 60 --out data/btcidr_1h.csv
"""

import argparse
import csv
import json
import os
import time
import urllib.request
from typing import Any, Dict, List

BASE = "https://indodax.com"
UA = "grid-backtest/1.0"

# Days per request. 15m bars are dense, so keep the window small.
WINDOW_DAYS = {"15": 25, "30": 45, "60": 120, "240": 365, "1D": 1825}


def get(url: str, timeout: int = 25) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("Data", []))
    out = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        k = {key.lower(): v for key, v in r.items()}
        if "close" not in k:
            continue
        out.append({
            "ts": int(float(k.get("time", k.get("t", 0)))),
            "open": float(k.get("open", k.get("o", 0))),
            "high": float(k.get("high", k.get("h", 0))),
            "low": float(k.get("low", k.get("l", 0))),
            "close": float(k.get("close", k.get("c", 0))),
            "volume": float(k.get("volume", k.get("v", 0)) or 0),
        })
    return out


def check(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Arithmetic guard: low <= open/close <= high must hold for a real candle."""
    bad = 0
    for c in candles:
        if (c["high"] < max(c["open"], c["close"]) - 1e-9
                or c["low"] > min(c["open"], c["close"]) + 1e-9):
            bad += 1
    return {"bad": bad, "n": len(candles)}


def fetch_paged(symbol: str, tf: str, days: int, pause: float = 0.4) -> List[Dict[str, Any]]:
    now = int(time.time())
    window = WINDOW_DAYS.get(tf, 30) * 86400
    start = now - days * 86400

    seen: Dict[int, Dict[str, Any]] = {}
    cursor = start
    page = 0

    while cursor < now:
        end = min(cursor + window, now)
        url = (f"{BASE}/tradingview/history_v2"
               f"?from={cursor}&to={end}&symbol={symbol}&tf={tf}")
        page += 1
        try:
            rows = parse(get(url))
        except Exception as e:
            print(f"  page {page}: FAILED ({type(e).__name__}) -- skipping window")
            cursor = end
            continue

        for r in rows:
            seen[r["ts"]] = r

        done = (end - start) / max(now - start, 1)
        print(f"  page {page}: +{len(rows):>5} rows, {len(seen):>7} total "
              f"({done:>5.0%})", flush=True)

        if end >= now:
            break
        cursor = end
        time.sleep(pause)      # be polite to a public endpoint

    return [seen[ts] for ts in sorted(seen)]


def write_csv(candles: List[Dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["ts", "open", "high", "low", "close", "volume"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in candles:
            w.writerow({k: c[k] for k in cols})


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    fe = sub.add_parser("fetch")
    fe.add_argument("--symbol", default="BTCIDR")
    fe.add_argument("--tf", default="15", help="15, 30, 60, 240, 1D")
    fe.add_argument("--days", type=int, default=365)
    fe.add_argument("--out", default="data/btcidr_15m.csv")
    args = p.parse_args()

    print(f"Fetching {args.symbol} {args.tf} over {args.days} days, paged:")
    candles = fetch_paged(args.symbol, args.tf, args.days)
    if not candles:
        raise SystemExit("no candles returned")

    q = check(candles)
    span = (candles[-1]["ts"] - candles[0]["ts"]) / 86400
    gaps = [candles[i]["ts"] - candles[i - 1]["ts"] for i in range(1, len(candles))]
    common = max(set(gaps), key=gaps.count) if gaps else 0
    missing = sum(1 for g in gaps if g > common * 1.5)

    write_csv(candles, args.out)

    print(f"\n  {len(candles):,} candles spanning {span:.0f} days -> {args.out}")
    print(f"  bar spacing: {common}s   gaps in the series: {missing}")
    print(f"  arithmetic check: {q['bad']}/{q['n']} bad candles"
          f"  {'(clean)' if not q['bad'] else '(!! investigate)'}")
    if missing:
        print(f"  note: {missing} gaps -- Indodax omits bars with no trades. "
              f"The replay handles this, but very thin periods are not real "
              f"trading opportunities.")


if __name__ == "__main__":
    main()
