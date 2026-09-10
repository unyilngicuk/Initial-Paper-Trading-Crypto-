"""
THE decision function.

This module is the single source of truth for what the bot does. The replay
harness imports `decide`. The live GitHub Actions job imports `decide`. There
is no second implementation anywhere, and there must never be one.

`decide` is pure: it reads state and a candle, and returns intended actions.
It never touches the network, never places an order, never mutates state.
Execution and bookkeeping belong to the engine.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import Config
from indicators import rsi_wilder


@dataclass
class Action:
    side: str                     # "buy" | "sell"
    qty_idr: Optional[float] = None    # for buys: how much IDR to spend
    qty_coin: Optional[float] = None   # for sells: how much coin to sell
    reason: str = ""
    lot_id: Optional[int] = None       # which grid lot this closes
    level_idx: Optional[int] = None    # which grid level this opens
    tag: str = "grid"                  # "grid" | "hold"


@dataclass
class Lot:
    """One filled grid level, held until its take-profit."""
    lot_id: int
    level_idx: int
    qty_coin: float
    entry_price: float
    entry_ts: int

    def target_price(self, cfg: Config) -> float:
        return self.entry_price * (1.0 + cfg.take_profit_pct)


def new_state(cfg: Config) -> Dict[str, Any]:
    return {
        "idr": cfg.starting_idr,
        "grid_coin": 0.0,       # coin held by open grid lots
        "hold_coin": 0.0,       # the 30% bucket, bought once, never sold by the bot
        "hold_bought": False,
        "closes": [],           # rolling window of closes
        "lots": [],             # list[Lot]
        "next_lot_id": 1,
        "anchor": None,         # grid reference price
        "peak_equity": cfg.starting_idr,
        "halted": False,
        "bars_seen": 0,
    }


def grid_level_price(anchor: float, idx: int, cfg: Config) -> float:
    """Level 0 sits one spacing below the anchor, level 1 two spacings, etc."""
    return anchor * (1.0 - cfg.spacing_pct * (idx + 1))


def occupied_levels(state: Dict[str, Any]) -> set:
    return {lot.level_idx for lot in state["lots"]}


def decide(state: Dict[str, Any], candle: Dict[str, Any], cfg: Config) -> List[Action]:
    """
    Given current state and the just-closed candle, return the actions to take.

    Order of precedence:
      1. Sells first (free up capital, realise profit).
      2. The one-time hold purchase.
      3. Grid buys, subject to the RSI filter and the halt flag.
    """
    actions: List[Action] = []
    price = candle["close"]
    ts = candle["ts"]

    # --- warmup ---
    if state["bars_seen"] < cfg.warmup_bars:
        return actions

    rsi = rsi_wilder(state["closes"], cfg.rsi_period)
    if rsi is None:
        return actions

    # --- 1. take profit on any lot whose target the candle high reached ---
    # Which price counts as "reached" depends on cfg.fill_model -- see config.py.
    # Default is the close, because a 15-minute polling bot sending market
    # orders cannot capture an intrabar touch it never saw.
    exit_probe = candle["close"] if cfg.fill_model == "close" else candle["high"]
    for lot in state["lots"]:
        if exit_probe >= lot.target_price(cfg):
            actions.append(
                Action(
                    side="sell",
                    qty_coin=lot.qty_coin,
                    lot_id=lot.lot_id,
                    reason=f"take profit lvl{lot.level_idx} @ {lot.target_price(cfg):.0f}",
                    tag="grid",
                )
            )

    # --- 2. the hold bucket: buy once, then leave it alone forever ---
    if not state["hold_bought"] and not state["halted"]:
        actions.append(
            Action(
                side="buy",
                qty_idr=cfg.hold_capital,
                reason="initial hold allocation",
                tag="hold",
            )
        )

    # --- everything below is new risk; the halt blocks it ---
    if state["halted"]:
        return actions

    # --- 3. anchor management ---
    anchor = state["anchor"]
    if anchor is None:
        anchor = price
    elif not state["lots"] and price > anchor * (1.0 + cfg.recentre_pct):
        # Flat and price has run away upward: follow it up rather than
        # waiting forever for a retrace to a stale grid.
        anchor = price

    # --- 4. grid buys ---
    if rsi < cfg.rsi_buy_below and rsi < cfg.rsi_block_above:
        taken = occupied_levels(state)
        pending_spend = sum(a.qty_idr or 0.0 for a in actions if a.side == "buy")

        for idx in range(cfg.num_levels):
            if idx in taken:
                continue
            level_price = grid_level_price(anchor, idx, cfg)

            entry_probe = candle["close"] if cfg.fill_model == "close" else candle["low"]
            if entry_probe > level_price:
                continue

            if state["idr"] - pending_spend < cfg.slice_idr:
                break  # out of dry powder

            actions.append(
                Action(
                    side="buy",
                    qty_idr=cfg.slice_idr,
                    level_idx=idx,
                    reason=f"grid lvl{idx} @ {level_price:.0f} rsi={rsi:.1f}",
                    tag="grid",
                )
            )
            pending_spend += cfg.slice_idr

    state["_pending_anchor"] = anchor
    return actions
