import json, os, sys, time, urllib.parse, urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from strategy_momentum import (
    COOLDOWN_HOURS, EXCLUDED_COINS, HALT_THRESHOLD, HARD_STOP_PCT,
    INITIAL_CAPITAL, MAX_GAIN_24H_PCT, MIN_GAIN_10H_PCT, MIN_GAIN_24H_PCT,
    MIN_PRICE_IDR, MIN_VOL_IDR, PROFIT_COOLDOWN_EXEMPT,
    PROFIT_SWEEP_PCT, PROFIT_SWEEP_THRESHOLD, PROTECTION_THRESHOLD,
    ROUNDTRIP_FEE_PCT, TRAIL_PCT,
    MomentumSlot, check_exit, qualifies_for_entry,
)

SLOT_STATE_PATHS = [
    os.environ.get("SLOT_1_STATE_PATH", "state_momentum_1.json"),
    os.environ.get("SLOT_2_STATE_PATH", "state_momentum_2.json"),
]
INDODAX_BASE = "https://indodax.com"
UA = "unyil-momentum/1.0"
PAPER_TRADING_ONLY = True
ROUTINE_NOTIFY_THROTTLE_SECONDS = int(os.environ.get("ROUTINE_NOTIFY_THROTTLE_SECONDS", 3600))
SEP = "\u2500" * 28


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
    if d.get("balance") is None:
        d["balance"] = INITIAL_CAPITAL
    d.setdefault("trade_count", 0)
    d.setdefault("total_pnl", 0.0)
    d.setdefault("profit_reserve", 0.0)
    d.setdefault("loss_cooldown", {})
    d.setdefault("deployed_capital", 0.0)
    for old in ["idr", "bars_seen", "lots", "principal", "anchor",
                "_pending_anchor", "closes", "strategy_name"]:
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
        print(f"[WARN] price fetch failed for {coin}: {e}", flush=True)
        return None


