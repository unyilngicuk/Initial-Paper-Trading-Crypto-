"""
Unyil Momentum -- altcoin momentum strategy.
STRATEGY_FAMILY = "MOMENTUM"

Enters the top-2 altcoins (by IDR volume) that have risen >=15% in the
last 24 hours. Manages each position with three layered exits:

  1. Hard stop      -- -3% from entry price (limits immediate reversal loss)
  2. Breakeven stop -- price drops back to entry + roundtrip fees (~0.23%)
                       once price has risen above that level (no winner turns loser)
  3. Trailing stop  -- -7% from the highest price seen since entry (rides the wave)

IMPORTANT: this strategy has NEVER been backtested on historical data --
no market-wide historical data exists to test it on. It goes straight to
paper trading. The exit rules are designed to bound losses tightly, but
no edge has been demonstrated. Treat results from the first 30 days as
pure discovery, not validation.
"""

from dataclasses import dataclass
from typing import Optional

STRATEGY_FAMILY = "MOMENTUM"

HARD_STOP_PCT = 0.03
TRAIL_PCT = 0.07
ROUNDTRIP_FEE_PCT = 0.0023
MIN_GAIN_24H_PCT = 0.15
CAPITAL_PER_SLOT = 1_000_000
EXCLUDED_COINS = {"btc", "eth", "tslax", "googlx"}


@dataclass
class MomentumSlot:
    slot_id: int
    coin: Optional[str] = None
    entry_price: float = 0.0
    peak_price: float = 0.0
    qty_coin: float = 0.0
    idr: float = CAPITAL_PER_SLOT
    halted: bool = False
    bars_seen: int = 0
    entry_ts: int = 0
    last_routine_notify_ts: float = 0.0

    @property
    def is_occupied(self) -> bool:
        return self.coin is not None and self.qty_coin > 0


def check_exit(slot: MomentumSlot, current_price: float):
    if not slot.is_occupied:
        return None, None

    entry = slot.entry_price
    if current_price > slot.peak_price:
        slot.peak_price = current_price
    peak = slot.peak_price

    hard_stop_price = entry * (1.0 - HARD_STOP_PCT)
    if current_price <= hard_stop_price:
        return (
            f"hard stop ({HARD_STOP_PCT:.0%} below entry "
            f"Rp {entry:,.0f}) @ Rp {current_price:,.0f}",
            "hard_stop"
        )

    breakeven_price = entry * (1.0 + ROUNDTRIP_FEE_PCT)
    if peak >= breakeven_price and current_price <= breakeven_price:
        return (
            f"breakeven stop (back to entry + fees "
            f"Rp {breakeven_price:,.0f}) @ Rp {current_price:,.0f}",
            "breakeven_stop"
        )

    if peak > entry:
        trail_stop_price = peak * (1.0 - TRAIL_PCT)
        if current_price <= trail_stop_price:
            return (
                f"trailing stop ({TRAIL_PCT:.0%} from peak "
                f"Rp {peak:,.0f}) @ Rp {current_price:,.0f}",
                "trailing_stop"
            )

    return None, None


def qualifies_for_entry(coin, current_price, price_24h_ago, vol_idr):
    if coin.lower() in EXCLUDED_COINS:
        return False
    if price_24h_ago <= 0 or current_price <= 0:
        return False
    gain = (current_price - price_24h_ago) / price_24h_ago
    return gain >= MIN_GAIN_24H_PCT
