import json, os, sys, time, urllib.parse, urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from strategy_pakog import (
    COLLAPSE_THRESHOLD, COOLDOWN_HOURS, EXCLUDED_COINS,
    HALT_THRESHOLD, INITIAL_CAPITAL, MIN_VOL_IDR,
    PROFIT_COOLDOWN_EXEMPT, PROFIT_SWEEP_PCT,
    PROFIT_SWEEP_THRESHOLD, PROTECTION_THRESHOLD,
    ROUNDTRIP_FEE_PCT, TRAIL_PCT, EMERGENCY_FLOOR_PCT,
    PakOgahSlot, check_exit, compute_score,
)

SLOT_STATE_PATHS = [
    os.environ.get("SLOT_1_STATE_PATH", "state_pakog_1.json"),
    os.environ.get("SLOT_2_STATE_PATH", "state_pakog_2.json"),
]
INDODAX_BASE = "https://indodax.com"
UA = "unyil-pakog-lite-v2/1.0"
ROUTINE_NOTIFY_THROTTLE_SECONDS = int(os.environ.get("ROUTINE_NOTIFY_THROTTLE_SECONDS", 3600))
SEP = "\u2500" * 28
PHASE_LABELS = {"risk": "Risk \U0001f6a8", "proven": "Proven \u2705", "trend_riding": "Trend-Riding \U0001f680"}

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
        return PakOgahSlot(slot_id=slot_id)
    with open(path) as f:
        d = json.load(f)
    d.setdefault("initial_capital", INITIAL_CAPITAL)
    d.setdefault("balance", INITIAL_CAPITAL)
    d.setdefault("trade_count", 0)
    d.setdefault("total_pnl", 0.0)
    d.setdefault("profit_reserve", 0.0)
    d.setdefault("loss_cooldown", {})
    d.setdefault("deployed_capital", 0.0)
    d.setdefault("breakeven_active", False)
    d.setdefault("trail_active", False)
    d.setdefault("phase", "risk")
    d.setdefault("scans_since_entry", 0)
    return PakOgahSlot(**d)

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
        return 0.0

