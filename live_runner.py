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
    from indodax_data import fetch_paged
    # Fetch enough days to cover the configured history window, plus a
    # small buffer -- a strategy with a long trend lookback (like Unyil 2.0)
    # needs far more than the ~2 days the original RSI+grid version required.
    bars_per_day = 96  # 15-minute bars
    days_needed = max(2, -(-cfg.history_window // bars_per_day) + 1)  # ceil + 1 day buffer
    return fetch_paged(f"{cfg.coin.upper()}{cfg.quote}", "15", days=days_needed)


def fetch_real_balance(cfg: Config) -> Dict[str, float]:
    """TODO: authenticated Indodax balance call (getInfo / trade API)."""
    raise NotImplementedError("Indodax authenticated balance call not implemented yet")


def place_order(action: Action, cfg: Config) -> Dict[str, Any]:
    """TODO: authenticated Indodax order call (trade API)."""
    raise NotImplementedError("Indodax authenticated order call not implemented yet")


def notify(message: str) -> None:
    """Telegram / email / whatever. Stdout is picked up by the Actions log."""
    print(f"[NOTIFY] {message}", flush=True)
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
        notify(f"NO ACTION at Rp {candle['close']:,.0f}. Waiting for a real signal.")
        if state.get("_pending_anchor") is not None:
            state["anchor"] = state["_pending_anchor"]
        save_state(state)
        return 0

    for action in actions:
        line = (f"{action.side.upper()} {action.tag} "
                f"{action.qty_idr or action.qty_coin} — {action.reason}")
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
