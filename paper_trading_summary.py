"""
Cross-asset portfolio summary -- reads all four state files (written by
the two separate trading workflows, on their own schedules) and sends
ONE combined summary to Telegram. Deliberately a separate, independent
script/workflow rather than bolted onto either trading workflow, since
this needs data from both and shouldn't be coupled to either schedule.

Format is one block of labeled lines per asset, not a fixed-width table
-- a real table's column alignment breaks on Telegram's mobile view
(different monospace rendering width than a desktop terminal), while
labeled lines wrap naturally and stay readable at any screen width.

Regime context is shown for native crypto assets only, reusing the same
guarded regime_manager logic live_runner.py already uses -- informational,
never decision-driving (see spec Section 11d on why automated
regime-switching was tried and rejected). Wrapper assets correctly show
no regime, since regime_manager's own guard refuses to classify that
kind of data (see spec Section 11e/11f).

Run: python3 paper_trading_summary.py
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from indodax_data import fetch_paged

ASSETS = [
    {"label": "BTC", "state_file": "state_btc.json", "symbol": "BTCIDR", "native": True},
    {"label": "ETH", "state_file": "state_eth.json", "symbol": "ETHIDR", "native": True},
    {"label": "TSLAX", "state_file": "state_tslax.json", "symbol": "TSLAXIDR", "native": False},
    {"label": "GOOGLX", "state_file": "state_googlx.json", "symbol": "GOOGLXIDR", "native": False},
]


def _fetch_latest_price(symbol: str):
    try:
        candles = fetch_paged(symbol, "60", days=1)
        if not candles:
            return None
        return candles[-1]["close"]
    except Exception as e:
        print(f"[WARN] could not fetch latest price for {symbol}: {e}", flush=True)
        return None


def _fetch_regime(symbol: str):
    """Native crypto only. Fetches enough real history for a genuine
    classification, not just enough for a single price point."""
    try:
        candles = fetch_paged(symbol, "15", days=35)
        if not candles:
            return None
        from regime_manager import apply_persistence, classify_all
        raw = classify_all(candles)
        confirmed = apply_persistence(raw)
        return confirmed[-1]["confirmed"]
    except Exception as e:
        print(f"[WARN] could not fetch regime for {symbol}: {e}", flush=True)
        return None


def _load_state(path: str):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _send_telegram(message: str) -> None:
    print(message, flush=True)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot_token and chat_id):
        print("[WARN] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- printed only", flush=True)
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        if not body.get("ok"):
            print(f"[TELEGRAM FAILED] {body}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM FAILED] {e}", flush=True)


def build_summary():
    rows = []
    missing = []
    total_initial = 0.0
    total_current = 0.0
    total_reserve = 0.0

    for asset in ASSETS:
        state = _load_state(asset["state_file"])
        if state is None:
            missing.append(asset["label"])
            continue

        principal = state.get("principal", 0.0)
        reserve = state.get("profit_reserve", 0.0)
        cash = state.get("idr", 0.0)
        lots = state.get("lots", [])
        coin_qty = sum(l.get("qty_coin", 0.0) for l in lots)

        price = _fetch_latest_price(asset["symbol"]) if coin_qty > 0 else 0.0
        if coin_qty > 0 and price is None:
            current_equity = cash
            price_note = " (price unavailable, position value omitted)"
        else:
            current_equity = cash + coin_qty * (price or 0.0)
            price_note = ""

        pct = ((current_equity - principal) / principal * 100) if principal > 0 else 0.0

        regime = _fetch_regime(asset["symbol"]) if asset["native"] else None

        rows.append({
            "label": asset["label"],
            "note": price_note,
            "initial": principal,
            "current": current_equity,
            "pct": pct,
            "reserve": reserve,
            "regime": regime,
            "has_position": coin_qty > 0,
        })
        total_initial += principal
        total_current += current_equity
        total_reserve += reserve

    return rows, missing, total_initial, total_current, total_reserve


def format_message(rows, missing, total_initial, total_current, total_reserve) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Portfolio summary -- {now}", ""]

    for r in rows:
        lines.append(f"{r['label']}{r['note']}")
        lines.append(f"  Initial: Rp {r['initial']:,.0f}")
        lines.append(f"  Current: Rp {r['current']:,.0f}  ({r['pct']:+.2f}%)")
        lines.append(f"  Reserve: Rp {r['reserve']:,.0f}")
        if r["regime"] is not None:
            lines.append(f"  Regime:  {r['regime']}")
        lines.append(f"  Position: {'open' if r['has_position'] else 'in cash'}")
        lines.append("")

    if len(rows) > 1:
        total_pct = ((total_current - total_initial) / total_initial * 100) if total_initial > 0 else 0.0
        lines.append("TOTAL")
        lines.append(f"  Initial: Rp {total_initial:,.0f}")
        lines.append(f"  Current: Rp {total_current:,.0f}  ({total_pct:+.2f}%)")
        lines.append(f"  Reserve: Rp {total_reserve:,.0f}")
        lines.append("")

    if missing:
        lines.append(f"No data yet for: {', '.join(missing)} -- first tick hasn't run.")

    return "\n".join(lines).rstrip()


def main():
    rows, missing, total_initial, total_current, total_reserve = build_summary()
    if not rows:
        _send_telegram("Portfolio summary: no paper-trading data exists yet for any asset.")
        return
    message = format_message(rows, missing, total_initial, total_current, total_reserve)
    _send_telegram(message)


if __name__ == "__main__":
    main()
