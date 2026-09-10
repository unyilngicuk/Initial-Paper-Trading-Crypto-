"""
REGIME MANAGER -- classifies the market as Bullish / Bearish / Sideways /
Uncertain, to inform a human decision about which strategy to run
(Unyil 2.0, Guardian, or Usro). It does not switch strategies itself.

This is a deliberate design boundary, not an oversight: an automatic
switcher would be fit to the same two known historical years every other
rejected idea in this project was fit to. This tool reports evidence; a
person decides.

INDICATORS
----------
  Long-term structure   SMA28   (28 days)
  Short-term structure  SMA7    (7 days)
  Momentum              ROC7    (7 days)
  Trend strength        ADX48   (48 bars = 12 hours)
  Volatility            ATR48   (48 bars = 12 hours), normalised against
                                 its own rolling 30-day median

All periods are in 15-minute bars: 7d=672, 28d=2688, 12h=48, 24h=96,
30d=2880.

SCORING
-------
Each of Bullish / Bearish / Sideways is scored out of 5 specific
conditions (see classify_bar). A category needs 4/5 to be a candidate
regime for that bar. If none reaches 4/5, or evidence materially
conflicts, the bar is Uncertain -- not a fourth trading regime, but a
signal that there isn't enough evidence yet to prefer any strategy.

PERSISTENCE (asymmetric)
-------------------------
A raw per-bar classification must persist for a number of CONSECUTIVE
bars before the CONFIRMED regime actually changes:
  Bullish            16 bars (4h)
  Sideways           16 bars (4h)
  Uncertain          16 bars (4h)
  Bearish (4/5)      16 bars (4h)
  Bearish (5/5)       8 bars (2h) -- faster reaction to high-confidence
                                     bearish evidence specifically. The
                                     symmetric "everything bearish reacts
                                     in 2h" version was tested and
                                     performed slightly worse, so only
                                     the 5/5 case gets the fast path.
"""

import argparse
import bisect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backtest import load_candles
from indicators import adx_series, atr_series, roc, sma

BARS_PER_DAY = 96
SMA_SHORT_PERIOD = 7 * BARS_PER_DAY        # 672
SMA_LONG_PERIOD = 28 * BARS_PER_DAY        # 2688
ROC_PERIOD = 7 * BARS_PER_DAY              # 672
ADX_PERIOD = 48                            # 12h
ATR_PERIOD = 48                            # 12h
SLOPE_LOOKBACK = 96                        # 24h
ATR_MEDIAN_WINDOW = 30 * BARS_PER_DAY      # 2880

PERSISTENCE_BARS = {
    "BULLISH": 16,
    "SIDEWAYS": 16,
    "UNCERTAIN": 16,
    "BEARISH_4": 16,
    "BEARISH_5": 8,
}


@dataclass
class RawClassification:
    label: str                  # "BULLISH" | "BEARISH" | "SIDEWAYS" | "UNCERTAIN"
    bull_score: int
    bear_score: int
    sideways_score: int
    volatility: str             # "LOW" | "NORMAL" | "HIGH" | "EXTREME" | "UNKNOWN"
    persistence_key: str        # which entry of PERSISTENCE_BARS applies


def _volatility_label(atr_ratio: Optional[float]) -> str:
    if atr_ratio is None:
        return "UNKNOWN"
    if atr_ratio < 0.8:
        return "LOW"
    if atr_ratio <= 1.3:
        return "NORMAL"
    if atr_ratio <= 1.75:
        return "HIGH"
    return "EXTREME"


