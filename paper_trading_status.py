"""
Human-readable status report for a paper-trading session -- reads the
same state.json live_runner.py maintains, no separate tracking needed.

Run: python3 paper_trading_status.py
     python3 paper_trading_status.py --state-path /path/to/state.json
"""

import argparse
import json
import os
from datetime import datetime, timezone


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state-path", default=os.environ.get("STATE_PATH", "state.json"))
    args = p.parse_args()

    if not os.path.exists(args.state_path):
        print(f"No paper-trading session found at {args.state_path} yet.")
        print("This file is created the first time live_runner.py runs.")
        return

    with open(args.state_path) as f:
        state = json.load(f)

    principal = state.get("principal", 0.0)
    reserve = state.get("profit_reserve", 0.0)
    cash = state.get("idr", 0.0)
    lots = state.get("lots", [])

    print("=" * 60)
    print("  PAPER TRADING STATUS")
    print("=" * 60)
    print(f"  Original principal:     Rp {principal:>15,.0f}")
    print(f"  Cash on hand:           Rp {cash:>15,.0f}")
    print(f"  Profit reserve:         Rp {reserve:>15,.0f}  (protected, never re-risked)")

    if lots:
        print(f"\n  Open position(s): {len(lots)}")
        for lot in lots:
            entry_ts = lot.get("entry_ts", 0)
            entry_dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"    lot #{lot.get('lot_id')}: {lot.get('qty_coin', 0):.6f} coin, "
                  f"entered {entry_dt} @ Rp {lot.get('entry_price', 0):,.0f}")
        print("\n  (Position value not shown here -- it depends on the CURRENT price, "
              "which this tool doesn't fetch. Run live_runner.py to get an up-to-date "
              "notification, or check the exchange directly for the live price.)")
    else:
        print("\n  No open position right now -- sitting in cash.")

    print(f"\n  Halted: {'YES -- see below' if state.get('halted') else 'No'}")
    if state.get("halted"):
        print("  A circuit breaker or balance mismatch paused new trades.")
        print("  Existing positions (if any) are left alone, not force-sold.")
        print("  Check the notification log for why, then decide what to do next.")

    print(f"\n  Bars seen so far: {state.get('bars_seen', 0):,}")
    print(f"  Strategy: {state.get('strategy_name', 'unknown')}")
    print("=" * 60)
    print("\nThis reflects PAPER trades only -- nothing here has touched a real")
    print("exchange account. Run live_runner.py (with EXECUTE=false, the default)")
    print("to advance the simulation with the next real market tick.")


if __name__ == "__main__":
    main()
