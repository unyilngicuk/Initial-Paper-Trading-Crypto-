import time
from strategy_momentum import (
    MomentumSlot, check_exit, qualifies_for_entry,
    HARD_STOP_PCT, TRAIL_PCT, ROUNDTRIP_FEE_PCT,
    MIN_GAIN_21H_PCT, MAX_DROP_FROM_21H_HIGH, EXCLUDED_COINS,
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

def test_hard_stop_silent_on_shallow():
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
    print("  ok  breakeven stop fires after rise then fall back")

def test_breakeven_silent_without_rise():
    slot = make_slot(entry_price=1000.0)
    _, exit_type = check_exit(slot, 1001.0)
    assert exit_type is None
    print("  ok  breakeven stop silent if price never rose above it")

def test_trailing_fires():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    _, exit_type = check_exit(slot, 1200.0 * (1 - TRAIL_PCT) - 1)
    assert exit_type == "trailing_stop"
    print(f"  ok  trailing stop fires on {TRAIL_PCT:.0%} drop from peak")

def test_trailing_silent_on_shallow():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0)
    _, exit_type = check_exit(slot, 1200.0 * 0.98)
    assert exit_type is None
    print("  ok  trailing stop silent on shallow pullback")

def test_hard_stop_priority():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1050.0)
    _, exit_type = check_exit(slot, 900.0)
    assert exit_type == "hard_stop"
    print("  ok  hard stop takes priority over trailing")

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

def test_qualifies_fully():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1160.0, 200_000_000, slot)
    assert ok, f"should qualify: {reason}"
    print("  ok  coin passing all filters qualifies")

def test_fails_below_13pct_gain():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", 1120.0, 1000.0, 1130.0, 200_000_000, slot)
    assert not ok
    print("  ok  rejects <13% 13h gain")

def test_fails_too_far_from_high():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1280.0, 200_000_000, slot)
    assert not ok, f"should reject (too far from high): {reason}"
    print("  ok  rejects when price >5% below 13h high")

def test_passes_near_high():
    slot = MomentumSlot(slot_id=1)
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1185.0, 200_000_000, slot)
    assert ok, f"should pass (near high): {reason}"
    print("  ok  accepts entry when within 5% of 13h high")

def test_excludes_portfolio_coins():
    slot = MomentumSlot(slot_id=1)
    for coin in EXCLUDED_COINS:
        ok, _ = qualifies_for_entry(coin, 2000.0, 1000.0, 2100.0, 999_999_999, slot)
        assert not ok
    print("  ok  excluded coins never qualify")

def test_rejects_low_price():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", MIN_PRICE_IDR - 1, 200.0, 310.0, 200_000_000, slot)
    assert not ok
    print(f"  ok  rejects coins below Rp {MIN_PRICE_IDR:,.0f}")

def test_rejects_low_volume():
    slot = MomentumSlot(slot_id=1)
    ok, _ = qualifies_for_entry("doge", 1150.0, 1000.0, 1160.0, MIN_VOL_IDR - 1, slot)
    assert not ok
    print(f"  ok  rejects volume below Rp {MIN_VOL_IDR/1e6:.0f}M")

def test_loss_cooldown_blocks():
    slot = MomentumSlot(slot_id=1)
    slot.loss_cooldown["doge"] = time.time() + COOLDOWN_HOURS * 3600
    ok, reason = qualifies_for_entry("doge", 1150.0, 1000.0, 1160.0, 200_000_000, slot)
    assert not ok and "cooldown" in reason
    print(f"  ok  loss cooldown blocks re-entry for {COOLDOWN_HOURS}h")

def test_cooldown_expires():
    slot = MomentumSlot(slot_id=1)
    slot.loss_cooldown["doge"] = time.time() - 1
    ok, _ = qualifies_for_entry("doge", 1150.0, 1000.0, 1160.0, 200_000_000, slot)
    assert ok
    print("  ok  expired cooldown allows re-entry")

def test_initial_balance():
    slot = MomentumSlot(slot_id=1)
    assert slot.balance == INITIAL_CAPITAL and slot.balance_pct == 0.0
    print(f"  ok  initial balance Rp {INITIAL_CAPITAL:,.0f}")

def test_halt_threshold():
    slot = MomentumSlot(slot_id=1)
    slot.balance = INITIAL_CAPITAL * HALT_THRESHOLD
    assert slot.is_halted_by_loss
    slot.balance = INITIAL_CAPITAL * HALT_THRESHOLD + 1
    assert not slot.is_halted_by_loss
    print("  ok  halt triggers at 40% loss")

def test_balance_pct():
    slot = MomentumSlot(slot_id=1)
    slot.balance = INITIAL_CAPITAL * 1.20
    assert abs(slot.balance_pct - 20.0) < 0.01
    slot.balance = INITIAL_CAPITAL * 0.80
    assert abs(slot.balance_pct - (-20.0)) < 0.01
    print("  ok  balance_pct tracks correctly")


def main():
    print("\nVerifying Unyil Momentum v3")
    print("-" * 60)
    print(" exit logic:")
    test_hard_stop_fires(); test_hard_stop_silent_on_shallow()
    test_breakeven_fires_after_rising(); test_breakeven_silent_without_rise()
    test_trailing_fires(); test_trailing_silent_on_shallow()
    test_hard_stop_priority(); test_peak_tracks(); test_empty_slot_no_exit()
    print(" entry filters:")
    test_qualifies_fully(); test_fails_below_13pct_gain()
    test_fails_too_far_from_high(); test_passes_near_high()
    test_excludes_portfolio_coins(); test_rejects_low_price()
    test_rejects_low_volume(); test_loss_cooldown_blocks(); test_cooldown_expires()
    print(" balance and halt:")
    test_initial_balance(); test_halt_threshold(); test_balance_pct()
    print("-" * 60)
    print("all passed\n")

if __name__ == "__main__":
    main()
