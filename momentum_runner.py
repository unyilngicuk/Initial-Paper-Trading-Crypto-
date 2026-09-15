"""
Unyil Momentum -- live runner.
Runs every 15 minutes via GitHub Actions. Checks exits for held coins,
scans for new entries in empty slots. Paper trading only.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from strategy_momentum import (
    CAPITAL_PER_SLOT, EXCLUDED_COINS, HARD_STOP_PCT,
    MIN_GAIN_24H_PCT, ROUNDTRIP_FEE_PCT, TRAIL_PCT,
    MomentumSlot, check_exit, qualifies_for_entry,
)

SLOT_STATE_PATHS = [
    os.environ.get("SLOT_1_STATE_PATH", "state_momentum_1.json"),
    os.environ.get("SLOT_2_STATE_PATH", "state_momentum_2.json"),
]

INDODAX_BASE = "https://indodax.com"
UA = "unyil-momentum/1.0"
PAPER_TRADING_ONLY = True
MIN_VOL_IDR = 100_000_000
ROUTINE_NOTIFY_THROTTLE_SECONDS = int(
    os.environ.get("ROUTINE_NOTIFY_THROTTLE_SECONDS", 2 * 3600)
)


def notify(message: str) -> None:
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


def notify_throttled(slot: MomentumSlot, message: str) -> None:
    print(f"[NOTIFY, throttled] {message}", flush=True)
    now = time.time()
    if now - slot.last_routine_notify_ts < ROUTINE_NOTIFY_THROTTLE_SECONDS:
        return
    slot.last_routine_notify_ts = now
    notify(message)


def load_slot(path: str, slot_id: int) -> MomentumSlot:
    if not os.path.exists(path):
        return MomentumSlot(slot_id=slot_id, idr=CAPITAL_PER_SLOT)
    with open(path) as f:
        d = json.load(f)
    return MomentumSlot(**d)


def save_slot(slot: MomentumSlot, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(slot), f, indent=2)
    os.replace(tmp, path)


def fetch_summaries() -> Dict[str, Any]:
    url = f"{INDODAX_BASE}/api/summaries"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_current_price(coin: str) -> Optional[float]:
    try:
        url = f"{INDODAX_BASE}/api/ticker/{coin}_idr"
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return float(data["ticker"]["last"])
    except Exception as e:
        print(f"[WARN] could not fetch price for {coin}: {e}", flush=True)
        return None


def scan_market(summaries: Dict[str, Any], already_held: set) -> List[Dict]:
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

        if not qualifies_for_entry(coin, current, price_24h, vol_idr):
            continue
        if vol_idr < MIN_VOL_IDR:
            continue

        gain_pct = (current - price_24h) / price_24h
        candidates.append({
            "coin": coin,
            "current_price": current,
            "price_24h_ago": price_24h,
            "gain_pct": gain_pct,
            "vol_idr": vol_idr,
        })

    candidates.sort(key=lambda x: x["vol_idr"], reverse=True)
    return candidates


def handle_exit(slot: MomentumSlot, current_price: float, path: str) -> bool:
    reason, exit_type = check_exit(slot, current_price)
    if not exit_type:
        return False

    proceeds = slot.qty_coin * current_price * (1 - 0.0023)
    slot.idr = proceeds
    pnl = proceeds - CAPITAL_PER_SLOT
    pnl_pct = pnl / CAPITAL_PER_SLOT * 100

    coin = slot.coin.upper()
    slot.coin = None
    slot.qty_coin = 0.0
    slot.entry_price = 0.0
    slot.peak_price = 0.0
    slot.entry_ts = 0

    save_slot(slot, path)

    notify(
        f"[MOMENTUM slot {slot.slot_id}] EXIT -- {coin}\n"
        f"Reason: {reason}\n"
        f"P&L: Rp {pnl:+,.0f} ({pnl_pct:+.2f}%)\n"
        f"Cash returned to slot: Rp {slot.idr:,.0f}"
    )
    return True


def handle_entry(slot: MomentumSlot, candidate: Dict, path: str) -> None:
    if PAPER_TRADING_ONLY and os.environ.get("EXECUTE", "false").lower() == "true":
        notify(
            f"[MOMENTUM slot {slot.slot_id}] BLOCKED: real execution disabled "
            f"(paper trading only). Would have entered {candidate['coin'].upper()}."
        )
        return

    coin = candidate["coin"]
    price = candidate["current_price"]
    gain_pct = candidate["gain_pct"]
    vol_idr = candidate["vol_idr"]

    investable = slot.idr * (1 - 0.000111)
    qty = investable / price

    slot.coin = coin
    slot.entry_price = price
    slot.peak_price = price
    slot.qty_coin = qty
    slot.idr = 0.0
    slot.entry_ts = int(time.time())

    save_slot(slot, path)

    notify(
        f"[MOMENTUM slot {slot.slot_id}] ENTRY [PAPER TRADE]\n"
        f"Coin: {coin.upper()}\n"
        f"24h gain: {gain_pct:.1%} | Vol IDR: Rp {vol_idr:,.0f}\n"
        f"Entry price: Rp {price:,.0f}\n"
        f"Qty: {qty:.6f} {coin.upper()}\n"
        f"Capital: Rp {CAPITAL_PER_SLOT:,.0f}\n"
        f"Exits: hard stop {HARD_STOP_PCT:.0%} | breakeven+fees | trail {TRAIL_PCT:.0%}"
    )


def main() -> int:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[MOMENTUM] Running at {now_utc}", flush=True)

    slots = [load_slot(path, i + 1) for i, path in enumerate(SLOT_STATE_PATHS)]

    if any(s.halted for s in slots):
        notify("[MOMENTUM] One or more slots are halted -- skipping this cycle.")
        return 0

    try:
        summaries = fetch_summaries()
    except Exception as e:
        notify(f"[MOMENTUM] Could not fetch market summaries: {e}. Skipping.")
        return 0

    # Phase 1: check exits for occupied slots
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if not slot.is_occupied:
            continue
        current_price = fetch_current_price(slot.coin)
        if current_price is None:
            notify(f"[MOMENTUM slot {slot.slot_id}] Could not fetch price for "
                   f"{slot.coin.upper()} -- skipping exit check this cycle.")
            continue

        equity = slot.qty_coin * current_price
        pct = (equity - CAPITAL_PER_SLOT) / CAPITAL_PER_SLOT * 100
        exited = handle_exit(slot, current_price, path)

        if not exited:
            notify_throttled(
                slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Coin: {slot.coin.upper()}\n"
                f"Price: Rp {current_price:,.0f}\n"
                f"Equity: Rp {equity:,.0f} ({pct:+.2f}%)\n"
                f"Peak: Rp {slot.peak_price:,.0f}\n"
                f"Action: holding -- no exit triggered"
            )
            save_slot(slot, path)

    # Phase 2: fill empty slots from scanner
    candidates = scan_market(summaries, already_held={s.coin for s in slots if s.coin})

    cand_idx = 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.is_occupied:
            continue
        if cand_idx >= len(candidates):
            notify_throttled(
                slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Status: empty -- no qualifying coins (>={MIN_GAIN_24H_PCT:.0%} gain, "
                f">{MIN_VOL_IDR/1e6:.0f}M IDR volume) found this check."
            )
            save_slot(slot, path)
            continue

        candidate = candidates[cand_idx]
        cand_idx += 1
        handle_entry(slot, candidate, path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
