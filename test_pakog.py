import time
from strategy_pakog import (
    PakOgahSlot, check_exit, compute_score,
    score_24h_momentum, score_7d_trend, score_volume,
    INITIAL_CAPITAL, COLLAPSE_THRESHOLD,
    HARD_STOP_PCT, TRAIL_PCT, BREAKEVEN_PCT, TRAIL_ACTIVATION_PCT,
    EARLY_FAILURE_PCT, EARLY_FAILURE_SCANS, PROVEN_PCT,
    TREND_RIDING_PCT, EMERGENCY_FLOOR_PCT,
)

def make_slot(coin="doge", entry_price=1000.0):
    return PakOgahSlot(
        slot_id=1, coin=coin, entry_price=entry_price,
        peak_price=entry_price, qty_coin=100.0,
        balance=0.0, deployed_capital=INITIAL_CAPITAL,
        entry_ts=int(time.time()), phase="risk", scans_since_entry=0)

def test_phase_risk_to_proven():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    assert slot.phase == "proven"
    print("  ok  risk to proven at +3%")

def test_phase_proven_to_trend_riding():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    check_exit(slot, 1081.0, 60)
    assert slot.phase == "trend_riding"
    print("  ok  proven to trend-riding at +8%")

def test_breakeven_in_proven():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    check_exit(slot, 1041.0, 60)
    assert slot.breakeven_active
    print("  ok  breakeven activates at +4% inside proven")

def test_trail_in_trend_riding():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    check_exit(slot, 1081.0, 60)
    check_exit(slot, 1101.0, 60)
    assert slot.trail_active
    print("  ok  trail activates at +10% in trend-riding")

def test_hard_stop_5pct():
    slot = make_slot()
    _, t = check_exit(slot, 949.0, 60)
    assert t == "hard_stop"
    print("  ok  hard stop at -5%")

def test_hard_stop_silent_29pct():
    slot = make_slot()
    _, t = check_exit(slot, 971.0, 60)
    assert t is None
    print("  ok  hard stop silent at -2.9%")

def test_early_failure():
    slot = make_slot()
    _, t = check_exit(slot, 969.0, 60)
    assert t == "early_failure"
    print("  ok  early failure at -3% in risk phase")

def test_early_failure_not_after_proven():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    _, t = check_exit(slot, 969.0, 60)
    assert t != "early_failure"
    print("  ok  early failure inactive after proven")

def test_early_failure_after_scan5():
    slot = make_slot()
    for _ in range(5):
        check_exit(slot, 980.0, 60)
    _, t = check_exit(slot, 969.0, 60)
    assert t != "early_failure"
    print("  ok  early failure inactive after scan 5")

def test_emergency_floor():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    check_exit(slot, 1081.0, 60)
    check_exit(slot, 1300.0, 60)
    _, t = check_exit(slot, 1143.0, 60)
    assert t == "emergency_floor"
    print("  ok  emergency floor at -12% from peak in trend-riding")

def test_momentum_collapse():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    _, t = check_exit(slot, 1010.0, COLLAPSE_THRESHOLD - 1)
    assert t == "momentum_collapse"
    print("  ok  momentum collapse when score <42")

def test_trailing_stop():
    slot = make_slot()
    check_exit(slot, 1031.0, 60)
    check_exit(slot, 1081.0, 60)
    check_exit(slot, 1200.0, 60)
    _, t = check_exit(slot, 1200.0 * (1 - TRAIL_PCT) - 1, 60)
    assert t == "trailing_stop"
    print("  ok  trailing stop at -7% from peak")

def test_compute_score():
    ticker = {"last":"1150","high":"1200","low":"1000","buy":"1148","sell":"1152","vol_idr":"2000000000"}
    total, scores = compute_score(ticker, "1000", "800")
    assert total > 0 and len(scores) == 7
    print(f"  ok  compute_score returns {total}/70")

def test_halt():
    slot = PakOgahSlot(slot_id=1)
    slot.balance = INITIAL_CAPITAL * 0.60
    assert slot.is_halted_by_loss
    print("  ok  halt at 40% loss")

def test_empty_no_exit():
    slot = PakOgahSlot(slot_id=1)
    _, t = check_exit(slot, 1000.0, 60)
    assert t is None
    print("  ok  empty slot never exits")

print("\nVerifying Pak Ogah Lite v2")
print("-" * 50)
print(" phases:")
test_phase_risk_to_proven()
test_phase_proven_to_trend_riding()
test_breakeven_in_proven()
test_trail_in_trend_riding()
print(" hard stop:")
test_hard_stop_5pct()
test_hard_stop_silent_29pct()
print(" early failure:")
test_early_failure()
test_early_failure_not_after_proven()
test_early_failure_after_scan5()
print(" emergency floor:")
test_emergency_floor()
print(" other exits:")
test_momentum_collapse()
test_trailing_stop()
print(" misc:")
test_compute_score()
test_halt()
test_empty_no_exit()
print("-" * 50)
print("all passed\n")
