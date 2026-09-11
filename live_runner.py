"""
The live job — the thing GitHub Actions runs every 15 minutes.

Note what it imports: the SAME `decide` the backtest calls. That is the entire
point of the architecture. If you ever find yourself re-implementing strategy
logic in this file, stop: put it in strategy.py so the replay tests it too.

STATUS: real exchange execution is stubbed. The PAPER simulation itself
always runs regardless (see the action loop below) -- EXECUTE only controls
whether a REAL order is ALSO attempted on top of that.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List

from config import Config
from engine import HaltSignal, apply_fill, check_drawdown, equity, verify_balance
from strategy import Action, Lot, decide, new_state

STATE_PATH = os.environ.get("STATE_PATH", "state.json")
EXECUTE = os.environ.get("EXECUTE", "false").lower() == "true"
# How often a routine "nothing happened" check-in is actually sent to
# Telegram, per coin -- real trades, halts, and profit-protection are
# NEVER throttled, only these routine updates. Default: 2 hours.
ROUTINE_NOTIFY_THROTTLE_SECONDS = int(os.environ.get("ROUTINE_NOTIFY_THROTTLE_SECONDS", 2 * 3600))


# ---------------------------------------------------------------- state

def load_state(cfg: Config) -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return new_state(cfg)
    with open(STATE_PATH) as f:
        raw = json.load(f)
    raw["lots"] = [Lot(**l) for l in raw.get("lots", [])]
    return raw


def save_state(state: Dict[str, Any]) -> None:
    out = dict(state)
    out["lots"] = [vars(l) for l in state["lots"]]
    out.pop("_pending_anchor", None)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- exchange

def fetch_latest_candles(cfg: Config) -> List[Dict[str, Any]]:
    from indodax_data import fetch_paged
    import strategy as active_strategy_module

    strategy_family = getattr(active_strategy_module, "STRATEGY_FAMILY", "NATIVE_CRYPTO")

    if strategy_family == "WRAPPER":
        interval = "60"
        days_needed = 14
        candles = fetch_paged(f"{cfg.coin.upper()}{cfg.quote}", interval, days=days_needed)
        if cfg.orb_session_start_hour is None and candles:
            from asset_type_detector import detect_session_start_hour
            detected = detect_session_start_hour(candles)
            if detected is not None:
                cfg.orb_session_start_hour = detected
                print(f"[INFO] auto-detected session start hour: {detected}:00 UTC", flush=True)
        return candles

    interval = "15"
    bars_per_day = 96
    days_needed = max(2, -(-cfg.history_window // bars_per_day) + 1)
    return fetch_paged(f"{cfg.coin.upper()}{cfg.quote}", interval, days=days_needed)


def fetch_real_balance(cfg: Config) -> Dict[str, float]:
    raise NotImplementedError("Indodax authenticated balance call not implemented yet")


def place_order(action: Action, cfg: Config) -> Dict[str, Any]:
    raise NotImplementedError("Indodax authenticated order call not implemented yet")


def _format_check_report(coin_label: str, state: Dict[str, Any], candle: Dict[str, Any],
                          cfg: Config, regime_note: str, action_line: str = None) -> str:
    price = candle["close"]
    principal = state.get("principal", cfg.starting_idr)
    current_equity = equity(state, price)
    pct = ((current_equity - principal) / principal * 100) if principal > 0 else 0.0
    has_position = bool(state.get("lots"))

    regime_line = ""
    if regime_note:
        label = regime_note.split("regime:")[-1].split("(")[0].strip()
        regime_line = f"Regime: {label}\n"

    lines = [
        f"[{coin_label}] Check-in",
        f"Price: Rp {price:,.0f}",
        regime_line.rstrip(),
        f"Equity: Rp {current_equity:,.0f} ({pct:+.2f}%)",
        f"Position: {'open' if has_position else 'in cash'}",
        f"Action: {action_line or 'none -- waiting for a real signal'}",
    ]
    return "\n".join(l for l in lines if l)


def notify_throttled(state: Dict[str, Any], message: str, throttle_seconds: int) -> None:
    """
    For routine "nothing happened" check-ins ONLY. Real trades, halts,
    and profit-protection should NEVER go through this.
    """
    print(f"[NOTIFY, throttled] {message}", flush=True)
    now = time.time()
    last = state.get("last_routine_notify_ts", 0)
    if now - last < throttle_seconds:
        return
    state["last_routine_notify_ts"] = now
    notify(message)


def notify(message: str) -> None:
    print(f"[NOTIFY] {message}", flush=True)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        import urllib.request
        import urllib.parse
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

    hook = os.environ.get("NOTIFY_WEBHOOK")
    if hook:
        import urllib.request
        req = urllib.request.Request(
            hook,
            data=json.dumps({"text": message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[NOTIFY FAILED] {e}", flush=True)


# ---------------------------------------------------------------- main

def main() -> int:
    cfg = Config()

    if os.environ.get("COIN"):
        cfg.coin = os.environ["COIN"].lower()
    if os.environ.get("STARTING_IDR"):
        cfg.starting_idr = float(os.environ["STARTING_IDR"])
    if os.environ.get("GAP_MODE"):
        cfg.gap_mode = os.environ["GAP_MODE"]

    coin_label = cfg.coin.upper()

    problems = cfg.validate()
    if problems:
        notify(f"[{coin_label}] Config rejected: " + "; ".join(problems))
        return 1

    state = load_state(cfg)

    candles = fetch_latest_candles(cfg)
    if not candles:
        notify(f"[{coin_label}] No candles returned; skipping this cycle.")
        return 0

    candle = candles[-1]
    state["closes"] = [c["close"] for c in candles[-cfg.history_window:]]

    regime_note = ""
    strategy_family = getattr(__import__("strategy"), "STRATEGY_FAMILY", None)
    if strategy_family == "NATIVE_CRYPTO":
        try:
            from regime_manager import apply_persistence, classify_all
            raw = classify_all(candles)
            confirmed = apply_persistence(raw)
            latest = confirmed[-1]
            regime_note = (f" | regime: {latest['confirmed']} "
                           f"(persistence key: {latest['raw'].persistence_key})")
        except Exception as e:
            regime_note = f" | regime: unavailable ({e})"
    state["bars_seen"] = max(state["bars_seen"] + 1, len(state["closes"]))

    halt_msg = check_drawdown(state, candle["close"], cfg)
    if halt_msg:
        notify(f"[{coin_label}] HALT: {halt_msg}. New trades stopped. "
               f"Existing positions untouched. Your call what happens next.")
        save_state(state)
        return 0

    if EXECUTE:
        try:
            real = fetch_real_balance(cfg)
            drift = abs(real.get("idr", 0) - state["idr"])
            if drift > max(1000.0, state["idr"] * 0.01):
                notify(f"[{coin_label}] PAUSED: balance mismatch. Local {state['idr']:,.0f} IDR "
                       f"vs exchange {real.get('idr', 0):,.0f}. Waiting for you.")
                state["halted"] = True
                save_state(state)
                return 0
        except NotImplementedError:
            notify(f"[{coin_label}] PAUSED: authenticated Indodax API not yet wired up. "
                   "Refusing to trade blind.")
            return 1

    err = verify_balance(state)
    if err:
        notify(f"[{coin_label}] PAUSED: internal state inconsistent ({err}). Waiting for you.")
        state["halted"] = True
        save_state(state)
        return 0

    reserve_before = state.get("profit_reserve", 0.0)
    actions = decide(state, candle, cfg)

    if state.get("profit_reserve", 0.0) > reserve_before:
        swept = state["profit_reserve"] - reserve_before
        notify(f"[{coin_label}] PROFIT PROTECTED: {swept:,.0f} IDR moved to reserve "
               f"(total reserve now {state['profit_reserve']:,.0f} IDR). "
               f"This money will not be risked in future trades. "
               f"Withdraw it to your bank whenever you'd like it fully off the exchange.")

    if not actions:
        notify_throttled(state, _format_check_report(coin_label, state, candle, cfg, regime_note),
                          throttle_seconds=ROUTINE_NOTIFY_THROTTLE_SECONDS)
        if state.get("_pending_anchor") is not None:
            state["anchor"] = state["_pending_anchor"]
        save_state(state)
        return 0

    fills = []
    for action in actions:
        line = (f"{action.side.upper()} {action.tag} "
                f"{action.qty_idr or action.qty_coin} — {action.reason}{regime_note}")

        apply_fill(state, action, candle, cfg, fills)
        err = verify_balance(state)
        if err:
            notify(f"[{coin_label}] PAUSED: internal state inconsistent after a fill "
                   f"({err}). Waiting for you.")
            state["halted"] = True
            save_state(state)
            return 0

        if not EXECUTE:
            notify(_format_check_report(coin_label, state, candle, cfg, regime_note,
                                          action_line=f"PAPER TRADE -- {line}"))
            continue
        try:
            place_order(action, cfg)
            notify(f"[{coin_label}] filled (real order): {line}")
        except NotImplementedError:
            notify(f"[{coin_label}] [PAPER TRADE, NO REAL API YET] {line}")
        except Exception as e:
            notify(f"[{coin_label}] PAUSED: real order failed ({e}), but the paper "
                   f"simulation already recorded this fill. Waiting for you.")
            state["halted"] = True
            break

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
