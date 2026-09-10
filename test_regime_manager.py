"""
Tests for regime_manager.py.

Run: python3 test_regime_manager.py
"""

import regime_manager as rm
from indicators import adx_series, atr_series


# ---------------------------------------------------------- indicator correctness

def test_adx_stays_bounded_0_to_100():
    """
    Regression test for a real bug found during development: an earlier
    version conflated two different Wilder smoothing conventions (sum-style
    vs. average-style), which made ADX drift toward period*100 (e.g. 1400)
    instead of staying bounded. ADX is a percentage-like measure and must
    never leave [0, 100], on ANY input.
    """
    import random
    random.seed(5)
    candles = []
    price = 100.0
    for i in range(500):
        price *= (1.0 + random.uniform(-0.05, 0.055))  # deliberately noisy/extreme
        price = max(price, 1.0)
        candles.append({"high": price * 1.02, "low": price * 0.98, "close": price})
    values = adx_series(candles, 14)
    bad = [v for v in values if v is not None and (v < 0 or v > 100)]
    assert not bad, f"ADX left [0,100]: {bad[:5]}"
    print("  ok  ADX stays within [0, 100] across a noisy 500-bar series")


def test_adx_distinguishes_trend_from_chop():
    candles_trend = []
    price = 100.0
    for i in range(200):
        price *= 1.006
        candles_trend.append({"high": price * 1.001, "low": price * 0.999, "close": price})
    adx_trend = adx_series(candles_trend, 14)[-1]

    # A genuine oscillation (not a random walk, which can drift directionally
    # by chance) -- fast enough (multiple full cycles within the ADX window)
    # that it has no net directional persistence for ADX to pick up.
    import math
    candles_chop = []
    for i in range(200):
        price = 100.0 + 2.0 * math.sin(i * 2.0)
        candles_chop.append({"high": price * 1.001, "low": price * 0.999, "close": price})
    adx_chop = adx_series(candles_chop, 14)[-1]

    assert adx_trend > adx_chop, f"expected trend ADX ({adx_trend}) > chop ADX ({adx_chop})"
    assert adx_chop < 20, f"expected chop ADX below 20, got {adx_chop}"
    print(f"  ok  ADX distinguishes trend ({adx_trend:.1f}) from chop ({adx_chop:.1f})")


def test_atr_scales_with_volatility():
    import random
    random.seed(4)
    price = 100.0
    calm = []
    for i in range(200):
        price *= (1.0 + random.uniform(-0.002, 0.002))
        calm.append({"high": price * 1.001, "low": price * 0.999, "close": price})
    price = 100.0
    wild = []
    for i in range(200):
        price *= (1.0 + random.uniform(-0.03, 0.03))
        wild.append({"high": price * 1.02, "low": price * 0.98, "close": price})
    atr_calm = atr_series(calm, 14)[-1]
    atr_wild = atr_series(wild, 14)[-1]
    assert atr_wild > atr_calm * 3, f"expected wild ATR much bigger: calm={atr_calm}, wild={atr_wild}"
    print(f"  ok  ATR scales with real volatility (calm={atr_calm:.3f}, wild={atr_wild:.3f})")


# ---------------------------------------------------------- scoring logic

def test_classify_bullish_5_of_5():
    c = rm.classify_bar(price=110, sma7=108, sma28=100, sma28_prev24h=99.8,
                         roc7=3.0, adx48=25, atr_ratio=1.0)
    assert c.label == "BULLISH" and c.bull_score == 5, c
    print("  ok  clean 5/5 bullish signal classifies as BULLISH")


def test_classify_bullish_needs_at_least_4():
    # Only 3 of 5 conditions met: price>sma7, spread>1%, adx>=20 -- but
    # slope flat and roc7 weak. Must NOT be bullish.
    c = rm.classify_bar(price=105, sma7=104, sma28=100, sma28_prev24h=100.05,
                         roc7=0.5, adx48=22, atr_ratio=1.0)
    assert c.label != "BULLISH", f"expected not-bullish with only 3/5, got {c}"
    print("  ok  3/5 bullish conditions does not classify as BULLISH")


def test_classify_bearish_5_of_5():
    c = rm.classify_bar(price=90, sma7=92, sma28=100, sma28_prev24h=100.2,
                         roc7=-3.0, adx48=25, atr_ratio=1.0)
    assert c.label == "BEARISH" and c.bear_score == 5, c
    assert c.persistence_key == "BEARISH_5"
    print("  ok  clean 5/5 bearish signal classifies as BEARISH with the fast persistence key")


def test_classify_bearish_4_of_5_uses_slow_persistence_key():
    # 4 conditions, ADX below 20 so the 5th (trend strength) doesn't fire.
    c = rm.classify_bar(price=90, sma7=92, sma28=100, sma28_prev24h=100.2,
                         roc7=-3.0, adx48=15, atr_ratio=1.0)
    assert c.label == "BEARISH" and c.bear_score == 4, c
    assert c.persistence_key == "BEARISH_4"
    print("  ok  4/5 bearish signal uses the slower BEARISH_4 persistence key")


def test_classify_sideways_requires_positive_evidence():
    c = rm.classify_bar(price=100.2, sma7=100.1, sma28=100.0, sma28_prev24h=100.02,
                         roc7=0.3, adx48=10, atr_ratio=1.0)
    assert c.label == "SIDEWAYS" and c.sideways_score == 5, c
    print("  ok  a genuine range-bound bar classifies as SIDEWAYS (5/5)")


def test_classify_uncertain_when_nothing_reaches_4():
    # Deliberately middling on everything -- no category should clear 4/5.
    c = rm.classify_bar(price=101, sma7=100.5, sma28=100.0, sma28_prev24h=99.95,
                         roc7=1.5, adx48=17, atr_ratio=1.1)
    assert c.label == "UNCERTAIN", c
    print("  ok  ambiguous evidence (nothing reaching 4/5) classifies as UNCERTAIN")


