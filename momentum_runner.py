
import json, os, sys, time, urllib.parse, urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from strategy_momentum import (
    COOLDOWN_HOURS, EXCLUDED_COINS, HALT_THRESHOLD, HARD_STOP_PCT,
    INITIAL_CAPITAL, MAX_GAIN_10H_PCT, MIN_GAIN_24H_PCT,
    MIN_PRICE_IDR, MIN_VOL_IDR, ROUNDTRIP_FEE_PCT, TRAIL_PCT,
    MomentumSlot, check_exit, qualifies_for_entry,
)

SLOT_STATE_PATHS = [
    os.environ.get("SLOT_1_STATE_PATH", "state_momentum_1.json"),
    os.environ.get("SLOT_2_STATE_PATH", "state_momentum_2.json"),
]
INDODAX_BASE = "https://indodax.com"
UA = "unyil-momentum/1.0"
PAPER_TRADING_ONLY = True
ROUTINE_NOTIFY_THROTTLE_SECONDS = int(os.environ.get("ROUTINE_NOTIFY_THROTTLE_SECONDS", 2*3600))


def notify(message):
    print(f"[NOTIFY] {message}", flush=True)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
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


def notify_throttled(slot, message):
    print(f"[NOTIFY, throttled] {message}", flush=True)
    now = time.time()
    if now - slot.last_routine_notify_ts < ROUTINE_NOTIFY_THROTTLE_SECONDS:
        return
    slot.last_routine_notify_ts = now
    notify(message)


def load_slot(path, slot_id):
    if not os.path.exists(path):
        return MomentumSlot(slot_id=slot_id)
    with open(path) as f:
        d = json.load(f)
    d.setdefault("initial_capital", INITIAL_CAPITAL)
    d.setdefault("balance", d.pop("idr", INITIAL_CAPITAL))
    d.setdefault("trade_count", 0)
    d.setdefault("total_pnl", 0.0)
    d.setdefault("loss_cooldown", {})
    for old in ["idr", "bars_seen"]:
        d.pop(old, None)
    return MomentumSlot(**d)


def save_slot(slot, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(slot), f, indent=2)
    os.replace(tmp, path)


def fetch_summaries():
    url = f"{INDODAX_BASE}/api/summaries"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_current_price(coin):
    try:
        url = f"{INDODAX_BASE}/api/ticker/{coin}_idr"
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return float(data["ticker"]["last"])
    except Exception as e:
        print(f"[WARN] could not fetch price for {coin}: {e}", flush=True)
        return None


