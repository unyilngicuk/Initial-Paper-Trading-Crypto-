"""
Tests for strategy_momentum.py exit and entry logic.
Run: python3 test_momentum.py
"""
import time
from strategy_momentum import (
    MomentumSlot, check_exit, qualifies_for_entry,
    HARD_STOP_PCT, TRAIL_PCT, ROUNDTRIP_FEE_PCT,
    MIN_GAIN_24H_PCT, EXCLUDED_COINS, CAPITAL_PER_SLOT,
)


def make_slot(coin="doge", entry_price=1000.0, peak_price=None):
    s = MomentumSlot(slot_id=1, coin=coin, entry_price=entry_price,
                     peak_price=peak_price or entry_price,
                     qty_coin=100.0, idr=0.0, entry_ts=int(time.time()))
    return s


def test_hard_stop_fires_on_immediate_drop():
    slot = make_slot(entry_price=1000.0, peak_price=1000.0)
    reason, exit_type = check_exit(slot, 1000.0 * (1 - HARD_STOP_PCT))
    assert exit_type == "hard_stop", f"expected hard_stop, got {exit_type}"
    print("  ok  hard stop fires when price drops 3% from entry")

def test_hard_stop_does_not_fire_on_shallow_drop():
    slot = make_slot(entry_price=1000.0, peak_price=1000.0)
    reason, exit_type = check_exit(slot, 980.0)
    assert exit_type is None, f"hard stop fired too early: {exit_type}"
    print("  ok  hard stop does not fire on a shallow 2% drop")

def test_breakeven_stop_fires_after_rising_then_falling_back():
    slot = make_slot(entry_price=1000.0)
    breakeven = 1000.0 * (1 + ROUNDTRIP_FEE_PCT)
    check_exit(slot, breakeven + 5.0)
    reason, exit_type = check_exit(slot, breakeven - 1.0)
    assert exit_type == "breakeven_stop", f"expected breakeven_stop, got {exit_type}"
    print("  ok  breakeven stop fires when price drops back to entry+fees after rising above it")

def test_breakeven_stop_does_not_fire_before_rising_above_it():
    slot = make_slot(entry_price=1000.0, peak_price=1000.0)
    reason, exit_type = check_exit(slot, 1001.0)
    assert exit_type is None
    print("  ok  breakeven stop does not fire if price never rose above it")

def test_trailing_stop_fires_from_peak():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    reason, exit_type = check_exit(slot, 1200.0 * (1 - TRAIL_PCT) - 1)
    assert exit_type == "trailing_stop", f"expected trailing_stop, got {exit_type}"
    print("  ok  trailing stop fires on 7% drop from peak")

def test_trailing_stop_does_not_fire_on_shallow_pullback():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    reason, exit_type = check_exit(slot, 1200.0 * 0.96)
    assert exit_type is None, f"trail fired too early: {exit_type}"
    print("  ok  trailing stop does not fire on a shallow 5% pullback from peak")

def test_trailing_stop_does_not_fire_from_entry_price():
    slot = make_slot(entry_price=1000.0, peak_price=1000.0)
    reason, exit_type = check_exit(slot, 1000.0 * (1 - TRAIL_PCT))
    assert exit_type == "hard_stop", (
        f"expected hard_stop (not trailing) when price drops from entry, got {exit_type}"
    )
    print("  ok  trailing stop does not fire from entry price -- hard stop takes priority")

def test_hard_stop_has_priority_over_trailing():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1050.0)
    reason, exit_type = check_exit(slot, 900.0)
    assert exit_type == "hard_stop", f"expected hard_stop to take priority, got {exit_type}"
    print("  ok  hard stop takes priority over trailing stop")

def test_peak_updates_on_every_bar():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1100.0)
    assert slot.peak_price == 1100.0
    check_exit(slot, 1200.0)
    assert slot.peak_price == 1200.0
    check_exit(slot, 1150.0)
    assert slot.peak_price == 1200.0
    print("  ok  peak_price updates correctly on each bar")

def test_empty_slot_never_exits():
    slot = MomentumSlot(slot_id=1)
    reason, exit_type = check_exit(slot, 1000.0)
    assert exit_type is None
    print("  ok  empty slot never triggers an exit")

def test_qualifies_at_exactly_15_percent():
    assert qualifies_for_entry("doge", 1150.0, 1000.0, vol_idr=1_000_000)
    print("  ok  coin qualifying at exactly 15% gain passes")

def test_does_not_qualify_below_15_percent():
    assert not qualifies_for_entry("doge", 1140.0, 1000.0, vol_idr=1_000_000)
    print("  ok  coin below 15% gain does not qualify")

def test_excluded_coins_never_qualify():
    for coin in EXCLUDED_COINS:
        assert not qualifies_for_entry(coin, 2000.0, 1000.0, vol_idr=9_999_999_999)
    print(f"  ok  excluded coins ({', '.join(sorted(EXCLUDED_COINS))}) never qualify")

def test_zero_price_does_not_qualify():
    assert not qualifies_for_entry("doge", 0.0, 1000.0, vol_idr=1_000_000)
    assert not qualifies_for_entry("doge", 1000.0, 0.0, vol_idr=1_000_000)
    print("  ok  zero prices correctly rejected (guard against bad API data)")

def test_capital_per_slot_is_correct():
    assert CAPITAL_PER_SLOT == 1_000_000
    print(f"  ok  capital per slot locked at Rp {CAPITAL_PER_SLOT:,.0f}")


def main():
    print("\nVerifying Unyil Momentum (altcoin momentum, paper-trading only)")
    print("-" * 66)
    print(" exit logic:")
    test_hard_stop_fires_on_immediate_drop()
    test_hard_stop_does_not_fire_on_shallow_drop()
    test_breakeven_stop_fires_after_rising_then_falling_back()
    test_breakeven_stop_does_not_fire_before_rising_above_it()
    test_trailing_stop_fires_from_peak()
    test_trailing_stop_does_not_fire_on_shallow_pullback()
    test_trailing_stop_does_not_fire_from_entry_price()
    test_hard_stop_has_priority_over_trailing()
    test_peak_updates_on_every_bar()
    test_empty_slot_never_exits()
    print(" entry logic:")
    test_qualifies_at_exactly_15_percent()
    test_does_not_qualify_below_15_percent()
    test_excluded_coins_never_qualify()
    test_zero_price_does_not_qualify()
    test_capital_per_slot_is_correct()
    print("-" * 66)
    print("all passed\n")


if __name__ == "__main__":
    main()