def _rolling_median(values: List[Optional[float]], window: int) -> List[Optional[float]]:
    """
    Rolling median over a trailing window. Deliberately simple (sorted
    list, O(window) per step) rather than a fancier data structure --
    this runs occasionally as a diagnostic tool, not inside a live
    per-bar decision loop, so clarity wins over cleverness here.
    """
    n = len(values)
    out: List[Optional[float]] = [None] * n
    window_vals: List[float] = []
    buf: List[Optional[float]] = []
    for i in range(n):
        v = values[i]
        buf.append(v)
        if v is not None:
            bisect.insort(window_vals, v)
        if len(buf) > window:
            oldest = buf.pop(0)
            if oldest is not None:
                idx = bisect.bisect_left(window_vals, oldest)
                if idx < len(window_vals) and window_vals[idx] == oldest:
                    window_vals.pop(idx)
        if window_vals:
            m = len(window_vals)
            out[i] = (window_vals[m // 2] if m % 2 == 1
                      else (window_vals[m // 2 - 1] + window_vals[m // 2]) / 2)
    return out


def classify_bar(
    price: float,
    sma7: Optional[float],
    sma28: Optional[float],
    sma28_prev24h: Optional[float],
    roc7: Optional[float],
    adx48: Optional[float],
    atr_ratio: Optional[float],
) -> RawClassification:
    """
    Score Bullish / Bearish / Sideways per the fixed rules, and decide
    Uncertain if nothing reaches 4/5. ATR is an overlay (used for the
    reported volatility label and the Sideways score), never a
    directional vote on its own.
    """
    vol = _volatility_label(atr_ratio)

    if sma7 is None or sma28 is None or sma28_prev24h is None or roc7 is None or adx48 is None:
        return RawClassification("UNCERTAIN", 0, 0, 0, vol, "UNCERTAIN")

    sma_spread_pct = (sma7 - sma28) / sma28 * 100.0
    sma28_slope_pct = (sma28 - sma28_prev24h) / sma28_prev24h * 100.0

    bull_score = 0
    bull_score += 1 if price > sma7 else 0
    bull_score += 1 if sma_spread_pct > 1.0 else 0
    bull_score += 1 if sma28_slope_pct > 0.10 else 0
    bull_score += 1 if roc7 > 2.0 else 0
    bull_score += 1 if adx48 >= 20 else 0

    bear_score = 0
    bear_score += 1 if price < sma7 else 0
    bear_score += 1 if sma_spread_pct < -1.0 else 0
    bear_score += 1 if sma28_slope_pct < -0.10 else 0
    bear_score += 1 if roc7 < -2.0 else 0
    bear_score += 1 if adx48 >= 20 else 0

    sideways_score = 0
    sideways_score += 1 if abs(sma_spread_pct) < 1.0 else 0
    sideways_score += 1 if abs(sma28_slope_pct) <= 0.10 else 0
    sideways_score += 1 if abs(roc7) <= 1.0 else 0
    sideways_score += 1 if adx48 < 15 else 0
    sideways_score += 1 if (atr_ratio is not None and atr_ratio < 1.3) else 0

    is_bullish = bull_score >= 4
    is_bearish = bear_score >= 4
    is_sideways = sideways_score >= 4

    conflict = sum([is_bullish, is_bearish, is_sideways]) > 1
    extreme_vol_unaligned = (vol == "EXTREME" and not (is_bullish and bull_score == 5)
                              and not (is_bearish and bear_score == 5))

    if conflict or extreme_vol_unaligned:
        return RawClassification("UNCERTAIN", bull_score, bear_score, sideways_score, vol, "UNCERTAIN")
    if is_bullish:
        return RawClassification("BULLISH", bull_score, bear_score, sideways_score, vol, "BULLISH")
    if is_bearish:
        key = "BEARISH_5" if bear_score == 5 else "BEARISH_4"
        return RawClassification("BEARISH", bull_score, bear_score, sideways_score, vol, key)
    if is_sideways:
        return RawClassification("SIDEWAYS", bull_score, bear_score, sideways_score, vol, "SIDEWAYS")
    return RawClassification("UNCERTAIN", bull_score, bear_score, sideways_score, vol, "UNCERTAIN")


def compute_series(candles: List[Dict[str, Any]]) -> Dict[str, List[Optional[float]]]:
    """All indicator series needed, aligned to `candles`."""
    closes = [c["close"] for c in candles]
    n = len(closes)

    sma7_series: List[Optional[float]] = [None] * n
    sma28_series: List[Optional[float]] = [None] * n
    roc7_series: List[Optional[float]] = [None] * n

    running_sum7 = 0.0
    running_sum28 = 0.0
    for i in range(n):
        running_sum7 += closes[i]
        if i >= SMA_SHORT_PERIOD:
            running_sum7 -= closes[i - SMA_SHORT_PERIOD]
        if i >= SMA_SHORT_PERIOD - 1:
            sma7_series[i] = running_sum7 / SMA_SHORT_PERIOD

        running_sum28 += closes[i]
        if i >= SMA_LONG_PERIOD:
            running_sum28 -= closes[i - SMA_LONG_PERIOD]
        if i >= SMA_LONG_PERIOD - 1:
            sma28_series[i] = running_sum28 / SMA_LONG_PERIOD

        if i >= ROC_PERIOD and closes[i - ROC_PERIOD] != 0:
            roc7_series[i] = (closes[i] / closes[i - ROC_PERIOD] - 1.0) * 100.0

    adx_full = adx_series(candles, ADX_PERIOD)
    atr_full = atr_series(candles, ATR_PERIOD)
    atr_median = _rolling_median(atr_full, ATR_MEDIAN_WINDOW)

    atr_ratio_series: List[Optional[float]] = [None] * n
    for i in range(n):
        if atr_full[i] is not None and atr_median[i] not in (None, 0):
            atr_ratio_series[i] = atr_full[i] / atr_median[i]

    return {
        "sma7": sma7_series,
        "sma28": sma28_series,
        "roc7": roc7_series,
        "adx48": adx_full,
        "atr_ratio": atr_ratio_series,
    }


def classify_all(candles: List[Dict[str, Any]], allow_wrapper: bool = False) -> List[RawClassification]:
    """
    Raises ValueError if the data looks like a WRAPPER asset (tokenized
    stock), since this classifier's indicators (SMA/ADX/ATR) were built
    and validated specifically for native crypto's continuous, 24/7
    trading -- never for wrapper assets' sparse, thin-liquidity pattern
    (confirmed structurally different: see asset_type_detector.py and
    the spec's asset-generalization sections). This is a real safety
    check, not a formality: an unguarded call here would produce a
    confident-looking regime classification using math that doesn't
    apply to what's actually in the data. Callers that genuinely need to
    bypass this (e.g. deliberately re-confirming the WRAPPER finding
    itself) can pass allow_wrapper=True.
    """
    _guard_against_wrapper_data(candles, allow_wrapper)
    series = compute_series(candles)
    n = len(candles)
    out: List[RawClassification] = []
    for i in range(n):
        sma28_prev24h = series["sma28"][i - SLOPE_LOOKBACK] if i >= SLOPE_LOOKBACK else None
        out.append(classify_bar(
            price=candles[i]["close"],
            sma7=series["sma7"][i],
            sma28=series["sma28"][i],
            sma28_prev24h=sma28_prev24h,
            roc7=series["roc7"][i],
            adx48=series["adx48"][i],
            atr_ratio=series["atr_ratio"][i],
        ))
    return out


def _guard_against_wrapper_data(candles: List[Dict[str, Any]], allow_wrapper: bool = False) -> None:
    if allow_wrapper or len(candles) < 96 * 7:
        return
    try:
        from asset_type_detector import classify_asset
        result = classify_asset(candles)
    except Exception:
        return  # never let the guard's own failure block a real native-crypto run
    if result.get("label") == "WRAPPER":
        raise ValueError(
            "regime_manager.py's classifier (SMA/ADX/ATR) was built and validated for "
            "native crypto's continuous trading, never for WRAPPER assets (tokenized "
            "stocks) -- this data looks like a WRAPPER asset. Run asset_type_detector.py "
            "on it directly for the correct read, or pass allow_wrapper=True if this is "
            "a deliberate, informed exception."
        )


def apply_persistence(raw: List[RawClassification]) -> List[Dict[str, Any]]:
    """
    Returns, per bar: the CONFIRMED regime (what a human should actually
    act on), plus the raw label, streak progress, and -- critically for
    any downstream consumer that cares about high- vs. moderate-confidence
    Bearish specifically -- which persistence key actually confirmed the
    current regime (e.g. "BEARISH_5" vs "BEARISH_4"). Confirmation
    requires `raw.persistence_key`'s required bar count of CONSECUTIVE
    matching raw classifications -- a change that reverts before reaching
    that count never confirms.
    """
    out = []
    confirmed = "UNCERTAIN"
    confirmed_key = "UNCERTAIN"
    streak_key = None
    streak_count = 0

    for r in raw:
        if r.persistence_key == streak_key:
            streak_count += 1
        else:
            streak_key = r.persistence_key
            streak_count = 1

        required = PERSISTENCE_BARS.get(streak_key, 16)
        if r.label != confirmed and streak_count >= required:
            confirmed = r.label
            confirmed_key = streak_key

        out.append({
            "confirmed": confirmed,
            "confirmed_key": confirmed_key,
            "raw": r,
            "streak_count": streak_count,
            "streak_required": required,
        })
    return out


STRATEGY_SUGGESTION = {
    "BULLISH": "Usro (validated: beat buy-and-hold in 2 of 4 tested bullish periods)",
    "BEARISH": "Guardian (validated: -0.91% vs. market -37.30% in the real crash quarter)",
    "SIDEWAYS": "Unyil 2.0 (validated: +106,724 IDR profit, 40% win rate across 10 real trades entered during confirmed Sideways conditions -- the highest win rate of any regime bucket)",
    "UNCERTAIN": "Unyil 2.0 (the general-purpose default; Uncertain itself means retain whatever is currently running)",
}


def _confidence(score: int) -> str:
    if score == 5:
        return "HIGH"
    if score == 4:
        return "MODERATE"
    return "N/A"


def format_report(entry: Dict[str, Any]) -> str:
    raw = entry["raw"]
    confirmed = entry["confirmed"]
    streak = min(entry["streak_count"], entry["streak_required"])
    required = entry["streak_required"]

    lines = [f"Regime: {confirmed}"]
    if confirmed == "UNCERTAIN":
        lines.append(f"Bull score: {raw.bull_score}/5")
        lines.append(f"Bear score: {raw.bear_score}/5")
        lines.append(f"Sideways score: {raw.sideways_score}/5")
        lines.append(f"Volatility: {raw.volatility}")
        lines.append(f"Persistence: {streak}/{required} candles confirmed")
        lines.append("Action: retain current strategy / block strategy switch")
        lines.append(f"Suggested strategy: {STRATEGY_SUGGESTION['UNCERTAIN']}")
    else:
        score = {"BULLISH": raw.bull_score, "BEARISH": raw.bear_score,
                 "SIDEWAYS": raw.sideways_score}.get(confirmed, 0)
        lines.append(f"Score: {score}/5")
        lines.append(f"Volatility: {raw.volatility}")
        lines.append(f"Persistence: {streak}/{required} candles confirmed")
        lines.append(f"Confidence: {_confidence(score)}")
        lines.append(f"Suggested strategy: {STRATEGY_SUGGESTION[confirmed]}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--history", action="store_true",
                   help="print every confirmed regime CHANGE across the full dataset, "
                        "instead of just the latest snapshot")
    args = p.parse_args()

    candles = load_candles(args.candles)
    if len(candles) < SMA_LONG_PERIOD + SLOPE_LOOKBACK:
        raise SystemExit(
            f"need at least {SMA_LONG_PERIOD + SLOPE_LOOKBACK} bars for a first reading, "
            f"got {len(candles)}"
        )

    try:
        raw = classify_all(candles)
    except ValueError as e:
        raise SystemExit(f"\n{e}\n")
    entries = apply_persistence(raw)

    if args.history:
        last_confirmed = None
        for i, e in enumerate(entries):
            if e["confirmed"] != last_confirmed:
                ts = candles[i]["ts"]
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                print(f"{dt}  ->  {e['confirmed']}  "
                      f"(bull {e['raw'].bull_score}/5, bear {e['raw'].bear_score}/5, "
                      f"sideways {e['raw'].sideways_score}/5, vol {e['raw'].volatility})")
                last_confirmed = e["confirmed"]
    else:
        last = entries[-1]
        ts = candles[-1]["ts"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"As of {dt}:\n")
        print(format_report(last))


if __name__ == "__main__":
    main()