def fetch_10h_price(coin):
    """Fetch price ~10 hours ago for momentum confirmation (must be >5% gain)."""
    try:
        now_ts = int(time.time())
        from_ts = now_ts - 11 * 3600
        url = (f"{INDODAX_BASE}/tradingview/history"
               f"?symbol={coin.upper()}_IDR&resolution=60"
               f"&from={from_ts}&to={now_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode().strip()
        if not raw:
            return None
        data = json.loads(raw)
        closes = data.get("c", [])
        if not closes:
            return None
        return float(closes[-10]) if len(closes) >= 10 else float(closes[0])
    except Exception as e:
        print(f"[WARN] 10h fetch failed for {coin}: {e}", flush=True)
        return None


def scan_market(summaries, already_held, slots):
    tickers = summaries.get("tickers", {})
    prices_24h = summaries.get("prices_24h", {})
    total_idr_pairs = 0
    rejected = {"excluded_or_held": 0, "no_price_data": 0,
                "below_min_price": 0, "below_min_volume": 0,
                "below_gain_threshold": 0, "passed_prefilter": 0}
    candidates = []
    for pair_id, ticker in tickers.items():
        if not pair_id.endswith("_idr"):
            continue
        total_idr_pairs += 1
        coin = pair_id[:-4]
        if coin in EXCLUDED_COINS or coin in already_held:
            rejected["excluded_or_held"] += 1
            continue
        pair_key = pair_id.replace("_", "")
        price_24h_str = prices_24h.get(pair_key)
        if not price_24h_str:
            rejected["no_price_data"] += 1
            continue
        try:
            current = float(ticker.get("last", 0))
            price_24h = float(price_24h_str)
            vol_idr = float(ticker.get("vol_idr", 0))
        except (ValueError, TypeError):
            rejected["no_price_data"] += 1
            continue
        if current < MIN_PRICE_IDR:
            rejected["below_min_price"] += 1
            continue
        if vol_idr < MIN_VOL_IDR:
            rejected["below_min_volume"] += 1
            continue
        if price_24h <= 0:
            rejected["no_price_data"] += 1
            continue
        gain_24h = (current - price_24h) / price_24h
        if gain_24h < MIN_GAIN_24H_PCT or gain_24h > MAX_GAIN_24H_PCT:
            rejected["below_gain_threshold"] += 1
            continue
        rejected["passed_prefilter"] += 1
        candidates.append({"coin": coin, "current_price": current,
                            "price_24h_ago": price_24h,
                            "gain_24h_pct": gain_24h, "vol_idr": vol_idr})
    # Rank by IDR volume (highest liquidity first) -- v1 selection method
    candidates.sort(key=lambda x: x["vol_idr"], reverse=True)
    eligible = total_idr_pairs - rejected["excluded_or_held"]
    top_str = ", ".join(
        c["coin"].upper() + " (" + "{:.0%}".format(c["gain_24h_pct"]) + ")"
        for c in candidates[:5]) or "none"
    log_lines = [
        f"[SCAN] {total_idr_pairs} IDR pairs, {eligible} eligible",
        f"  below Rp{MIN_PRICE_IDR:,.0f}/coin:   {rejected['below_min_price']}",
        f"  below Rp{MIN_VOL_IDR//1_000_000}M volume: {rejected['below_min_volume']}",
        f"  outside {MIN_GAIN_24H_PCT:.0%}-{MAX_GAIN_24H_PCT:.0%} 24h band: {rejected['below_gain_threshold']}",
        f"  passed pre-filter: {rejected['passed_prefilter']}",
        f"  top: {top_str}",
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
    invested = slot.deployed_capital if slot.deployed_capital > 0 else slot.initial_capital
    proceeds = slot.qty_coin * current_price * (1.0 - ROUNDTRIP_FEE_PCT)
    trade_pnl = proceeds - invested
    trade_pnl_pct = (trade_pnl / invested * 100) if invested > 0 else 0.0
    swept = 0.0
    if proceeds >= PROTECTION_THRESHOLD and trade_pnl_pct / 100 >= PROFIT_SWEEP_THRESHOLD:
        swept = trade_pnl * PROFIT_SWEEP_PCT
        slot.profit_reserve += swept
        slot.balance = proceeds - swept
    else:
        slot.balance = proceeds
    slot.deployed_capital = 0.0
    slot.total_pnl += trade_pnl
    slot.trade_count += 1
    overall_pct = slot.balance_pct
    total_value = slot.balance + slot.profit_reserve
    coin = slot.coin
    profit_pct = trade_pnl / invested if invested > 0 else 0.0
    if profit_pct < PROFIT_COOLDOWN_EXEMPT:
        slot.loss_cooldown[coin] = time.time() + COOLDOWN_HOURS * 3600
        print(f"[COOLDOWN] {coin}: {profit_pct:.1%} -- cooldown {COOLDOWN_HOURS}h", flush=True)
    else:
        print(f"[NO COOLDOWN] {coin}: {profit_pct:.1%} -- re-entry allowed", flush=True)
    slot.coin = None
    slot.qty_coin = 0.0
    slot.entry_price = 0.0
    slot.peak_price = 0.0
    slot.entry_ts = 0
    if slot.is_halted_by_loss:
        slot.halted = True
    save_slot(slot, path)
    swept_line = (f"\nReserve this trade: Rp {swept:,.0f}\nTotal reserve: Rp {slot.profit_reserve:,.0f}"
                  if swept > 0 else "")
    halt_warning = (f"\nSLOT HALTED: balance Rp {slot.balance:,.0f} -- 40% loss reached."
                    if slot.halted else "")
    notify(
        f"[MOMENTUM slot {slot.slot_id}] EXIT -- {coin.upper()}\n"
        f"Reason: {reason}\n"
        f"Trade P&L: Rp {trade_pnl:+,.0f} ({trade_pnl_pct:+.2f}%)\n"
        f"{SEP}\n"
        f"Balance: Rp {slot.balance:,.0f}\n"
        f"Total reserve: Rp {slot.profit_reserve:,.0f}"
        f"{swept_line}\n"
        f"Total value: Rp {total_value:,.0f} ({overall_pct:+.2f}% from start)\n"
        f"All-time P&L: Rp {slot.total_pnl:+,.0f} ({slot.trade_count} trades)"
        f"{halt_warning}"
    )
    return True


def handle_entry(slot, candidate, path):
    coin = candidate["coin"]
    price_5h_ago = fetch_10h_price(coin)
    current_price = candidate["current_price"]
    price_24h_ago = candidate.get("price_24h_ago", 0)
    qualified, reason = qualifies_for_entry(
        coin=coin, current_price=current_price,
        price_24h_ago=price_24h_ago, price_5h_ago=price_5h_ago,
        vol_idr=candidate["vol_idr"], slot=slot)
    if not qualified:
        print(f"[SKIP] {coin}: {reason}", flush=True)
        return
    gain_10h_str = ""
    if price_5h_ago and price_5h_ago > 0:
        gain_10h = (current_price - price_5h_ago) / price_5h_ago
        gain_10h_str = f" | 10h: {gain_10h:.1%}"
    high_str = ""
    investable = slot.balance * (1.0 - 0.000111)
    qty = investable / current_price
    slot.deployed_capital = slot.balance
    slot.coin = coin
    slot.entry_price = current_price
    slot.peak_price = current_price
    slot.qty_coin = qty
    slot.balance = 0.0
    slot.entry_ts = int(time.time())
    save_slot(slot, path)
    notify(
        f"[MOMENTUM slot {slot.slot_id}] ENTRY [PAPER TRADE]\n"
        f"Coin: {coin.upper()}\n"
        f"24h: {candidate['gain_24h_pct']:.1%}{gain_10h_str}\n"
        f"Entry: Rp {current_price:,.0f} | Vol: Rp {candidate['vol_idr']/1e6:.0f}M\n"
        f"Qty: {qty:.4f} {coin.upper()}\n"
        f"Capital deployed: Rp {slot.deployed_capital:,.0f}\n"
        f"Total reserve: Rp {slot.profit_reserve:,.0f}\n"
        f"Exits: hard {HARD_STOP_PCT:.0%} | breakeven | trail {TRAIL_PCT:.0%}"
    )


def main():
    print(f"[MOMENTUM] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
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
            notify(f"[MOMENTUM slot {slot.slot_id}] Price fetch failed for {slot.coin.upper()}.")
            continue
        equity = slot.qty_coin * current_price
        deployed = slot.deployed_capital if slot.deployed_capital > 0 else slot.initial_capital
        trade_pct = (equity - deployed) / deployed * 100 if deployed > 0 else 0.0
        exited = handle_exit(slot, current_price, path)
        if not exited:
            notify_throttled(slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Coin: {slot.coin.upper()}\n"
                f"Price: Rp {current_price:,.0f}\n"
                f"Trade: Rp {equity:,.0f} ({trade_pct:+.2f}%) | Peak: Rp {slot.peak_price:,.0f}\n"
                f"Total reserve: Rp {slot.profit_reserve:,.0f}\n"
                f"Action: holding")
            save_slot(slot, path)
    candidates = scan_market(summaries,
                             already_held={s.coin for s in slots if s.coin},
                             slots=slots)
    cand_idx = 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.halted or slot.is_halted_by_loss or slot.is_occupied:
            continue
        if cand_idx >= len(candidates):
            scan_summary = getattr(slot, "_scan_summary", "")
            notify_throttled(slot,
                f"[MOMENTUM slot {slot.slot_id}] Check-in\n"
                f"Status: empty\n"
                f"Balance: Rp {slot.balance:,.0f} | Reserve: Rp {slot.profit_reserve:,.0f}\n"
                f"{scan_summary}")
            save_slot(slot, path)
            continue
        entered = False
        while cand_idx < len(candidates) and not entered:
            candidate = candidates[cand_idx]
            cand_idx += 1
            handle_entry(slot, candidate, path)
            entered = slot.is_occupied
    return 0

if __name__ == "__main__":
    sys.exit(main())
