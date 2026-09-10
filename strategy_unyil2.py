"""
UNYIL 2.0 -- trend-following with adaptive position sizing.

Why this exists: the original RSI + grid strategy (strategy.py) was tested
on 2 real years of BTC/IDR, in three cost presets, across eight parameter
configurations. All eight lost to simple buy-and-hold by 31-37 percentage
points (see FINDINGS.md). The gap was structural, not a tuning problem:
the grid caps winners at +1.5% and calls that a strategy, so it structurally
cannot capture a real trend, and its one apparent "win" (the 2025 crash
period) was underexposure -- sitting in cash -- not skill.

This is not a re-tuning of that idea. It is a different mechanic:

  1. TREND, NOT MEAN-REVERSION. Buy while price is above a long moving
     average (the trend is up). Sell when price falls convincingly below
     it (the trend has broken). There is no small take-profit that caps
     a winner -- the position rides the trend for as long as it lasts.

  2. FEWER TRADES. A ~28-day trend filter fires a handful of times across
     two years, not 85+ times. Every trade pays Indonesia's asymmetric fee
     (buy + sell + the 0.21% PPh sell tax); fewer trades means less of the
     strategy's return is donated to fees before it even has a view.

  3. ADAPTIVE SIZING -- the "learn from failure" mechanism. This is a
     deterministic, fully transparent rule, not machine learning: after a
     losing trade, the fraction of cash committed to the next entry is
     cut (risk_frac *= loss_reduction_factor, floored at min_risk_frac).
     After a winning trade, it recovers toward the base size. The effect:
     a strategy that has just been wrong trades smaller until it proves
     itself right again, rather than repeating the same mistake at full
     size. This is a real, well-established risk-management technique
     (position sizing conditioned on recent realised performance) -- it
     is not prediction, and it makes no claim to "understand" why a trade
     lost.

  4. A HARD STOP-LOSS independent of the trend filter. A ~28-day moving
     average reacts slowly. If price craters fast, the stop protects
     capital before the slow-moving trend line catches up.

  5. PERIODIC PROFIT PROTECTION. Every withdrawal_period_days (default
     183, ~6 months), any open position is closed and cash above the
     original principal is swept into a reserve future entries never
     touch. This exists because the bot's API key has no withdrawal
     permission (a deliberate security choice) -- so "taking profit
     periodically" can't mean an actual bank withdrawal from inside the
     strategy. It means the same thing in effect: money already made
     stops being risked. Tested on real 2024-2026 BTC/IDR data broken
     into sequential 6-month windows, this was both safer AND more
     profitable than letting everything compound continuously, because
     a later bad quarter could then only threaten the principal, not
     profit already banked from earlier quarters. The user can still
     manually withdraw the reserve to their bank any time.

Same purity contract as the original: decide() reads state and a candle,
returns intended actions, and never moves money itself. It IS allowed to
update its own bookkeeping (risk_frac, trade_history) -- exactly as the
original tracked `_pending_anchor` -- because that is strategy memory, not
wallet state. The engine alone executes.
"""

from dataclasses import dataclass, field
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
    level_idx: Optional[int] = None   # unused here, kept for engine compatibility
    tag: str = "trend"


@dataclass
class Lot:
    """The one open position, when there is one. Reuses the grid engine's
    Lot shape so engine.py needs zero changes."""
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        # Not used for exit logic here (trend/stop decide exits, not a fixed
        # take-profit target) -- present only because engine.py's fill_model
        # branch references it for grid-style intrabar fills. Unyil 2.0 always
        # uses close-based decisions, so this is never actually read.
        return self.entry_price * (1.0 + cfg.stop_loss_pct)


