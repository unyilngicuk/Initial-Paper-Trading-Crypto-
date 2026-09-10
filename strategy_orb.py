"""
UNYIL ORB -- Opening Range Breakout, WRAPPER family.

The first strategy purpose-built for the WRAPPER category (tokenized
stocks: confirmed on NVDAX, TSLAX, AAPLX). Every NATIVE_CRYPTO strategy
(Unyil 2.0, Guardian, Usro) showed real structural weakness on this
category -- all three underperformed buy-and-hold on all three tested
stocks, and all nine combinations tripped their circuit breaker. The
diagnosed mechanism: a tokenized wrapper trades 24/7, but real price
discovery only happens ~6.5h/day (the underlying market's real hours),
leaving 70-95% of all 15-minute bars completely flat.

ORB IS BUILT AROUND THAT PATTERN DIRECTLY, NOT AGAINST IT
-----------------------------------------------------------
This is a well-established professional equity-trading concept, not
something invented for this project: when real trading resumes after a
long quiet stretch, the first genuine price movement often sets the
direction for a meaningful chunk of that session. The strategy:
  1. Track the high of the first `orb_range_bars` bars of REAL activity
     each session (the "opening range").
  2. If price breaks decisively above that range, enter -- fast, full
     size, no multi-day trend confirmation (there usually isn't enough
     real trading history within a session for one anyway).
  3. Manage the position with a tight stop (wrong within a session,
     get out fast) and a trailing stop once it's working.

This is explicitly HIGH RISK, HIGH RETURN by design: no confirmation
delay, full exposure, a tight stop that will be hit often. That is the
trade-off being made on purpose, symmetric to how Usro accepts more
risk than Guardian in exchange for capturing more of a rally.

SESSION DETECTION IS ASSET-AGNOSTIC
------------------------------------
cfg.orb_session_start_hour must be set explicitly (e.g. via
asset_type_detector.detect_session_start_hour()) rather than assumed to
be NASDAQ hours -- a wrapper could track any underlying market. If it is
not set, this strategy will not trade at all, rather than silently
guessing wrong.

SAFETY -- unchanged from every other strategy in this project
-----------------------------------------------------------------
  * decide() is pure: reads state and a candle, returns intended
    actions, never moves money. The engine alone executes.
  * Exits are ALWAYS evaluated, including while halted.
  * Spot only. No leverage, no borrowing -- "high risk" here means fast,
    full-size, tight-stop entries, never anything that risks more than
    the capital actually deposited.
  * Periodic profit protection: profit above principal is swept into a
    reserve future entries never touch.
  * Shares the 20% shared circuit breaker default (not tightened or
    loosened without evidence, same principle as Usro).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config

STRATEGY_FAMILY = "WRAPPER"


@dataclass
class Action:
    side: str
    qty_idr: Optional[float] = None
    qty_coin: Optional[float] = None
    reason: str = ""
    lot_id: Optional[int] = None
    level_idx: Optional[int] = None
    tag: str = "orb"


@dataclass
class Lot:
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        return self.entry_price * (1.0 + cfg.orb_trail_pct)


def new_state(cfg: Config) -> Dict[str, Any]:
    return {
        "idr": cfg.starting_idr,
        "grid_coin": 0.0,
        "hold_coin": 0.0,
        "hold_bought": False,
        "closes": [],
        "lots": [],
        "next_lot_id": 1,
        "anchor": None,
        "peak_equity": cfg.starting_idr,
        "halted": False,
        "bars_seen": 0,
        "trade_history": [],
        "strategy_name": "orb",
        "principal": cfg.starting_idr,
        "profit_reserve": 0.0,
        "period_start_ts": None,
        "current_session_date": None,
        "bars_into_session": 0,
        "orb_high": None,
        "orb_low": None,
        "high_water_price": None,
        "breakout_taken_this_session": False,
    }


def _record_trade_outcome(state: Dict[str, Any], entry_price: float,
                           exit_price: float, ts: int) -> None:
    pnl_pct = (exit_price - entry_price) / entry_price
    state["trade_history"].append({"ts": ts, "pnl_pct": pnl_pct, "won": pnl_pct > 0})
    state["trade_history"] = state["trade_history"][-20:]


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    """
    Precedence:
      0. Periodic profit protection.
      1. Session bookkeeping: detect a new session, track the opening
         range during its first `orb_range_bars` bars.
      2. Exits -- always evaluated, even while halted.
      3. New entries -- only after the opening range is set, only once
         per session, blocked while halted.
    """
    actions: List[Action] = []
    price = candle["close"]
    ts = candle["ts"]

    if cfg.orb_session_start_hour is None:
        return actions  # not configured for this asset -- refuse to guess

    if state["period_start_ts"] is None:
        state["period_start_ts"] = ts

    state["bars_seen"] += 1
    if state["bars_seen"] < cfg.warmup_bars:
        return actions

    lot = state["lots"][0] if state["lots"] else None
    period_elapsed = (ts - state["period_start_ts"]) >= cfg.withdrawal_period_days * 86400

    if period_elapsed:
        if lot is not None:
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"period close @ {price:.0f} (banking profit)", tag="orb")]
        realized = state["idr"] - state["principal"] - state["profit_reserve"]
        if realized > 0:
            state["profit_reserve"] += realized
        state["period_start_ts"] = ts

    # --- 1. session bookkeeping ---
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    today = (dt.year, dt.month, dt.day)
    is_session_open_bar = (dt.hour == cfg.orb_session_start_hour)

    if is_session_open_bar and state["current_session_date"] != today:
        state["current_session_date"] = today
        state["bars_into_session"] = 0
        state["orb_high"] = None
        state["orb_low"] = None
        state["breakout_taken_this_session"] = False

    if state["current_session_date"] == today and state["bars_into_session"] < cfg.orb_range_bars:
        state["orb_high"] = price if state["orb_high"] is None else max(state["orb_high"], price)
        state["orb_low"] = price if state["orb_low"] is None else min(state["orb_low"], price)
        state["bars_into_session"] += 1

    # --- 2. exits (always evaluated, even while halted) ---
    if lot is not None:
        if state["high_water_price"] is None or price > state["high_water_price"]:
            state["high_water_price"] = price
        hard_stop = lot.entry_price * (1.0 - cfg.orb_stop_pct)
        trail_stop = state["high_water_price"] * (1.0 - cfg.orb_trail_pct)
        stop_price = max(hard_stop, trail_stop)

        if price <= stop_price:
            reason = "trailing stop" if trail_stop > hard_stop else "stop loss"
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"{reason} @ {price:.0f} (entry {lot.entry_price:.0f})",
                           tag="orb")]
        return actions

    # --- 3. new entries ---
    if state["halted"]:
        return actions
    if state["orb_high"] is None or state["bars_into_session"] < cfg.orb_range_bars:
        return actions
    if state["breakout_taken_this_session"]:
        return actions

    breakout_price = state["orb_high"] * (1.0 + cfg.orb_breakout_buffer_pct)
    if price <= breakout_price:
        return actions

    investable = state["idr"] - state["profit_reserve"]
    spend = investable * cfg.orb_max_exposure
    if spend <= 0:
        return actions

    state["breakout_taken_this_session"] = True
    actions.append(Action(
        side="buy",
        qty_idr=spend,
        level_idx=0,
        reason=(f"ORB breakout @ {price:.0f} (range high {state['orb_high']:.0f}, "
                f"exposure {cfg.orb_max_exposure:.0%}, reserve {state['profit_reserve']:,.0f})"),
        tag="orb",
    ))
    return actions
