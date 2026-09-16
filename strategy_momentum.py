from dataclasses import dataclass, field
from typing import Optional

STRATEGY_FAMILY = "MOMENTUM"
HARD_STOP_PCT          = 0.03
TRAIL_PCT              = 0.03
ROUNDTRIP_FEE_PCT      = 0.0023
MIN_GAIN_21H_PCT       = 0.13
MAX_DROP_FROM_13H_HIGH = 0.05
INITIAL_CAPITAL        = 1_000_000
HALT_THRESHOLD         = 0.60
EXCLUDED_COINS         = {"btc", "eth", "tslax", "googlx", "nvdax"}
MIN_VOL_IDR            = 100_000_000
MIN_PRICE_IDR          = 300
COOLDOWN_HOURS         = 13


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
    halted: bool = False
    entry_ts: int = 0
    deployed_capital: float = 0.0  # exact amount invested at entry
    last_routine_notify_ts: float = 0.0
    loss_cooldown: dict = field(default_factory=dict)

    @property
    def is_occupied(self):
        return self.coin is not None and self.qty_coin > 0

    @property
    def is_halted_by_loss(self):
        # Only check halt when not in a position -- while deployed,
        # balance is 0 which would falsely trigger the 40% loss check.
        if self.is_occupied:
            return False
        return self.balance <= self.initial_capital * HALT_THRESHOLD

    @property
    def balance_pct(self):
        return (self.balance / self.initial_capital - 1.0) * 100


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


def qualifies_for_entry(coin, current_price, price_13h_ago, high_13h, vol_idr, slot):
    import time as _time
    coin_l = coin.lower()
    if coin_l in EXCLUDED_COINS:
        return False, "excluded coin"
    if current_price < MIN_PRICE_IDR:
        return False, f"price Rp {current_price:,.0f} below Rp {MIN_PRICE_IDR:,.0f}"
    if vol_idr < MIN_VOL_IDR:
        return False, f"volume below Rp {MIN_VOL_IDR/1e6:.0f}M"
    # 13h gain check -- if candle data unavailable (common for smaller
    # altcoins on Indodax TradingView endpoint), skip this check rather
    # than blocking all entries. The pre-filter already requires >= 13%
    # 24h gain, which is a reasonable proxy when 13h data is missing.
    if price_13h_ago and price_13h_ago > 0 and current_price > 0:
        gain_13h = (current_price - price_13h_ago) / price_13h_ago
        if gain_13h < MIN_GAIN_21H_PCT:
            return False, f"13h gain {gain_13h:.1%} below {MIN_GAIN_21H_PCT:.0%}"
    else:
        print(f"[INFO] no 13h candle data for {coin_l} -- relying on 24h pre-filter", flush=True)

    # Near-high check -- only applied when 13h high data is available
    if high_13h and high_13h > 0:
        drop = (high_13h - current_price) / high_13h
        if drop > MAX_DROP_FROM_13H_HIGH:
            return False, f"price {drop:.1%} below 13h high Rp {high_13h:,.0f}"
    cooldown_expires = slot.loss_cooldown.get(coin_l, 0)
    if _time.time() < cooldown_expires:
        hours_left = (cooldown_expires - _time.time()) / 3600
        return False, f"loss cooldown active ({hours_left:.1f}h remaining)"
    return True, ""