def new_state(cfg: Config) -> Dict[str, Any]:
    return {
        "idr": cfg.starting_idr,
        "grid_coin": 0.0,        # the single trend position's coin, if any
        "hold_coin": 0.0,        # unused in Unyil 2.0 -- no separate hold bucket
        "hold_bought": False,    # left False: Unyil 2.0's own decide() never reads
                                 # this flag, but verify_engine.py's generic
                                 # "buy and hold" sanity check does, and needs it
                                 # to start False to perform its scripted test buy
        "closes": [],
        "lots": [],              # 0 or 1 Lot
        "next_lot_id": 1,
        "anchor": None,          # unused, kept for engine/report compatibility
        "peak_equity": cfg.starting_idr,
        "halted": False,
        "bars_seen": 0,
        # -- Unyil 2.0 specific memory --
        "risk_frac": cfg.base_risk_frac,
        "trade_history": [],     # list of {"ts": int, "pnl_pct": float, "won": bool}
        "consecutive_losses": 0,     # only shrinks sizing after cfg.losses_before_shrink in a row
        "strategy_name": "unyil2",   # lets report.py print the right header
        # -- periodic profit protection --
        "principal": cfg.starting_idr,   # the baseline that stays invested
        "profit_reserve": 0.0,           # realized profit, never re-risked
        "period_start_ts": None,         # set on the first candle seen
        # optional external sizing hook, no-op (1.0) unless something drives it --
        # e.g. a regime-aware harness cutting new-entry size on a severe signal,
        # without touching this strategy's own entry/exit logic at all
        "regime_risk_multiplier": 1.0,
        # optional external stop-tightening hook, no-op (None -> use cfg.stop_loss_pct
        # normally) unless something drives it. Distinct from the multiplier above:
        # this affects an EXISTING open position's exit, not new-entry sizing --
        # deliberately, since a prior experiment found sizing new entries during
        # severe Bearish did nothing (the two conditions rarely co-occur). An open
        # position has no such conflict: it can be held regardless of what
        # triggered it, so tightening its stop during a severe signal is a
        # mechanism that can actually engage.
        "stop_pct_override": None,
        # -- bounded adaptive sizing (only used when enabled in config) --
        "adaptive_frac": 1.0,            # multiplier applied on top of risk_frac
        "adaptive_log": [],              # audit trail: every adjustment + its reason
        "cooldown_until_ts": None,       # set after a losing exit; blocks new entries
    }


def _update_adaptive_sizing(state: Dict[str, Any], cfg: Config, ts: int) -> None:
    """
    Bounded, auditable position-size adjustment. Adjusts SIZE ONLY -- never
    entry or exit rules. See the config block for why that distinction
    matters. No-ops entirely unless explicitly enabled.
    """
    if not cfg.adaptive_sizing_enabled:
        return

    history = state["trade_history"]
    if len(history) < cfg.adaptive_min_trades:
        return  # not enough evidence to act on

    window = history[-cfg.adaptive_window:]
    wins = sum(1 for t in window if t["won"])
    win_rate = wins / len(window)

    before = state["adaptive_frac"]

    if win_rate < cfg.adaptive_low_winrate:
        after = max(cfg.adaptive_floor, before - cfg.adaptive_step)
        direction = "down"
    elif win_rate > cfg.adaptive_high_winrate:
        after = min(cfg.adaptive_ceiling, before + cfg.adaptive_step)
        direction = "up"
    else:
        return  # inside the neutral band: leave sizing alone

    if after == before:
        return  # already at the bound

    state["adaptive_frac"] = after
    state["adaptive_log"].append({
        "ts": ts,
        "win_rate": round(win_rate, 3),
        "window_size": len(window),
        "from": round(before, 3),
        "to": round(after, 3),
        "reason": (f"rolling win rate {win_rate:.0%} over last {len(window)} trades "
                   f"{'below' if direction == 'down' else 'above'} "
                   f"{cfg.adaptive_low_winrate:.0%}-{cfg.adaptive_high_winrate:.0%} band"),
    })