def fetch_price_10h_ago(coin):
    try:
        now_ts = int(time.time())
        ts_10h = now_ts - 10 * 3600
        url = (f"{INDODAX_BASE}/tradingview/history"
               f"?symbol={coin.upper()}_IDR&resolution=60"
               f"&from={ts_10h - 3600}&to={ts_10h + 3600}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        closes = data.get("c", [])
        return float(closes[-1]) if closes else None
    except Exception as e:
        print(f"[WARN] could not fetch 10h price for {coin}: {e}", flush=True)
        return None


def scan_market(summaries, already_held):
    tickers = summaries.get("tickers", {})
    prices_24h = summaries.get("prices_24h", {})
    candidates = []
    for pair_id, ticker in tickers.items():
        if not pair_id.endswith("_idr"):
            continue
        coin = pair_id[:-4]
        if coin in EXCLUDED_COINS or coin in already_held:
            continue
        pair_key = pair_id.replace("_", "")
        price_24h_str = prices_24h.get(pair_key)
        if not price_24h_str:
            continue
        try:
            current = float(ticker.get("last", 0))
            price_24h = float(price_24h_str)
            vol_idr = float(ticker.get("vol_idr", 0))
        except (ValueError, TypeError):
            continue
        if current < MIN_PRICE_IDR or vol_idr < MIN_VOL_IDR or price_24h <= 0:
            continue
        gain_24h = (current - price_24h) / price_24h
        if gain_24h < MIN_GAIN_24H_PCT:
            continue
        candidates.append({
            "coin": coin, "current_price": current,
            "price_24h_ago": price_24h, "gain_24h_pct": gain_24h, "vol_idr": vol_idr,
        })
    candidates.sort(key=lambda x: x["vol_idr"], reverse=True)

    # Always log to stdout for Actions log visibility
    eligible = total_idr_pairs - rejected["excluded_or_held"]
    log_lines = [
        f"[SCAN] {total_idr_pairs} IDR pairs scanned, "
        f"{eligible} eligible (excl. portfolio/held)",
        f"  below Rp{MIN_PRICE_IDR:,.0f}/coin: {rejected['below_min_price']}",
        f"  below Rp{MIN_VOL_IDR/1e6:.0f}M volume:  {rejected['below_min_volume']}",
        f"  below {MIN_GAIN_24H_PCT:.0%} 24h gain:  {rejected['below_15pct_24h']}",
        f"  passed all pre-filters:    {rejected['passed_all']} "
        f"(10h check happens at entry)",
        f"  top candidates: "
        f"{', '.join(c['coin'].upper() + ' (' + f"{c['gain_24h_pct']:.0%}" + ')' for c in candidates[:5]) or 'none'}",
    ]
    for line in log_lines:
        print(line, flush=True)

    scan_summary = "\n".join(log_lines)
    for slot in slots:
        slot._scan_summary = scan_summary

    return candidates


def handle_exit(slot, current_price, path):
    reason, exit_type = check_exit(slot, current_price)
    if not exit_type:
        return False
    invested = slot.balance
    proceeds = slot.qty_coin * current_price * (1.0 - ROUNDTRIP_FEE_PCT)
    trade_pnl = proceeds - invested
    trade_pnl_pct = (trade_pnl / invested * 100) if invested > 0 else 0.0
    slot.balance = proceeds
    slot.total_pnl += trade_pnl
    slot.trade_count += 1
    overall_pct = slot.balance_pct
    coin = slot.coin
    if trade_pnl < 0:
        slot.loss_cooldown[coin] = time.time() + COOLDOWN_HOURS * 3600
    slot.coin = None
    slot.qty_coin = 0.0
    slot.entry_price = 0.0
    slot.peak_price = 0.0
    slot.entry_ts = 0
    if slot.is_halted_by_loss:
        slot.halted = True
    save_slot(slot, path)
    halt_warning = (
        f"\nSLOT HALTED: balance Rp {slot.balance:,.0f} ({overall_pct:+.1f}%) "
        f"-- 40% loss threshold reached. Manual reset required."
        if slot.halted else ""
    )
    notify(
        f"[MOMENTUM slot {slot.slot_id}] EXIT -- {coin.upper()}\n"
        f"Reason: {reason}\n"
        f"Trade P&L: Rp {trade_pnl:+,.0f} ({trade_pnl_pct:+.2f}% on this trade)\n"
        f"Slot balance: Rp {slot.balance:,.0f} ({overall_pct:+.2f}% from start)\n"
        f"Total P&L all trades: Rp {slot.total_pnl:+,.0f}"
        f"{halt_warning}"
    )
    return True


def handle_entry(slot, candidate, price_10h_ago, path):
    qualified, reason = qualifies_for_entry(
        coin=candidate["coin"],
        current_price=candidate["current_price"],
        price_24h_ago=candidate["price_24h_ago"],
        price_10h_ago=price_10h_ago or 0.0,
        vol_idr=candidate["vol_idr"],
        slot=slot,
    )
    if not qualified:
        print(f"[SKIP] {candidate['coin']}: {reason}", flush=True)
        return
    coin = candidate["coin"]
    price = candidate["current_price"]
    gain_24h = candidate["gain_24h_pct"]
    vol_idr = candidate["vol_idr"]
    gain_10h_str = ""
    if price_10h_ago and price_10h_ago > 0:
        gain_10h = (price - price_10h_ago) / price_10h_ago
        gain_10h_str = f" | 10h: {gain_10h:.1%}"
    investable = slot.balance * (1.0 - 0.000111)
    qty = investable / price
    slot.coin = coin
    slot.entry_price = price
    slot.peak_price = price
    slot.qty_coin = qty
    slot.entry_ts = int(time.time())
    save_slot(slot, path)
    notify(
        f"[MOMENTUM slot {slot.slot_id}] ENTRY [PAPER TRADE]\n"
        f"Coin: {coin.upper()}\n"
        f"24h gain: {gain_24h:.1%}{gain_10h_str} | Vol: Rp {vol_idr/1e6:.0f}M\n"
        f"Entry price: Rp {price:,.0f}\n"
        f"Qty: {qty:.6f} {coin.upper()}\n"
        f"Capital deployed: Rp {slot.balance:,.0f} (slot: {slot.balance_pct:+.1f}% from start)\n"
        f"Exits: hard stop {HARD_STOP_PCT:.0%} | breakeven+fees | trail {TRAIL_PCT:.0%}"
    )


def main():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[MOMENTUM] Running at {now_utc}", flush=True)
    slots = [load_slot(path, i+1) for i, path in enumerate(SLOT_STATE_PATHS)]
    if all(s.halted or s.is_halted_by_loss for s in slots):
        notify("[MOMENTUM] Both slots halted -- no action.")
        return 0
    try:
        summaries = fetch_summaries()
    except Exception as e:
        notify(f"[MOMENTUM] Could not fetch market data: {e}. Skipping.")
        return 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.halted or slot.is_halted_by_loss or not slot.is_occupied:
            continue
        current_price = fetch_current_price(slot.coin)
        if current_price is None:
            notify(f"[MOMENTUM slot {slot.slot_id}] Could not fetch price for {slot.coin.upper()}.")
            continue
        equity = slot.qty_coin * current_price
        trade_pct = (equity - slot.balance) / slot.balance * 100 if slot.balance > 0 else 0.0
        exited = handle_exit(slot, current_price, path)
        if not exited:
            notify_throttled(
                slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Coin: {slot.coin.upper()}\n"
                f"Price: Rp {current_price:,.0f}\n"
                f"This trade: Rp {equity:,.0f} ({trade_pct:+.2f}%)\n"
                f"Peak: Rp {slot.peak_price:,.0f}\n"
                f"Slot balance from start: {slot.balance_pct:+.2f}%\n"
                f"Action: holding -- no exit triggered"
            )
            save_slot(slot, path)
    candidates = scan_market(summaries, already_held={s.coin for s in slots if s.coin})
    cand_idx = 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.halted or slot.is_halted_by_loss or slot.is_occupied:
            continue
        if cand_idx >= len(candidates):
            scan_summary = getattr(slot, "_scan_summary", "")
            notify_throttled(
                slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Status: empty -- no qualifying coins found\n"
                f"Slot balance from start: {slot.balance_pct:+.2f}%\n"
                f"{scan_summary}"
            )
            save_slot(slot, path)
            continue
        entered = False
        while cand_idx < len(candidates) and not entered:
            candidate = candidates[cand_idx]
            cand_idx += 1
            price_10h = fetch_price_10h_ago(candidate["coin"])
            handle_entry(slot, candidate, price_10h, path)
            entered = slot.is_occupied
    return 0


if __name__ == "__main__":
    sys.exit(main())