def scan_and_score(summaries, already_held, slots):
    tickers = summaries.get("tickers", {})
    prices_24h = summaries.get("prices_24h", {})
    prices_7d = summaries.get("prices_7d", {})
    candidates = []
    rejected = {"excluded": 0, "no_data": 0, "low_volume": 0, "scored": 0}
    for pair_id, ticker in tickers.items():
        if not pair_id.endswith("_idr"):
            continue
        coin = pair_id[:-4]
        if coin in EXCLUDED_COINS or coin in already_held:
            rejected["excluded"] += 1
            continue
        pair_key = pair_id.replace("_", "")
        p24h = prices_24h.get(pair_key)
        p7d  = prices_7d.get(pair_key)
        if not p24h or not p7d:
            rejected["no_data"] += 1
            continue
        try:
            vol_idr = float(ticker.get("vol_idr", 0))
        except (TypeError, ValueError):
            rejected["no_data"] += 1
            continue
        if vol_idr < MIN_VOL_IDR:
            rejected["low_volume"] += 1
            continue
        total, scores = compute_score(ticker, p24h, p7d)
        if total <= 0:
            rejected["no_data"] += 1
            continue
        rejected["scored"] += 1
        candidates.append({
            "coin": coin, "score": total, "scores": scores,
            "current_price": float(ticker.get("last", 0)),
            "vol_idr": vol_idr, "ticker": ticker,
            "price_24h": float(p24h), "price_7d": float(p7d),
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    total_pairs = len([k for k in tickers if k.endswith("_idr")])
    top_str = ", ".join(c["coin"].upper()+"("+str(c["score"])+"/70)" for c in candidates[:5]) or "none"
    log_lines = [
        f"[PAK OGAH v2 SCAN] {total_pairs} IDR pairs",
        f"  excluded/held:  {rejected['excluded']}",
        f"  below Rp100M:   {rejected['low_volume']}",
        f"  no data:        {rejected['no_data']}",
        f"  scored:         {rejected['scored']}",
        f"  top: {top_str}",
    ]
    for line in log_lines:
        print(line, flush=True)
    scan_summary = "\n".join(log_lines)
    for slot in slots:
        slot._scan_summary = scan_summary
    return candidates

def handle_exit(slot, current_price, current_score, path):
    prev_phase = slot.phase
    reason, exit_type = check_exit(slot, current_price, current_score)
    if slot.phase != prev_phase:
        notify(
            f"[PAK OGAH v2 slot {slot.slot_id}] PHASE TRANSITION\n"
            f"Coin: {slot.coin.upper()}\n"
            f"{prev_phase.upper()} \u2192 {slot.phase.upper()}\n"
            f"Price: Rp {current_price:,.0f}\n"
            f"Score: {current_score}/70"
        )
    if not exit_type:
        return False
    invested = slot.deployed_capital if slot.deployed_capital > 0 else slot.initial_capital
    proceeds = slot.qty_coin * current_price * (1.0 - ROUNDTRIP_FEE_PCT)
    trade_pnl = proceeds - invested
    trade_pct = (trade_pnl / invested * 100) if invested > 0 else 0.0
    swept = 0.0
    if proceeds >= PROTECTION_THRESHOLD and trade_pnl / invested >= PROFIT_SWEEP_THRESHOLD:
        swept = trade_pnl * PROFIT_SWEEP_PCT
        slot.profit_reserve += swept
        slot.balance = proceeds - swept
    else:
        slot.balance = proceeds
    slot.deployed_capital = 0.0
    slot.breakeven_active = False
    slot.trail_active = False
    slot.total_pnl += trade_pnl
    slot.trade_count += 1
    overall_pct = slot.balance_pct
    total_value = slot.balance + slot.profit_reserve
    coin = slot.coin
    profit_pct = trade_pnl / invested if invested > 0 else 0.0
    if profit_pct < PROFIT_COOLDOWN_EXEMPT:
        slot.loss_cooldown[coin] = time.time() + COOLDOWN_HOURS * 3600
        print(f"[COOLDOWN] {coin}: {profit_pct:.1%} -- {COOLDOWN_HOURS}h", flush=True)
    else:
        print(f"[NO COOLDOWN] {coin}: {profit_pct:.1%} -- re-entry allowed", flush=True)
    slot.coin = None
    slot.qty_coin = 0.0
    slot.entry_price = 0.0
    slot.peak_price = 0.0
    slot.entry_ts = 0
    slot.phase = "risk"
    slot.scans_since_entry = 0
    if slot.is_halted_by_loss:
        slot.halted = True
    save_slot(slot, path)
    swept_line = (f"\nReserve this trade: Rp {swept:,.0f}\nTotal reserve: Rp {slot.profit_reserve:,.0f}" if swept > 0 else "")
    halt_warning = (f"\nSLOT HALTED: balance Rp {slot.balance:,.0f} -- 40% loss reached." if slot.halted else "")
    notify(
        f"[PAK OGAH v2 slot {slot.slot_id}] EXIT -- {coin.upper()}\n"
        f"Reason: {reason}\n"
        f"Trade P&L: Rp {trade_pnl:+,.0f} ({trade_pct:+.2f}%)\n"
        f"Score at exit: {current_score}/70\n"
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
    price = candidate["current_price"]
    score = candidate["score"]
    coin_l = coin.lower()
    cooldown_expires = slot.loss_cooldown.get(coin_l, 0)
    if time.time() < cooldown_expires:
        hours_left = (cooldown_expires - time.time()) / 3600
        print(f"[SKIP] {coin}: cooldown active ({hours_left:.1f}h remaining)", flush=True)
        return
    if price <= 0 or slot.balance <= 0:
        return
    investable = slot.balance * (1.0 - 0.000111)
    qty = investable / price
    slot.deployed_capital = slot.balance
    slot.coin = coin_l
    slot.entry_price = price
    slot.peak_price = price
    slot.qty_coin = qty
    slot.balance = 0.0
    slot.entry_ts = int(time.time())
    slot.breakeven_active = False
    slot.trail_active = False
    slot.phase = "risk"
    slot.scans_since_entry = 0
    save_slot(slot, path)
    gain_24h = (price - candidate["price_24h"]) / candidate["price_24h"]
    gain_7d  = (price - candidate["price_7d"])  / candidate["price_7d"]
    scores_str = " | ".join(k+":"+str(v) for k, v in candidate["scores"].items())
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    notify(
        f"[PAK OGAH v2 slot {slot.slot_id}] ENTRY [PAPER TRADE] {now_utc}\n"
        f"Coin: {coin.upper()}\n"
        f"Score: {score}/70\n"
        f"24h: {gain_24h:+.1%} | 7d: {gain_7d:+.1%}\n"
        f"Entry: Rp {price:,.0f} | Vol: Rp {candidate['vol_idr']/1e6:.0f}M\n"
        f"Qty: {qty:.4f} {coin.upper()}\n"
        f"Capital: Rp {slot.deployed_capital:,.0f} | Reserve: Rp {slot.profit_reserve:,.0f}\n"
        f"Phase: Risk \U0001f6a8 | Hard stop: 5% | Early failure: 3% (first 5 scans)\n"
        f"Proven at +3% | Breakeven at +4% | Trend-Riding at +8%\n"
        f"Trail -7% at +10% | Emergency floor -12% from peak\n"
        f"Scores: {scores_str}"
    )

def main():
    print(f"[PAK OGAH v2] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
    slots = [load_slot(path, i+1) for i, path in enumerate(SLOT_STATE_PATHS)]
    if all(s.halted or s.is_halted_by_loss for s in slots):
        notify("[PAK OGAH v2] Both slots halted -- no action.")
        return 0
    try:
        summaries = fetch_summaries()
    except Exception as e:
        notify(f"[PAK OGAH v2] Could not fetch market data: {e}. Skipping.")
        return 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.halted or slot.is_halted_by_loss or not slot.is_occupied:
            continue
        current_price = fetch_current_price(slot.coin)
        if current_price <= 0:
            notify(f"[PAK OGAH v2 slot {slot.slot_id}] Price fetch failed for {slot.coin.upper()}.")
            continue
        tickers = summaries.get("tickers", {})
        prices_24h = summaries.get("prices_24h", {})
        prices_7d = summaries.get("prices_7d", {})
        ticker = tickers.get(f"{slot.coin}_idr", {})
        pair_key = f"{slot.coin}idr"
        p24h = prices_24h.get(pair_key, 0)
        p7d  = prices_7d.get(pair_key, 0)
        current_score, _ = compute_score(ticker, p24h, p7d) if ticker else (0, {})
        equity = slot.qty_coin * current_price
        deployed = slot.deployed_capital if slot.deployed_capital > 0 else slot.initial_capital
        trade_pct = (equity - deployed) / deployed * 100 if deployed > 0 else 0.0
        exited = handle_exit(slot, current_price, current_score, path)
        if not exited:
            phase_str = PHASE_LABELS.get(slot.phase, slot.phase)
            notify_throttled(slot,
                f"[PAK OGAH v2 slot {slot.slot_id}] Check-in\n"
                f"Coin: {slot.coin.upper()}\n"
                f"Price: Rp {current_price:,.0f}\n"
                f"Score: {current_score}/70\n"
                f"Phase: {phase_str} | Scan #{slot.scans_since_entry}\n"
                f"Trade: Rp {equity:,.0f} ({trade_pct:+.2f}%) | Peak: Rp {slot.peak_price:,.0f}\n"
                f"Total reserve: Rp {slot.profit_reserve:,.0f}\n"
                f"Action: holding")
            save_slot(slot, path)
    candidates = scan_and_score(summaries, already_held={s.coin for s in slots if s.coin}, slots=slots)
    cand_idx = 0
    for slot, path in zip(slots, SLOT_STATE_PATHS):
        if slot.halted or slot.is_halted_by_loss or slot.is_occupied:
            continue
        if cand_idx >= len(candidates):
            scan_summary = getattr(slot, "_scan_summary", "")
            notify_throttled(slot,
                f"[PAK OGAH v2 slot {slot.slot_id}] Check-in\n"
                f"Status: empty\n"
                f"Balance: Rp {slot.balance:,.0f} | Reserve: Rp {slot.profit_reserve:,.0f}\n"
                f"{scan_summary}")
            save_slot(slot, path)
            continue
        entered = False
        while cand_idx < len(candidates) and not entered:
            candidate = candidates[cand_idx]
            cand_idx += 1
            fresh = load_slot(path, slot.slot_id)
            if fresh.is_occupied:
                print(f"[SKIP] slot {slot.slot_id}: already occupied by concurrent run", flush=True)
                break
            handle_entry(slot, candidate, path)
            entered = slot.is_occupied
    return 0

if __name__ == "__main__":
    sys.exit(main())