def _record_trade_outcome(state: Dict[str, Any], cfg: Config,
                           entry_price: float, exit_price: float, ts: int) -> None:
    """
    Update the adaptive sizing memory from a trade this call is about to
    close. This is an approximation (pre-fee, pre-slippage) used only to
    decide the NEXT position's size -- the engine's own ledger is the real
    money record and is untouched by this.

    A single isolated loss does NOT shrink sizing -- diagnosis on real data
    showed the biggest winning trends were repeatedly entered at reduced
    size because an isolated whipsaw loss (often the shakeout right before
    a real move) had just shrunk risk_frac. Sizing now only shrinks after
    cfg.losses_before_shrink consecutive losses, which should still protect
    capital in a genuine bad stretch without punishing the entry that would
    have caught the next real trend.
    """
    pnl_pct = (exit_price - entry_price) / entry_price
    won = pnl_pct > 0

    state["trade_history"].append({"ts": ts, "pnl_pct": pnl_pct, "won": won})
    state["trade_history"] = state["trade_history"][-20:]  # keep it bounded

    if won:
        state["consecutive_losses"] = 0
        state["risk_frac"] = min(cfg.base_risk_frac,
                                  state["risk_frac"] * cfg.win_recovery_factor)
    else:
        state["consecutive_losses"] += 1
        if state["consecutive_losses"] >= cfg.losses_before_shrink:
            state["risk_frac"] = max(cfg.min_risk_frac,
                                      state["risk_frac"] * cfg.loss_reduction_factor)
        # Re-entry cooldown: only after losses. A winning exit means the
        # trend was genuine, so there's no reason to sit out a fresh signal.
        if cfg.reentry_cooldown_hours > 0:
            state["cooldown_until_ts"] = ts + int(cfg.reentry_cooldown_hours * 3600)

    _update_adaptive_sizing(state, cfg, ts)


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    """
    Order of precedence, same philosophy as the original:
      1. Exits run regardless of halt status -- a halt stops new risk, it
         does not trap you in a position past your own protective rules.
      2. New entries are blocked while halted.

    Periodic profit protection is layered in at the top: closing an open
    position at a period boundary is itself a risk-reducing action, so it
    runs alongside the other exits, before anything else is considered.
    """
    actions: List[Action] = []
    price = candle["close"]
    ts = candle["ts"]

    if state["period_start_ts"] is None:
        state["period_start_ts"] = ts

    period_elapsed = (ts - state["period_start_ts"]) >= cfg.withdrawal_period_days * 86400

    if state["bars_seen"] < cfg.warmup_bars:
        return actions

    lot = state["lots"][0] if state["lots"] else None

    # --- 0. periodic profit protection ---
    if period_elapsed:
        if lot is not None:
            # Close the position first -- profit can't be protected while
            # it's still an unrealized, at-risk coin position.
            _record_trade_outcome(state, cfg, lot.entry_price, price, ts)
            return [
                Action(
                    side="sell",
                    qty_coin=lot.qty_coin,
                    lot_id=lot.lot_id,
                    reason=f"period close @ {price:.0f} (banking profit)",
                    tag="trend",
                )
            ]
        else:
            # Already flat -- safe to sweep any realized profit into the
            # reserve now, and start the next period.
            realized_overage = state["idr"] - state["principal"] - state["profit_reserve"]
            if realized_overage > 0:
                state["profit_reserve"] += realized_overage
            state["period_start_ts"] = ts

    trend = sma(state["closes"], cfg.trend_ma_period)
    if trend is None:
        return actions  # not enough history yet for the trend filter

    # --- 1. exits (always evaluated, even while halted) ---
    if lot is not None:
        effective_stop_pct = (state["stop_pct_override"]
                               if state["stop_pct_override"] is not None
                               else cfg.stop_loss_pct)
        stop_price = lot.entry_price * (1.0 - effective_stop_pct)
        trend_break_price = trend * (1.0 - cfg.trend_buffer_pct)

        hit_stop = price <= stop_price
        hit_trend_break = price < trend_break_price

        if hit_stop or hit_trend_break:
            reason = "stop loss" if hit_stop else "trend break"
            if hit_stop and state["stop_pct_override"] is not None:
                reason = f"tightened stop loss ({effective_stop_pct:.0%})"
            _record_trade_outcome(state, cfg, lot.entry_price, price, ts)
            actions.append(
                Action(
                    side="sell",
                    qty_coin=lot.qty_coin,
                    lot_id=lot.lot_id,
                    reason=f"{reason} @ {price:.0f} (entry {lot.entry_price:.0f})",
                    tag="trend",
                )
            )
            return actions  # one decision per bar keeps this easy to reason about

    # --- 2. new entries (blocked while halted) ---
    if lot is None and not state["halted"]:
        # Re-entry cooldown after a losing exit -- blocks NEW risk only.
        # Exits above are unaffected, as always.
        cooling = (state["cooldown_until_ts"] is not None
                   and ts < state["cooldown_until_ts"])
        if cooling:
            return actions

        entry_price_threshold = trend * (1.0 + cfg.trend_buffer_pct)
        if price > entry_price_threshold:
            investable = state["idr"] - state["profit_reserve"]
            spend = (investable * state["risk_frac"] * state["adaptive_frac"]
                     * state["regime_risk_multiplier"])
            if spend > 0:
                adaptive_note = ""
                if cfg.adaptive_sizing_enabled and state["adaptive_frac"] != 1.0:
                    adaptive_note = f", adaptive {state['adaptive_frac']:.2f}"
                regime_note = ""
                if state["regime_risk_multiplier"] != 1.0:
                    regime_note = f", regime-cut {state['regime_risk_multiplier']:.2f}"
                actions.append(
                    Action(
                        side="buy",
                        qty_idr=spend,
                        level_idx=0,
                        reason=(f"trend entry @ {price:.0f} "
                                f"(trend {trend:.0f}, risk_frac {state['risk_frac']:.2f}"
                                f"{adaptive_note}{regime_note}, "
                                f"reserve {state['profit_reserve']:,.0f})"),
                        tag="trend",
                    )
                )

    return actions
