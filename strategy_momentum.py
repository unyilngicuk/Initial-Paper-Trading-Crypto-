from dataclasses import dataclass, field
from typing import Optional

STRATEGY_FAMILY = "MOMENTUM"
HARD_STOP_PCT          = 0.03
TRAIL_PCT              = 0.07
ROUNDTRIP_FEE_PCT      = 0.0023
MIN_GAIN_24H_PCT       = 0.10   # lower bound of 24h gain band
MAX_GAIN_24H_PCT       = 0.20   # upper bound of 24h gain band
MIN_GAIN_10H_PCT       = 0.05   # momentum confirmation: must be >5% in last 10h
INITIAL_CAPITAL        = 2_500_000
PROTECTION_THRESHOLD   = 3_000_000
PROFIT_SWEEP_THRESHOLD = 0.10
PROFIT_SWEEP_PCT       = 0.50
HALT_THRESHOLD         = 0.60
EXCLUDED_COINS         = {"btc", "eth", "tslax", "googlx", "nvdax"}
MIN_VOL_IDR            = 100_000_000
MIN_PRICE_IDR          = 300
COOLDOWN_HOURS         = 13
PROFIT_COOLDOWN_EXEMPT = 0.13


@dataclass
class MomentumSlot:
    slot_id: int
    initial_capital: float = INITIAL_CAPITAL
    balance: float = INITIAL_CAPITAL
    coin: object = None
    entry_price: float = 0.0
    peak_price: float = 0.0
    qty_coin: float = 0.0
    trade_count: int = 0
    total_pnl: float = 0.0
    profit_reserve: float = 0.0
    deployed_capital: float = 0.0
    halted: bool = False
    entry_ts: int = 0
    last_routine_notify_ts: float = 0.0
    loss_cooldown: dict = field(default_factory=dict)

    @property
    def is_occupied(self):
        return self.coin is not None and self.qty_coin > 0

    @property
    def is_halted_by_loss(self):
        if self.is_occupied:
            return False
        return self.balance <= self.initial_capital * HALT_THRESHOLD

    @property
    def balance_pct(self):
        return (self.balance / self.initial_capital - 1.0) * 100

    @property
    def total_value(self):
        return self.balance + self.profit_reserve

    @property
    def in_protection_mode(self):
        if self.balance < self.initial_capital:
            return False
        return self.balance >= PROTECTION_THRESHOLD


def check_exit(slot, current_price):
    if not slot.is_occupied:
        return None, None
    entry = slot.entry_price
    if current_price > slot.peak_price:
        slot.peak_price = current_price
    peak = slot.peak_price
    if current_price <= entry * (1.0 - HARD_STOP_PCT):
        return (f"hard stop ({HARD_STOP_PCT:.0%} below entry Rp {entry:,.0f}) @ Rp {current_price:,.0f}", "hard_stop")
    breakeven = entry * (1.0 + ROUNDTRIP_FEE_PCT)
    if peak >= breakeven and current_price <= breakeven:
        return (f"breakeven stop (Rp {breakeven:,.0f}) @ Rp {current_price:,.0f}", "breakeven_stop")
    if peak > entry and current_price <= peak * (1.0 - TRAIL_PCT):
        return (f"trailing stop ({TRAIL_PCT:.0%} from peak Rp {peak:,.0f}) @ Rp {current_price:,.0f}", "trailing_stop")
    return None, None


def qualifies_for_entry(coin, current_price, price_24h_ago,
                         price_5h_ago, vol_idr, slot):
    import time as _time
    coin_l = coin.lower()
    if coin_l in EXCLUDED_COINS:
        return False, "excluded coin"
    if current_price < MIN_PRICE_IDR:
        return False, f"price Rp {current_price:,.0f} below Rp {MIN_PRICE_IDR:,.0f}"
    if vol_idr < MIN_VOL_IDR:
        return False, f"volume below Rp {MIN_VOL_IDR/1e6:.0f}M"
    if not price_24h_ago or price_24h_ago <= 0 or current_price <= 0:
        return False, "invalid price data"
    gain_24h = (current_price - price_24h_ago) / price_24h_ago
    if gain_24h < MIN_GAIN_24H_PCT:
        return False, f"24h gain {gain_24h:.1%} below {MIN_GAIN_24H_PCT:.0%}"
    if gain_24h > MAX_GAIN_24H_PCT:
        return False, f"24h gain {gain_24h:.1%} above {MAX_GAIN_24H_PCT:.0%} (too late)"
    if price_5h_ago and price_5h_ago > 0:
        gain_10h = (current_price - price_5h_ago) / price_5h_ago
        if gain_10h < MIN_GAIN_10H_PCT:
            return False, f"10h gain {gain_10h:.1%} below {MIN_GAIN_10H_PCT:.0%} (momentum not confirmed)"
    cooldown_expires = slot.loss_cooldown.get(coin_l, 0)
    if _time.time() < cooldown_expires:
        hours_left = (cooldown_expires - _time.time()) / 3600
        return False, f"loss cooldown active ({hours_left:.1f}h remaining)"
    return True, ""
