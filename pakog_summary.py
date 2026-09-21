import json, os, urllib.parse, urllib.request
from datetime import datetime, timezone
from strategy_pakog import INITIAL_CAPITAL

SLOTS = [
    {"label": "Slot 1", "state_file": "state_pakog_1.json"},
    {"label": "Slot 2", "state_file": "state_pakog_2.json"},
]
SEP = "\u2500" * 28
PHASE_LABELS = {
    "risk":         "Risk \U0001f6a8",
    "proven":       "Proven \u2705",
    "trend_riding": "Trend-Riding \U0001f680",
}

def fetch_price(coin):
    try:
        url = f"https://indodax.com/api/ticker/{coin.lower()}_idr"
        req = urllib.request.Request(url, headers={"User-Agent": "unyil-pakog-v2/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return float(data["ticker"]["last"])
    except:
        return 0.0

def send_telegram(message):
    print(message, flush=True)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot_token and chat_id):
        return
    url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req  = urllib.request.Request(url, data=data)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        if not body.get("ok"):
            print(f"[TELEGRAM FAILED] {body}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM FAILED] {e}", flush=True)

def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M WIB")
    total_initial = 0.0
    total_value   = 0.0
    total_reserve = 0.0
    lines = ["\U0001f4ca Pak Ogah Lite v2 Summary", now_str, SEP]
    for slot_info in SLOTS:
        path  = slot_info["state_file"]
        label = slot_info["label"]
        if not os.path.exists(path):
            lines.append(f"{label}: no data yet")
            lines.append("")
            total_initial += INITIAL_CAPITAL
            total_value   += INITIAL_CAPITAL
            continue
        with open(path) as f:
            d = json.load(f)
        initial  = d.get("initial_capital", INITIAL_CAPITAL)
        balance  = d.get("balance", initial)
        reserve  = d.get("profit_reserve", 0.0)
        deployed = d.get("deployed_capital", 0.0)
        coin     = d.get("coin")
        qty      = d.get("qty_coin", 0.0)
        trades   = d.get("trade_count", 0)
        halted   = d.get("halted", False)
        phase    = d.get("phase", "risk")
        scans    = d.get("scans_since_entry", 0)
        if coin and qty > 0:
            price      = fetch_price(coin)
            open_value = qty * price if price > 0 else deployed
            curr_value = open_value + reserve
            pnl_pct    = (open_value - deployed) / deployed * 100 if deployed > 0 else 0.0
            phase_str  = PHASE_LABELS.get(phase, phase)
            status     = f"{coin.upper()} (holding)"
            pos_line   = (
                f"  Deployed: Rp {deployed:,.0f}\n"
                f"  Current: Rp {open_value:,.0f} ({pnl_pct:+.2f}%)\n"
                f"  Phase: {phase_str} | Scan #{scans}"
            )
        else:
            curr_value = balance + reserve
            status     = "in cash" + (" -- HALTED" if halted else "")
            pos_line   = f"  Balance: Rp {balance:,.0f}"
        total_initial += initial
        total_value   += curr_value
        total_reserve += reserve
        lines.append(f"{label}: {status}")
        lines.append(pos_line)
        lines.append(f"  Reserve: Rp {reserve:,.0f}")
        lines.append(f"  All-time trades: {trades}")
        lines.append("")
    total_pnl     = total_value - total_initial
    total_pnl_pct = (total_pnl / total_initial * 100) if total_initial > 0 else 0.0
    lines.append(SEP)
    lines.append(f"Total initial:  Rp {total_initial:,.0f}")
    lines.append(f"Total reserve:  Rp {total_reserve:,.0f}")
    lines.append(f"Total value:    Rp {total_value:,.0f}")
    lines.append(f"Total P&L:      Rp {total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)")
    lines.append(SEP)
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
