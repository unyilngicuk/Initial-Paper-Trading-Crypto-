"""
Cross-asset portfolio summary -- reads all four state files (written by
the two separate trading workflows, on their own schedules) and sends
ONE combined table to Telegram. Deliberately a separate, independent
script/workflow rather than bolted onto either trading workflow, since
this needs data from both and shouldn't be coupled to either schedule.

Run: python3 paper_trading_summary.py
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from indodax_data import fetch_paged

ASSETS = [
    {"label": "BTC", "state_file": "state_btc.json", "symbol": "BTCIDR"},
    {"label": "ETH", "state_file": "state_eth.json", "symbol": "ETHIDR"},
    {"label": "TSLAX", "state_file": "state_tslax.json", "symbol": "TSLAXIDR"},
    {"label": "GOOGLX", "state_file": "state_googlx.json", "symbol": "GOOGLXIDR"},
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
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": message, "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=data)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        if not body.get("ok"):
            print(f"[TELEGRAM FAILED] {body}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM FAILED] {e}", flush=True)


def build_summary_table():
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

        rows.append({
            "label": asset["label"] + price_note,
            "initial": principal,
            "current": current_equity,
            "pct": pct,
            "reserve": reserve,
        })
        total_initial += principal
        total_current += current_equity
        total_reserve += reserve

    return rows, missing, total_initial, total_current, total_reserve


def format_message(rows, missing, total_initial, total_current, total_reserve) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"*Portfolio summary* -- {now}", "```"]
    lines.append(f"{'COIN':<10}{'INITIAL':>12}{'CURRENT':>12}{'P&L':>9}{'RESERVE':>12}")
    lines.append("-" * 55)
    for r in rows:
        lines.append(
            f"{r['label']:<10}{r['initial']:>12,.0f}{r['current']:>12,.0f}"
            f"{r['pct']:>8.2f}%{r['reserve']:>12,.0f}"
        )
    if len(rows) > 1:
        total_pct = ((total_current - total_initial) / total_initial * 100) if total_initial > 0 else 0.0
        lines.append("-" * 55)
        lines.append(
            f"{'TOTAL':<10}{total_initial:>12,.0f}{total_current:>12,.0f}"
            f"{total_pct:>8.2f}%{total_reserve:>12,.0f}"
        )
    lines.append("```")
    if missing:
        lines.append(f"_No data yet for: {', '.join(missing)} -- first tick hasn't run._")
    return "\n".join(lines)


def main():
    rows, missing, total_initial, total_current, total_reserve = build_summary_table()
    if not rows:
        _send_telegram("Portfolio summary: no paper-trading data exists yet for any asset.")
        return
    message = format_message(rows, missing, total_initial, total_current, total_reserve)
    _send_telegram(message)


if __name__ == "__main__":
    main()
