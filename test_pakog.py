import time
from strategy_pakog import (
    PakOgahSlot, check_exit, compute_score,
    score_24h_momentum, score_7d_trend, score_volume,
    score_proximity_to_high, score_volatility, score_spread,
    score_acceleration, INITIAL_CAPITAL, COLLAPSE_THRESHOLD,
    HARD_STOP_PCT, TRAIL_PCT, BREAKEVEN_PCT, TRAIL_ACTIVATION_PCT,
)

def make_slot(coin="doge", entry_price=1000.0):
    s = PakOgahSlot(slot_id=1, coin=coin, entry_price=entry_price,
                    peak_price=entry_price, qty_coin=100.0,
                    balance=0.0, deployed_capital=INITIAL_CAPITAL,
                    entry_ts=int(time.time()))
    return s

def test_hard_stop():
    slot = make_slot(entry_price=1000.0)
    _, t = check_exit(slot, 1000.0 * (1 - HARD_STOP_PCT), 60)
    assert t == "hard_stop"
    print("  ok  hard stop at -4%")

def test_hard_stop_silent():
    slot = make_slot(entry_price=1000.0)
    _, t = check_exit(slot, 970.0, 60)
    assert t is None
    print("  ok  hard stop silent at -3%")

def test_momentum_collapse():
    slot = make_slot(entry_price=1000.0)
    _, t = check_exit(slot, 1010.0, COLLAPSE_THRESHOLD - 1)
    assert t == "momentum_collapse"
    print("  ok  momentum collapse exit when score < 42")

def test_no_collapse_above_threshold():
    slot = make_slot(entry_price=1000.0)
    _, t = check_exit(slot, 1010.0, COLLAPSE_THRESHOLD)
    assert t is None
    print("  ok  no collapse when score >= 42")

def test_breakeven_activates():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1000.0 * (1 + BREAKEVEN_PCT + 0.01), 60)
    assert slot.breakeven_active
    print("  ok  breakeven activates at +4%")

def test_trail_activates():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1000.0 * (1 + TRAIL_ACTIVATION_PCT + 0.01), 60)
    assert slot.trail_active
    print("  ok  trail activates at +10% with score >= 42")

def test_trail_no_activate_low_score():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1000.0 * (1 + TRAIL_ACTIVATION_PCT + 0.01), 30)
    assert not slot.trail_active
    print("  ok  trail does not activate when score < 42")

def test_trailing_stop():
    slot = make_slot(entry_price=1000.0)
    check_exit(slot, 1200.0, 60)
    assert slot.trail_active
    _, t = check_exit(slot, 1200.0 * (1 - TRAIL_PCT) - 1, 60)
    assert t == "trailing_stop"
    print("  ok  trailing stop fires at -7% from peak")

def test_score_24h_momentum():
    assert score_24h_momentum(0.17) == 10
    assert score_24h_momentum(0.30) == 5
    assert score_24h_momentum(0.60) == 0
    assert score_24h_momentum(0.03) == 0
    print("  ok  24h momentum scoring")

def test_score_7d_trend():
    assert score_7d_trend(0.40) == 10
    assert score_7d_trend(-0.25) == 0
    assert score_7d_trend(0.05) == 6
    print("  ok  7d trend scoring")

def test_score_volume():
    assert score_volume(50_000_000) == 0
    assert score_volume(150_000_000) == 6
    assert score_volume(10_000_000_000) == 10
    print("  ok  volume scoring")

def test_score_proximity():
    assert score_proximity_to_high(990, 900, 1000) == 10
    assert score_proximity_to_high(940, 900, 1000) == 3
    print("  ok  proximity to high scoring")

def test_score_volatility():
    assert score_volatility(1200, 1000, 1100) == 10
    assert score_volatility(1010, 1000, 1005) == 2
    print("  ok  volatility scoring")

def test_score_spread():
    assert score_spread(9999, 10000, 10000) == 10
    assert score_spread(980, 1020, 1000) == 0
    print("  ok  spread scoring")

def test_score_acceleration():
    assert score_acceleration(0.15, 0.07) == 10
    assert score_acceleration(-0.05, 0.10) == 0
    print("  ok  acceleration scoring")

def test_compute_score():
    ticker = {"last": "1150", "high": "1200", "low": "1000",
              "buy": "1148", "sell": "1152", "vol_idr": "2000000000"}
    total, scores = compute_score(ticker, "1000", "800")
    assert total > 0
    assert len(scores) == 7
    print(f"  ok  compute_score returns {total}/70")

def test_empty_slot_no_exit():
    slot = PakOgahSlot(slot_id=1)
    _, t = check_exit(slot, 1000.0, 60)
    assert t is None
    print("  ok  empty slot never exits")

def test_halt_threshold():
    slot = PakOgahSlot(slot_id=1)
    slot.balance = INITIAL_CAPITAL * 0.60
    assert slot.is_halted_by_loss
    slot.balance = INITIAL_CAPITAL * 0.61
    assert not slot.is_halted_by_loss
    print("  ok  halt at 40% loss")

def test_halt_false_while_occupied():
    slot = make_slot()
    slot.balance = 0.0
    assert not slot.is_halted_by_loss
    print("  ok  halt False while position open")

print("\nVerifying Pak Ogah Lite")
print("-" * 55)
print(" exit logic:")
test_hard_stop(); test_hard_stop_silent()
test_momentum_collapse(); test_no_collapse_above_threshold()
test_breakeven_activates(); test_trail_activates()
test_trail_no_activate_low_score(); test_trailing_stop()
test_empty_slot_no_exit()
print(" scoring:")
test_score_24h_momentum(); test_score_7d_trend()
test_score_volume(); test_score_proximity()
test_score_volatility(); test_score_spread()
test_score_acceleration(); test_compute_score()
print(" capital:")
test_halt_threshold(); test_halt_false_while_occupied()
print("-" * 55)
print("all passed\n")
