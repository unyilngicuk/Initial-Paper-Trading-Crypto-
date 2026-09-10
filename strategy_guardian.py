"""
UNYIL GUARDIAN -- conservative capital preservation.

A sibling to Unyil 2.0, not a replacement. Same interface (decide,
new_state, Action, Lot), same engine, same safety guarantees. Different
objective.

WHY THIS EXISTS
---------------
Unyil 2.0's crash protection is a SIDE EFFECT, not a design goal: in the
Sep 2025 - Mar 2026 crash it lost 16.75% while the market lost 37.30%,
but only because a trend-following rule happens to sit in cash when the
trend is down. Nothing in it is actually trying to preserve capital.

Guardian makes preservation the explicit objective. It is designed to be
the strategy you run when you believe a decline is likely, or when you
simply care more about not losing than about keeping up.

WHAT IT DOES DIFFERENTLY
------------------------
  1. TWO TRENDS MUST AGREE. Enters only when price is above BOTH a
     medium (~14d) and a long (~50d) moving average, AND the medium is
     above the long. A single moving average crossing is noise often
     enough; requiring alignment filters most false starts. This is
     stricter than Unyil 2.0's single-trend test, so Guardian trades
     less and enters later -- deliberately.

  2. PARTIAL POSITIONS, NOT ALL-IN. Unyil 2.0 commits 100% of investable
     cash to one position. Guardian caps exposure (default 50%), so a
     bad entry can never put the whole balance at risk. This is the
     single biggest structural difference.

  3. TIGHTER STOP (8% vs 15%). Cuts losers faster. Costs some winners
     that would have recovered; that trade is the point.

  4. TRAILING STOP. Once a position gains, the stop follows price up
     and never moves back down. Converts an open gain into a floor,
     rather than risking the whole move round-trip.

  5. FASTER EXIT THAN ENTRY. Exits when price falls below the MEDIUM
     average (quick), but requires both averages aligned to enter
     (slow). Deliberately asymmetric: leaving should be easier than
     arriving when the goal is preservation.

  6. CRASH LOCKOUT. After exiting into a confirmed downtrend (medium
     below long), refuses new entries until that condition clears. This
     is what keeps it out of a falling market instead of repeatedly
     buying dips on the way down.

WHAT IT COSTS YOU
-----------------
Guardian will underperform Unyil 2.0, and underperform buy-and-hold, in
rising markets -- by more than Unyil 2.0 does. Partial sizing alone caps
upside at roughly half. That is not a flaw to be tuned away; it is the
price of the objective. Anyone comparing the two should compare them on
DRAWDOWN first and return second.

SAFETY -- unchanged from Unyil 2.0, all of it
---------------------------------------------
  * decide() is pure: reads state and a candle, returns intended actions,
    never moves money. The engine alone executes.
  * Exits are ALWAYS evaluated, including while halted. A halt stops new
    risk; it never traps a position past its own stop.
  * Spot only. No leverage, no borrowing, losses capped at deposit.
  * Periodic profit protection: profit above principal is swept into a
    reserve that future entries never touch.
  * Entry sizing always excludes that reserve.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import Config
from indicators import sma


@dataclass
class Action:
    side: str                     # "buy" | "sell"
    qty_idr: Optional[float] = None
    qty_coin: Optional[float] = None
    reason: str = ""
    lot_id: Optional[int] = None
    level_idx: Optional[int] = None
    tag: str = "guardian"


@dataclass
class Lot:
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        # Not used for exit logic (trailing stop and trend exit decide that).
        # Present only for engine compatibility.
        return self.entry_price * (1.0 + cfg.guardian_stop_pct)


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
        "strategy_name": "guardian",
        # -- periodic profit protection (same as Unyil 2.0) --
        "principal": cfg.starting_idr,
        "profit_reserve": 0.0,
        "period_start_ts": None,
        # -- Guardian specific --
        "high_water_price": None,   # highest price seen since entry, for the trailing stop
        "crash_lockout": False,     # set on exiting into a confirmed downtrend
        "cooldown_until_ts": None,  # set after a losing exit; blocks new entries
    }


def _trends(state: Dict[str, Any], cfg: Config):
    """Medium and long moving averages. Either may be None during warmup."""
    med = sma(state["closes"], cfg.guardian_trend_fast)
    lng = sma(state["closes"], cfg.guardian_trend_slow)
    return med, lng


def _record_trade_outcome(state: Dict[str, Any], cfg: Config, entry_price: float,
                           exit_price: float, ts: int) -> None:
    pnl_pct = (exit_price - entry_price) / entry_price
    won = pnl_pct > 0
    state["trade_history"].append({"ts": ts, "pnl_pct": pnl_pct, "won": won})
    state["trade_history"] = state["trade_history"][-20:]
    # Re-entry cooldown after a LOSING exit only. A winning exit means the
    # trend was real, so there's no reason to sit out a fresh signal.
    if not won and cfg.reentry_cooldown_hours > 0:
        state["cooldown_until_ts"] = ts + int(cfg.reentry_cooldown_hours * 3600)


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    """
    Precedence, mirroring Unyil 2.0's safety philosophy:
      0. Periodic profit protection (a risk-reducing action).
      1. Exits -- always evaluated, even while halted.
      2. New entries -- blocked while halted, and while in crash lockout.
    """
    actions: List[Action] = []
    price = candle["close"]
    ts = candle["ts"]

    if state["period_start_ts"] is None:
        state["period_start_ts"] = ts

    if state["bars_seen"] < cfg.warmup_bars:
        return actions

    lot = state["lots"][0] if state["lots"] else None
    period_elapsed = (ts - state["period_start_ts"]) >= cfg.withdrawal_period_days * 86400

    # --- 0. periodic profit protection ---
    if period_elapsed:
        if lot is not None:
            _record_trade_outcome(state, cfg, lot.entry_price, price, ts)
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"period close @ {price:.0f} (banking profit)",
                           tag="guardian")]
        realized = state["idr"] - state["principal"] - state["profit_reserve"]
        if realized > 0:
            state["profit_reserve"] += realized
        state["period_start_ts"] = ts

    med, lng = _trends(state, cfg)
    if med is None or lng is None:
        return actions  # not enough history for both averages yet

    downtrend_confirmed = med < lng

    # --- 1. exits (always evaluated, even while halted) ---
    if lot is not None:
        # Track the high-water mark for the trailing stop.
        if state["high_water_price"] is None or price > state["high_water_price"]:
            state["high_water_price"] = price

        hard_stop = lot.entry_price * (1.0 - cfg.guardian_stop_pct)
        trail_stop = state["high_water_price"] * (1.0 - cfg.guardian_trail_pct)
        # The trailing stop only ever tightens -- never below the hard stop.
        effective_stop = max(hard_stop, trail_stop)

        reason = None
        if price <= effective_stop:
            reason = ("trailing stop" if trail_stop > hard_stop else "stop loss")
        elif price < med * (1.0 - cfg.guardian_exit_buffer_pct):
            reason = "fell below medium trend"

        if reason:
            _record_trade_outcome(state, cfg, lot.entry_price, price, ts)
            if downtrend_confirmed:
                state["crash_lockout"] = True
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"{reason} @ {price:.0f} (entry {lot.entry_price:.0f})",
                           tag="guardian")]

    # --- 2. new entries ---
    if lot is not None or state["halted"]:
        return actions

    # Re-entry cooldown after a losing exit -- blocks NEW risk only.
    if state["cooldown_until_ts"] is not None and ts < state["cooldown_until_ts"]:
        return actions

    # Crash lockout clears only when the downtrend itself clears.
    if state["crash_lockout"]:
        if downtrend_confirmed:
            return actions
        state["crash_lockout"] = False

    # Both trends must agree, and price must clear both.
    aligned = (med > lng) and (price > med) and (price > lng)
    if not aligned:
        return actions

    investable = state["idr"] - state["profit_reserve"]
    spend = investable * cfg.guardian_max_exposure
    if spend <= 0:
        return actions

    actions.append(Action(
        side="buy",
        qty_idr=spend,
        level_idx=0,
        reason=(f"guardian entry @ {price:.0f} (med {med:.0f} > long {lng:.0f}, "
                f"exposure {cfg.guardian_max_exposure:.0%}, "
                f"reserve {state['profit_reserve']:,.0f})"),
        tag="guardian",
    ))
    return actions
