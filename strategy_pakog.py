from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict

STRATEGY_FAMILY    = "PAK_OGAH_LITE_V2"
INITIAL_CAPITAL    = 2_500_000
PROTECTION_THRESHOLD = 3_000_000
PROFIT_SWEEP_THRESHOLD = 0.10
PROFIT_SWEEP_PCT   = 0.50
HALT_THRESHOLD     = 0.60
ROUNDTRIP_FEE_PCT  = 0.0023
COLLAPSE_THRESHOLD = 42
MIN_VOL_IDR        = 100_000_000
EXCLUDED_COINS     = {"btc", "eth", "tslax", "googlx", "nvdax"}
COOLDOWN_HOURS     = 13
PROFIT_COOLDOWN_EXEMPT = 0.13
HARD_STOP_PCT        = 0.05
EARLY_FAILURE_PCT    = 0.03
EARLY_FAILURE_SCANS  = 5
PROVEN_PCT           = 0.03
BREAKEVEN_PCT        = 0.04
TREND_RIDING_PCT     = 0.08
TRAIL_PCT            = 0.07
TRAIL_ACTIVATION_PCT = 0.10
EMERGENCY_FLOOR_PCT  = 0.12

@dataclass
class PakOgahSlot:
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
    breakeven_active: bool = False
    trail_active: bool = False
    phase: str = "risk"
    scans_since_entry: int = 0

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
    def in_protection_mode(self):
        if self.balance < self.initial_capital:
            return False
        return self.balance >= PROTECTION_THRESHOLD

def score_24h_momentum(gain):
    if gain < 0.05: return 0
    if gain < 0.10: return 2
    if gain < 0.15: return 7
    if gain < 0.20: return 10
    if gain < 0.25: return 8
    if gain < 0.35: return 5
    if gain < 0.50: return 2
    return 0

def score_7d_trend(gain):
    if gain < -0.20: return 0
    if gain < -0.10: return 2
    if gain < 0.00:  return 4
    if gain < 0.10:  return 6
    if gain < 0.30:  return 9
    if gain < 0.60:  return 10
    return 6

def score_volume(vol_idr):
    if vol_idr < 100_000_000:    return 0
    if vol_idr < 300_000_000:    return 6
    if vol_idr < 1_000_000_000:  return 7
    if vol_idr < 5_000_000_000:  return 9
    if vol_idr < 20_000_000_000: return 10
    return 7

def score_proximity_to_high(last, low, high):
    if high <= low: return 5
    position = (last - low) / (high - low)
    if position >= 0.90: return 10
    if position >= 0.75: return 8
    if position >= 0.50: return 6
    if position >= 0.25: return 3
    return 1

def score_volatility(high, low, last):
    if last <= 0: return 5
    ratio = (high - low) / last
    if ratio < 0.03: return 2
    if ratio < 0.08: return 5
    if ratio < 0.15: return 8
    if ratio < 0.25: return 10
    if ratio < 0.40: return 6
    return 2

def score_spread(buy, sell, last):
    if last <= 0: return 5
    spread = (sell - buy) / last
    if spread < 0.001: return 10
    if spread < 0.003: return 8
    if spread < 0.005: return 6
    if spread < 0.010: return 4
    if spread < 0.015: return 2
    return 0

def score_acceleration(gain_24h, gain_7d):
    daily_7d = gain_7d / 7
    acc = gain_24h - daily_7d
    if gain_24h < 0 and gain_7d > 0: return 0
    if acc > 0.10: return 10
    if acc > 0.05: return 8
    if acc > 0.02: return 6
    if acc > 0.00: return 4
    return 1

def compute_score(ticker, price_24h, price_7d):
    try:
        last    = float(ticker.get("last", 0))
        high    = float(ticker.get("high", 0))
        low     = float(ticker.get("low",  0))
        buy     = float(ticker.get("buy",  0))
        sell    = float(ticker.get("sell", 0))
        vol_idr = float(ticker.get("vol_idr", 0))
        p24h    = float(price_24h)
        p7d     = float(price_7d)
    except (TypeError, ValueError):
        return 0, {}
    if last <= 0 or p24h <= 0 or p7d <= 0:
        return 0, {}
    gain_24h = (last - p24h) / p24h
    gain_7d  = (last - p7d)  / p7d
    scores = {
        "24h momentum":   score_24h_momentum(gain_24h),
        "7d trend":       score_7d_trend(gain_7d),
        "volume":         score_volume(vol_idr),
        "proximity high": score_proximity_to_high(last, low, high),
        "volatility":     score_volatility(high, low, last),
        "spread":         score_spread(buy, sell, last),
        "acceleration":   score_acceleration(gain_24h, gain_7d),
    }
    return sum(scores.values()), scores

def check_exit(slot, current_price, current_score):
    if not slot.is_occupied:
        return None, None
    entry = slot.entry_price
    if current_price > slot.peak_price:
        slot.peak_price = current_price
    peak = slot.peak_price
    gain = (current_price - entry) / entry
    slot.scans_since_entry += 1
    if slot.phase == "risk" and gain >= PROVEN_PCT:
        slot.phase = "proven"
    if slot.phase == "proven":
        if gain >= BREAKEVEN_PCT:
            slot.breakeven_active = True
        if gain >= TREND_RIDING_PCT:
            slot.phase = "trend_riding"
    if slot.phase == "trend_riding":
        if gain >= TRAIL_ACTIVATION_PCT and current_score >= COLLAPSE_THRESHOLD:
            slot.trail_active = True
    if current_price <= entry * (1.0 - HARD_STOP_PCT):
        return (f"hard stop ({HARD_STOP_PCT:.0%} below entry Rp {entry:,.0f}) @ Rp {current_price:,.0f} [phase: {slot.phase}]", "hard_stop")
    if slot.phase == "risk" and slot.scans_since_entry <= EARLY_FAILURE_SCANS:
        if gain <= -EARLY_FAILURE_PCT:
            return (f"early failure (-{EARLY_FAILURE_PCT:.0%} in risk phase, scan {slot.scans_since_entry}) @ Rp {current_price:,.0f}", "early_failure")
    if current_score < COLLAPSE_THRESHOLD:
        return (f"momentum collapse (score {current_score}/70) @ Rp {current_price:,.0f} [phase: {slot.phase}]", "momentum_collapse")
    if slot.phase == "trend_riding":
        emergency_floor = peak * (1.0 - EMERGENCY_FLOOR_PCT)
        if current_price <= emergency_floor:
            return (f"emergency floor ({EMERGENCY_FLOOR_PCT:.0%} from peak Rp {peak:,.0f}) @ Rp {current_price:,.0f}", "emergency_floor")
    if slot.breakeven_active:
        breakeven = entry * (1.0 + ROUNDTRIP_FEE_PCT)
        if current_price <= breakeven:
            return (f"breakeven stop (Rp {breakeven:,.0f}) @ Rp {current_price:,.0f} [phase: {slot.phase}]", "breakeven_stop")
    if slot.trail_active and current_price <= peak * (1.0 - TRAIL_PCT):
        return (f"trailing stop ({TRAIL_PCT:.0%} from peak Rp {peak:,.0f}) @ Rp {current_price:,.0f} [phase: {slot.phase}]", "trailing_stop")
    return None, None
