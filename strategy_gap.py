"""
UNYIL GAP -- Gap-and-Go / Gap-Fade, WRAPPER family.

A second, complementary strategy to ORB for the same category (tokenized
stocks: NVDAX, TSLAX, AAPLX). Where ORB reacts to the FIRST real move
within a session, GAP reacts to the GAP ITSELF -- the jump between the
last quoted price before the session opened and the first real price
once it does.

SPOT-ONLY REFRAMING, stated explicitly rather than left implicit
-------------------------------------------------------------------
The classic professional pair "gap-and-go vs. gap-fade" normally
includes a short side (fade an up-gap by shorting it, or short a
continuing down-gap). This system has no shorting -- spot only, a hard
boundary from the start of this project. Each mode below only trades
the ONE gap direction it can express as a long position:

  MODE "go":   an UP gap -- buy it, betting the move continues.
  MODE "fade": a DOWN gap -- buy it, betting the move reverses (the gap
               "fills" back toward the pre-session price).

A down-gap continuation and an up-gap fade would both require shorting
and are simply not tradeable here. Not an oversight -- this is what
"spot only" actually means once applied concretely to this pattern.

MECHANICS
---------
  1. Track the close of the bar immediately before the session opens.
  2. At the session-open bar, compute the gap vs. that pre-session close.
  3. If the gap clears cfg.gap_min_pct in the direction this mode
     trades, enter -- full size, no confirmation delay.
  4. Manage with a tight stop and a trailing stop, one attempt per
     session.

SAFETY -- unchanged from every other strategy in this project.
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
    tag: str = "gap"


@dataclass
class Lot:
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        # Not used for exit logic (the trailing stop decides that). Present
        # only for engine compatibility -- same pattern as every sibling
        # strategy's Lot class.
        return self.entry_price * (1.0 + cfg.gap_trail_pct)


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
        "strategy_name": "gap",
        "principal": cfg.starting_idr,
        "profit_reserve": 0.0,
        "period_start_ts": None,
        "current_session_date": None,
        "prev_bar_close": None,
        "gap_evaluated_this_session": False,
        "high_water_price": None,
        "ema": None,   # optional trend filter for gap_mode="fade" -- None until warmed up
    }


def _record_trade_outcome(state: Dict[str, Any], entry_price: float,
                           exit_price: float, ts: int) -> None:
    pnl_pct = (exit_price - entry_price) / entry_price
    state["trade_history"].append({"ts": ts, "pnl_pct": pnl_pct, "won": pnl_pct > 0})
    state["trade_history"] = state["trade_history"][-20:]


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    actions: List[Action] = []
    price = candle["close"]
    ts = candle["ts"]

    if cfg.orb_session_start_hour is None:
        return actions

    if state["period_start_ts"] is None:
        state["period_start_ts"] = ts

    state["bars_seen"] += 1
    if state["bars_seen"] < cfg.warmup_bars:
        state["prev_bar_close"] = price
        return actions

    if cfg.gap_fade_ema_enabled:
        k = 2.0 / (cfg.gap_fade_ema_period + 1)
        state["ema"] = price if state["ema"] is None else price * k + state["ema"] * (1 - k)

    lot = state["lots"][0] if state["lots"] else None
    period_elapsed = (ts - state["period_start_ts"]) >= cfg.withdrawal_period_days * 86400

    if period_elapsed:
        if lot is not None:
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            state["prev_bar_close"] = price
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"period close @ {price:.0f} (banking profit)", tag="gap")]
        realized = state["idr"] - state["principal"] - state["profit_reserve"]
        if realized > 0:
            state["profit_reserve"] += realized
        state["period_start_ts"] = ts

    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    today = (dt.year, dt.month, dt.day)
    is_session_open_bar = (dt.hour == cfg.orb_session_start_hour)

    gap_pct = None
    if is_session_open_bar and state["current_session_date"] != today:
        state["current_session_date"] = today
        state["gap_evaluated_this_session"] = False
        if state["prev_bar_close"]:
            gap_pct = (price - state["prev_bar_close"]) / state["prev_bar_close"]

    if lot is not None:
        if state["high_water_price"] is None or price > state["high_water_price"]:
            state["high_water_price"] = price
        hard_stop = lot.entry_price * (1.0 - cfg.gap_stop_pct)
        trail_stop = state["high_water_price"] * (1.0 - cfg.gap_trail_pct)
        stop_price = max(hard_stop, trail_stop)

        if price <= stop_price:
            reason = "trailing stop" if trail_stop > hard_stop else "stop loss"
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            state["prev_bar_close"] = price
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"{reason} @ {price:.0f} (entry {lot.entry_price:.0f})",
                           tag="gap")]
        state["prev_bar_close"] = price
        return actions

    if state["halted"]:
        state["prev_bar_close"] = price
        return actions
    if gap_pct is None or state["gap_evaluated_this_session"]:
        state["prev_bar_close"] = price
        return actions

    state["gap_evaluated_this_session"] = True

    should_enter = False
    gap_desc = ""
    if cfg.gap_mode == "go" and gap_pct >= cfg.gap_min_pct:
        should_enter = True
        gap_desc = f"up-gap {gap_pct:+.2%}, continuation bet"
    elif cfg.gap_mode == "fade" and gap_pct <= -cfg.gap_min_pct:
        should_enter = True
        gap_desc = f"down-gap {gap_pct:+.2%}, reversal bet"
        if cfg.gap_fade_ema_enabled and state["ema"] is not None:
            ema_floor = state["ema"] * (1.0 - cfg.gap_fade_ema_max_below_pct)
            if price < ema_floor:
                should_enter = False  # too far below trend -- looks like a real
                                       # decline, not an overreaction to fade

    if not should_enter:
        state["prev_bar_close"] = price
        return actions

    investable = state["idr"] - state["profit_reserve"]
    spend = investable * cfg.gap_max_exposure
    if spend <= 0:
        state["prev_bar_close"] = price
        return actions

    ema_note = ""
    if cfg.gap_fade_ema_enabled and state["ema"] is not None:
        ema_note = f", ema {state['ema']:.0f}"

    actions.append(Action(
        side="buy",
        qty_idr=spend,
        level_idx=0,
        reason=(f"GAP {cfg.gap_mode} @ {price:.0f} ({gap_desc}, "
                f"exposure {cfg.gap_max_exposure:.0%}, reserve {state['profit_reserve']:,.0f}"
                f"{ema_note})"),
        tag="gap",
    ))
    state["prev_bar_close"] = price
    return actions
