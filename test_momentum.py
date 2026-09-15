"""
Tests for strategy_momentum.py v2.
Run: python3 test_momentum.py
"""
import time
from strategy_momentum import (
    MomentumSlot, check_exit, qualifies_for_entry,
    HARD_STOP_PCT, TRAIL_PCT, ROUNDTRIP_FEE_PCT,
    MIN_GAIN_24H_PCT, MAX_GAIN_5H_PCT, EXCLUDED_COINS,
    INITIAL_CAPITAL, HALT_THRESHOLD, MIN_PRICE_IDR,
    MIN_VOL_IDR, COOLDOWN_HOURS,
)


def make_slot(coin="doge", entry_price=1000.0, balance=None):
    s = MomentumSlot(slot_id=1, coin=coin, entry_price=entry_price,
                     peak_price=entry_price, qty_coin=100.0,
                     balance=balance or INITIAL_CAPITAL,
                     entry_ts=int(time.time()))
    return s


def test_hard_stop_fires():
    slot = make_slot(entry_price=1000.0)
    _, exit_type = check_exit(slot, 1000.0 * (1 - HARD_STOP_PCT))
    assert exit_type == "hard_stop"
    print("  ok  hard stop fires at -3% from entry")

def test_hard_stop_silent_on_shallow_drop():
    slot = make_slot(entry_price=1000.0)
    _, exit_type = check_exit(slot, 980.0)
    assert exit_type is None
    print("  ok  hard stop silent on -2% drop")

def test_breakeven_fires_after_rising():
    slot = make_slot(entry_price=1000.0)
    breakeven = 1000.0 * (1 + ROUNDTRIP_FEE_PCT)
    check_exit(slot, breakeven + 5.0)
    _, exit_type = check_exit(slot, breakeven - 1.0)
    assert exit_type == "breakeven_stop"
    print("  ok  breakeven stop fires after price rises then falls back")

def test_breakeven_silent_without_prior_rise():
    slot = make_slot(entry_price=1000.0)
    _, exit_type = check_exit(slot, 1001.0)
    assert exit_type is None
    print("  ok  breakeven stop silent if price never rose above it")

def test_trailing_fires():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    _, exit_type = check_exit(slot, 1200.0 * (1 - TRAIL_PCT) - 1)
    assert exit_type == "trailing_stop"
    print("  ok  trailing stop fires on 7% drop from peak")

def test_trailing_silent_on_shallow():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    _, exit_type = check_exit(slot, 1200.0 * 0.96)
    assert exit_type is None
    print("  ok  trailing stop silent on 5% pullback")

def test_hard_stop_priority():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1050.0)
    _, exit_type = check_exit(slot, 900.0)
    assert exit_type == "hard_stop"
    print("  ok  hard stop takes priority over trailing stop")

def test_peak_tracks():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1100.0)
    assert slot.peak_price == 1100.0
    check_exit(slot, 1200.0)
    assert slot.peak_price == 1200.0
    check_exit(slot, 1150.0)
    assert slot.peak_price == 1200.0
    print("  ok  peak_price tracks correctly")

def test_empty_slot_no_exit():
    slot = MomentumSlot(slot_id=1)
    _, exit_type = check_exit(slot, 1000.0)
    assert exit_type is None
    print("  ok  empty slot never exits")

def test_qualifies_basic():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1060.0, 600_000_000, slot)
    assert ok, f"should qualify: {reason}"
    print("  ok  coin with 15% 24h gain and <10% 5h gain qualifies")

def test_fails_below_15pct():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", 1140.0, 1000.0, 1060.0, 600_000_000, slot)
    assert not ok
    print("  ok  rejects <15% 24h gain")

def test_fails_fresh_spike():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1026.0, 600_000_000, slot)
    assert not ok, f"should reject fresh spike: {reason}"
    print("  ok  rejects fresh spike (>=10% in last 5h)")

def test_passes_sustained():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1095.0, 600_000_000, slot)
    assert ok, f"should pass sustained move: {reason}"
    print("  ok  accepts sustained move (<10% in last 5h)")

def test_excludes_portfolio_coins():
    slot = MomentumSlot(slot_id=1)
    for coin in EXCLUDED_COINS:
        ok, _ = qualifies_for_entry(coin, 2000.0, 1000.0, 900.0, 9_999_999_999, slot)
        assert not ok
    print("  ok  excluded coins never qualify")

def test_rejects_low_price():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", MIN_PRICE_IDR - 1, 800.0, 700.0, 600_000_000, slot)
    assert not ok
    print(f"  ok  rejects coins below Rp {MIN_PRICE_IDR:,.0f}")

def test_rejects_low_volume():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", 1150.0, 1000.0, 1060.0, MIN_VOL_IDR - 1, slot)
    assert not ok
    print(f"  ok  rejects volume below Rp {MIN_VOL_IDR/1e6:.0f}M")

def test_loss_cooldown_blocks():
    slot = MomentumSlot(slot_id=1)
    slot.loss_cooldown["doge"] = time.time() + COOLDOWN_HOURS * 3600
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1060.0, 600_000_000, slot)
    assert not ok
    assert "cooldown" in reason
    print(f"  ok  loss cooldown blocks re-entry for {COOLDOWN_HOURS}h")

def test_cooldown_expires():
    slot = MomentumSlot(slot_id=1)
    slot.loss_cooldown["doge"] = time.time() - 1
    ok, _ = qualifies_for_entry("doge", 1150.0, 1000.0, 1060.0, 600_000_000, slot)
    assert ok
    print("  ok  expired cooldown allows re-entry")

def test_initial_balance():
    slot = MomentumSlot(slot_id=1)
    assert slot.balance == INITIAL_CAPITAL
    assert slot.balance_pct == 0.0
    print(f"  ok  initial balance is Rp {INITIAL_CAPITAL:,.0f}")

def test_halt_threshold():
    slot = MomentumSlot(slot_id=1)
    slot.balance = INITIAL_CAPITAL * HALT_THRESHOLD
    assert slot.is_halted_by_loss
    slot.balance = INITIAL_CAPITAL * HALT_THRESHOLD + 1
    assert not slot.is_halted_by_loss
    print(f"  ok  halt triggers at 40% loss")

def test_balance_pct():
    slot = MomentumSlot(slot_id=1)
    slot.balance = 1_200_000
    assert abs(slot.balance_pct - 20.0) < 0.01
    slot.balance = 800_000
    assert abs(slot.balance_pct - (-20.0)) < 0.01
    print("  ok  balance_pct tracks correctly")


def main():
    print("\nVerifying Unyil Momentum v2 (improved filters, accumulating balance)")
    print("-" * 70)
    print(" exit logic:")
    test_hard_stop_fires()
    test_hard_stop_silent_on_shallow_drop()
    test_breakeven_fires_after_rising()
    test_breakeven_silent_without_prior_rise()
    test_trailing_fires()
    test_trailing_silent_on_shallow()
    test_hard_stop_priority()
    test_peak_tracks()
    test_empty_slot_no_exit()
    print(" entry filters:")
    test_qualifies_basic()
    test_fails_below_15pct()
    test_fails_fresh_spike()
    test_passes_sustained()
    test_excludes_portfolio_coins()
    test_rejects_low_price()
    test_rejects_low_volume()
    test_loss_cooldown_blocks()
    test_cooldown_expires()
    print(" balance and halt logic:")
    test_initial_balance()
    test_halt_threshold()
    test_balance_pct()
    print("-" * 70)
    print("all passed\n")


if __name__ == "__main__":
    main()
