"""
The live job — the thing GitHub Actions runs every 15 minutes.

Note what it imports: the SAME `decide` the backtest calls. That is the entire
point of the architecture. If you ever find yourself re-implementing strategy
logic in this file, stop: put it in strategy.py so the replay tests it too.

STATUS: execution is stubbed. Indodax does publish a documented, authenticated
trading API (order placement and balance queries) — but the calls below have
not been implemented against it yet. Until fetch_real_balance() and
place_order() are filled in with real, signed Indodax API calls, run this
with EXECUTE=false and it behaves as a signal notifier: it tells you what it
would have done, without touching your account.
"""

import json
import os
import sys
from typing import Any, Dict, List

from config import Config
from engine import HaltSignal, check_drawdown, equity, verify_balance
from strategy import Action, Lot, decide, new_state

STATE_PATH = os.environ.get("STATE_PATH", "state.json")
EXECUTE = os.environ.get("EXECUTE", "false").lower() == "true"


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
    os.replace(tmp, STATE_PATH)   # atomic: a crash mid-write cannot corrupt state


# ---------------------------------------------------------------- exchange

def fetch_latest_candles(cfg: Config) -> List[Dict[str, Any]]:
    """
    Fetches enough real history to cover the active strategy's own
    lookback, at the RIGHT interval for its family -- 15-minute for
    NATIVE_CRYPTO (Unyil 2.0, Guardian, Usro), 60-minute for WRAPPER
    (ORB, GAP), matching exactly what backtesting validated each on.
    Using the wrong interval here would silently feed a strategy data
    it was never tested against.
    """
    from indodax_data import fetch_paged
    import strategy as active_strategy_module

    strategy_family = getattr(active_strategy_module, "STRATEGY_FAMILY", "NATIVE_CRYPTO")

    if strategy_family == "WRAPPER":
        interval = "60"
        bars_per_day = 24
        # WRAPPER strategies need enough real history for the asset-type
        # detector's own session-hour auto-detection (>= 1 week), not
        # just cfg.history_window (which isn't meaningfully used by GAP).
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
    """TODO: authenticated Indodax balance call (getInfo / trade API)."""
    raise NotImplementedError("Indodax authenticated balance call not implemented yet")


def place_order(action: Action, cfg: Config) -> Dict[str, Any]:
    """TODO: authenticated Indodax order call (trade API)."""
    raise NotImplementedError("Indodax authenticated order call not implemented yet")


def notify(message: str) -> None:
    """
    Always logs to stdout (picked up by the GitHub Actions log regardless).
    Sends to Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both
    set -- Telegram's Bot API needs its own specific shape (bot token in
    the URL path, chat_id + text in the body), not a generic webhook POST,
    so this is a real Telegram-specific call, not the old NOTIFY_WEBHOOK
    guess. Also still supports NOTIFY_WEBHOOK for anything else (Slack,
    a custom endpoint) if that's set instead or in addition.
    """
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

    # Per-job overrides via environment variables, so one script serves
    # every matrix job (different coin, capital, and gap_mode per job)
    # without hardcoding any of it in Config's own defaults.
    if os.environ.get("COIN"):
        cfg.coin = os.environ["COIN"].lower()
    if os.environ.get("STARTING_IDR"):
        cfg.starting_idr = float(os.environ["STARTING_IDR"])
    if os.environ.get("GAP_MODE"):
        cfg.gap_mode = os.environ["GAP_MODE"]

    problems = cfg.validate()
    if problems:
        notify("Config rejected: " + "; ".join(problems))
        return 1

    state = load_state(cfg)

    candles = fetch_latest_candles(cfg)
    if not candles:
        notify("No candles returned; skipping this cycle.")
        return 0

    candle = candles[-1]
    state["closes"] = [c["close"] for c in candles[-cfg.history_window:]]

    # --- informational regime context, native crypto only, decisions
    # unaffected. Automated regime-switching was tested carefully (see
    # spec Section 11d) and found to underperform running one strategy
    # continuously -- so this is shown as CONTEXT alongside whatever the
    # active strategy actually decides, never used to change strategies
    # live. Wrapped in try/except: a regime-manager failure must never
    # block the real trading decision below.
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

    # --- account-wide risk gate ---
    halt_msg = check_drawdown(state, candle["close"], cfg)
    if halt_msg:
        notify(f"HALT: {halt_msg}. New trades stopped. "
               f"Existing positions untouched. Your call what happens next.")
        save_state(state)
        return 0

    # --- balance reconciliation ---
    if EXECUTE:
        try:
            real = fetch_real_balance(cfg)
            drift = abs(real.get("idr", 0) - state["idr"])
            if drift > max(1000.0, state["idr"] * 0.01):
                notify(f"PAUSED: balance mismatch. Local {state['idr']:,.0f} IDR "
                       f"vs exchange {real.get('idr', 0):,.0f}. Waiting for you.")
                state["halted"] = True
                save_state(state)
                return 0
        except NotImplementedError:
            notify("PAUSED: authenticated Indodax API not yet wired up. "
                   "Refusing to trade blind.")
            return 1

    err = verify_balance(state)
    if err:
        notify(f"PAUSED: internal state inconsistent ({err}). Waiting for you.")
        state["halted"] = True
        save_state(state)
        return 0

    # --- the decision, from the one shared function ---
    reserve_before = state.get("profit_reserve", 0.0)
    actions = decide(state, candle, cfg)

    if state.get("profit_reserve", 0.0) > reserve_before:
        swept = state["profit_reserve"] - reserve_before
        notify(f"PROFIT PROTECTED: {swept:,.0f} IDR moved to reserve "
               f"(total reserve now {state['profit_reserve']:,.0f} IDR). "
               f"This money will not be risked in future trades. "
               f"Withdraw it to your bank whenever you'd like it fully off the exchange.")

    if not actions:
        notify(f"NO ACTION at Rp {candle['close']:,.0f}.{regime_note} Waiting for a real signal.")
        if state.get("_pending_anchor") is not None:
            state["anchor"] = state["_pending_anchor"]
        save_state(state)
        return 0

    for action in actions:
        line = (f"{action.side.upper()} {action.tag} "
                f"{action.qty_idr or action.qty_coin} — {action.reason}{regime_note}")
        if not EXECUTE:
            notify(f"[DRY RUN] would {line}")
            continue
        try:
            place_order(action, cfg)
            notify(f"filled: {line}")
        except NotImplementedError:
            notify(f"[NO API] would {line}")
            break
        except Exception as e:
            notify(f"PAUSED: order failed ({e}). Waiting for you.")
            state["halted"] = True
            break

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