def test_classify_missing_data_is_uncertain():
    c = rm.classify_bar(price=100, sma7=None, sma28=None, sma28_prev24h=None,
                         roc7=None, adx48=None, atr_ratio=None)
    assert c.label == "UNCERTAIN"
    print("  ok  missing indicator data (not enough warmup) classifies as UNCERTAIN")


# ---------------------------------------------------------- persistence

def _raw(label, key, **kw):
    return rm.RawClassification(label=label, bull_score=kw.get("bull", 0),
                                 bear_score=kw.get("bear", 0), sideways_score=kw.get("side", 0),
                                 volatility="NORMAL", persistence_key=key)


def test_persistence_blocks_a_single_bar_flip():
    raw = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BULLISH", "BULLISH", bull=5)] * 1
    entries = rm.apply_persistence(raw)
    assert entries[-1]["confirmed"] == "UNCERTAIN", (
        f"a single bullish bar flipped the confirmed regime: {entries[-1]['confirmed']}"
    )
    print("  ok  a single-bar signal does not flip the confirmed regime")


def test_bullish_confirms_after_16_consecutive_bars():
    raw = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BULLISH", "BULLISH", bull=5)] * 16
    entries = rm.apply_persistence(raw)
    assert entries[5 + 14]["confirmed"] == "UNCERTAIN", "confirmed too early (bar 15 of 16)"
    assert entries[5 + 15]["confirmed"] == "BULLISH", "did not confirm exactly at bar 16"
    print("  ok  BULLISH confirms exactly at the 16th consecutive matching bar, not before")


def test_bearish_5_confirms_after_only_8_bars():
    raw = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BEARISH", "BEARISH_5", bear=5)] * 8
    entries = rm.apply_persistence(raw)
    assert entries[5 + 6]["confirmed"] == "UNCERTAIN", "confirmed too early (bar 7 of 8)"
    assert entries[5 + 7]["confirmed"] == "BEARISH", "high-confidence bearish did not use the fast path"
    print("  ok  high-confidence (5/5) BEARISH confirms in only 8 bars, not 16")


def test_bearish_4_still_needs_16_bars():
    raw = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BEARISH", "BEARISH_4", bear=4)] * 16
    entries = rm.apply_persistence(raw)
    assert entries[5 + 7]["confirmed"] == "UNCERTAIN", (
        "4/5 bearish confirmed as fast as 5/5 -- the asymmetry was lost"
    )
    assert entries[5 + 15]["confirmed"] == "BEARISH"
    print("  ok  4/5 BEARISH still needs the full 16 bars (only 5/5 gets the fast path)")


def test_confirmed_key_distinguishes_bearish_4_from_5():
    """
    A downstream consumer (the switching simulation) needs to tell
    apart WHICH bearish path confirmed the regime, not just that it's
    bearish. Regression test for the field this depends on.
    """
    raw_4 = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BEARISH", "BEARISH_4", bear=4)] * 16
    entries_4 = rm.apply_persistence(raw_4)
    assert entries_4[-1]["confirmed"] == "BEARISH"
    assert entries_4[-1]["confirmed_key"] == "BEARISH_4"

    raw_5 = [_raw("UNCERTAIN", "UNCERTAIN")] * 5 + [_raw("BEARISH", "BEARISH_5", bear=5)] * 8
    entries_5 = rm.apply_persistence(raw_5)
    assert entries_5[-1]["confirmed"] == "BEARISH"
    assert entries_5[-1]["confirmed_key"] == "BEARISH_5"
    print("  ok  confirmed_key correctly distinguishes BEARISH_4 from BEARISH_5")


def test_streak_resets_if_raw_classification_changes_midway():
    raw = ([_raw("UNCERTAIN", "UNCERTAIN")] * 5
           + [_raw("BULLISH", "BULLISH", bull=5)] * 10
           + [_raw("SIDEWAYS", "SIDEWAYS", side=5)] * 3   # interrupts the streak
           + [_raw("BULLISH", "BULLISH", bull=5)] * 15)
    entries = rm.apply_persistence(raw)
    # Even though 10+15=25 bullish bars occurred in total, the interruption
    # means only the LAST 15 consecutive count -- not enough to confirm.
    assert entries[-1]["confirmed"] != "BULLISH", (
        "confirmed despite the streak being interrupted partway through"
    )
    print("  ok  an interruption resets the consecutive-bar streak correctly")


def main():
    print("\nVerifying Regime Manager")
    print("-" * 62)
    print(" indicator correctness:")
    test_adx_stays_bounded_0_to_100()
    test_adx_distinguishes_trend_from_chop()
    test_atr_scales_with_volatility()
    print(" scoring logic:")
    test_classify_bullish_5_of_5()
    test_classify_bullish_needs_at_least_4()
    test_classify_bearish_5_of_5()
    test_classify_bearish_4_of_5_uses_slow_persistence_key()
    test_classify_sideways_requires_positive_evidence()
    test_classify_uncertain_when_nothing_reaches_4()
    test_classify_missing_data_is_uncertain()
    print(" persistence / hysteresis:")
    test_persistence_blocks_a_single_bar_flip()
    test_bullish_confirms_after_16_consecutive_bars()
    test_bearish_5_confirms_after_only_8_bars()
    test_bearish_4_still_needs_16_bars()
    test_confirmed_key_distinguishes_bearish_4_from_5()
    test_streak_resets_if_raw_classification_changes_midway()
    print("-" * 62)
    print("all passed\n")


if __name__ == "__main__":
    main()
