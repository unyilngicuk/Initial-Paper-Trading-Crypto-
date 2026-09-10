"""
USRO -- bull-market specialist.

The counterpart to Guardian. Where Guardian is slow to enter and fast to
exit (because its job is avoiding decline), Usro is fast to enter and
SLOW to exit (because its job is staying in a rally through the normal
noise that would otherwise shake a position out early).

WHY THIS EXISTS
---------------
Every strategy tested so far -- including Unyil 2.0, the current general
default -- underperforms buy-and-hold in every rally quarter measured
(Q1 -9.43, Q2 -3.53, recent 3mo -11.58 vs. buy-and-hold). The common
cause, diagnosed directly from real trade logs: a trend-break exit fires
on ordinary pullbacks inside a genuine rally, and the position re-enters
later, at a worse average price, paying fees each time.

Usro removes that mechanism entirely. It has exactly one exit: a
wide trailing stop. There is no "price dipped below the trend line, sell"
rule to be triggered by noise.

WHAT IT DOES DIFFERENTLY FROM UNYIL 2.0
----------------------------------------
  1. SINGLE FAST TREND, MINIMAL BUFFER. One short (~5-day) moving average
     with a 0.1% buffer -- enters as early into a move as the trend
     filter can confirm, rather than waiting for a slower filter or a
     wider buffer to clear.

  2. FULL COMMITMENT, NO SIZE SHRINKING. Same lesson already learned from
     Unyil 2.0's adaptive-sizing experiment: this strategy's edge lives
     in win MAGNITUDE (a few big rallies), not win rate, so nothing here
     reduces the size of a fresh entry based on recent history.

  3. NO TREND-BREAK EXIT. This is the core design decision. The ONLY way
     out of a position is the trailing stop below. A normal 5-10% rally
     pullback that would trigger Unyil 2.0's trend-break exit is simply
     absorbed here -- the position stays open through it.

  4. ONE WIDE TRAILING STOP (default 15%), active from the moment of
     entry. Before any gain, it behaves as a hard stop. Once price rises,
     it follows the high-water mark up and never loosens. This is
     deliberately much wider than Unyil 2.0's 15% fixed-from-entry stop
     combined with its twitchy trend exit -- the point is to give a real
     rally enough room to breathe.

WHAT IT COSTS YOU
-----------------
This is explicitly NOT a defensive strategy. With no trend-break exit and
a wide stop, a genuine reversal costs significantly more before Momentum
reacts than it would with Unyil 2.0, let alone Guardian. It has no crash
lockout, no dual-trend confirmation, no partial sizing. Run this only
when you specifically believe a sustained rally is underway or likely --
symmetrically to how Guardian should only run when a decline is expected.

SAFETY -- unchanged from Unyil 2.0 and Guardian, all of it
------------------------------------------------------------
  * decide() is pure: reads state and a candle, returns intended actions,
    never moves money. The engine alone executes.
  * Exits are ALWAYS evaluated, including while halted.
  * Spot only. No leverage, no borrowing, losses capped at deposit.
  * Periodic profit protection: profit above principal is swept into a
    reserve future entries never touch.
  * Entry sizing always excludes that reserve.
  * Shared 20% circuit breaker (not tightened, unlike Guardian's 10% --
    no evidence yet that a different threshold suits this strategy;
    changing it without that evidence would be exactly the kind of
    unjustified tuning already rejected twice in this project).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import Config
from indicators import sma


@dataclass
class Action:
    side: str
    qty_idr: Optional[float] = None
    qty_coin: Optional[float] = None
    reason: str = ""
    lot_id: Optional[int] = None
    level_idx: Optional[int] = None
    tag: str = "usro"


@dataclass
class Lot:
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        # Not used for exit logic (the trailing stop decides that). Present
        # only for engine compatibility.
        return self.entry_price * (1.0 + cfg.usro_trail_pct)


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
        "strategy_name": "usro",
        # -- periodic profit protection (same as Unyil 2.0 / Guardian) --
        "principal": cfg.starting_idr,
        "profit_reserve": 0.0,
        "period_start_ts": None,
        # -- Momentum specific --
        "high_water_price": None,   # anchors the single trailing stop
    }


def _record_trade_outcome(state: Dict[str, Any], entry_price: float,
                           exit_price: float, ts: int) -> None:
    pnl_pct = (exit_price - entry_price) / entry_price
    state["trade_history"].append({"ts": ts, "pnl_pct": pnl_pct, "won": pnl_pct > 0})
    state["trade_history"] = state["trade_history"][-20:]


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    """
    Precedence:
      0. Periodic profit protection (risk-reducing, evaluated first).
      1. The one exit -- the trailing stop -- always evaluated, even
         while halted.
      2. New entries -- blocked while halted. No other gate.
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
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"period close @ {price:.0f} (banking profit)",
                           tag="usro")]
        realized = state["idr"] - state["principal"] - state["profit_reserve"]
        if realized > 0:
            state["profit_reserve"] += realized
        state["period_start_ts"] = ts

    trend = sma(state["closes"], cfg.usro_trend_period)
    if trend is None:
        return actions

    # --- 1. the one exit: the trailing stop (always evaluated) ---
    if lot is not None:
        if state["high_water_price"] is None or price > state["high_water_price"]:
            state["high_water_price"] = price
        stop_price = state["high_water_price"] * (1.0 - cfg.usro_trail_pct)

        if price <= stop_price:
            _record_trade_outcome(state, lot.entry_price, price, ts)
            state["high_water_price"] = None
            return [Action(side="sell", qty_coin=lot.qty_coin, lot_id=lot.lot_id,
                           reason=f"trailing stop @ {price:.0f} "
                                  f"(entry {lot.entry_price:.0f}, "
                                  f"peak {state['high_water_price'] if state['high_water_price'] else price:.0f})",
                           tag="usro")]
        return actions  # holding: no trend-break check, by design

    # --- 2. new entries (blocked only while halted) ---
    if state["halted"]:
        return actions

    entry_threshold = trend * (1.0 + cfg.usro_entry_buffer_pct)
    if price <= entry_threshold:
        return actions

    investable = state["idr"] - state["profit_reserve"]
    spend = investable * cfg.usro_max_exposure
    if spend <= 0:
        return actions

    actions.append(Action(
        side="buy",
        qty_idr=spend,
        level_idx=0,
        reason=(f"usro entry @ {price:.0f} (trend {trend:.0f}, "
                f"exposure {cfg.usro_max_exposure:.0%}, "
                f"reserve {state['profit_reserve']:,.0f})"),
        tag="usro",
    ))
    return actions
