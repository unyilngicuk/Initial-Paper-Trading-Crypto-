"""
Indicators computed from a rolling window of closes.

Deliberately written to work on a plain list, so the replay loop and the live
job compute RSI the same way from the same state. No pandas, no lookahead.
"""

from collections import deque
from typing import Deque, Dict, Optional, Sequence


def rsi_wilder(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """
    Wilder's RSI. Returns None until there are enough bars.

    Uses the full window each call. That is O(n) rather than O(1), but n is
    small (we keep ~100 closes) and correctness beats cleverness here: an
    incremental version that drifts from the backtest version is exactly the
    bug this whole design exists to prevent.
    """
    if len(closes) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change

    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def new_close_window(maxlen: int = 120) -> Deque[float]:
    return deque(maxlen=maxlen)


def sma(closes: Sequence[float], period: int) -> Optional[float]:
    """
    Simple moving average over the most recent `period` closes.
    Returns None until there are enough bars — no partial-window average,
    which would quietly bias an early trend read.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def roc(closes: Sequence[float], period: int) -> Optional[float]:
    """Rate of change, as a percentage, over `period` bars back."""
    if len(closes) <= period:
        return None
    past = closes[-1 - period]
    if past == 0:
        return None
    return (closes[-1] / past - 1.0) * 100.0


def _wilder_sum_smooth(values: Sequence[float], period: int) -> list:
    """
    Wilder's smoothing of a running SUM: seeds with the sum of the first
    `period` raw values, then each step is prev - prev/period + current.
    Used for +DM/-DM/TR feeding into the DI ratio, where a consistent
    scale on both sides of the ratio cancels out regardless of whether
    it's tracked as a sum or an average.
    """
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    seed = sum(values[:period])
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = prev - (prev / period) + values[i]
        out[i] = prev
    return out


def _wilder_avg_smooth(values: Sequence[float], period: int) -> list:
    """
    Wilder's moving average: seeds with the SIMPLE AVERAGE of the first
    `period` values, then each step is (prev*(period-1) + current) / period.
    This is the recursion that actually preserves an average scale --
    used for reporting ATR itself, and for smoothing DX into ADX. Mixing
    this up with the sum-style smoothing above (seeding with an average
    but recursing as a sum) is a real bug: it silently drifts the result
    toward period times its true value over time, e.g. ADX creeping
    toward ~1400 instead of staying bounded at 0-100.
    """
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def atr_series(candles: Sequence[Dict[str, float]], period: int) -> list:
    """
    Average True Range (Wilder), over full OHLC candles. Returns a list
    aligned to `candles`; entries before enough history exists are None.
    """
    n = len(candles)
    if n < 2:
        return [None] * n
    tr = [0.0]  # no TR for the very first bar (no previous close)
    for i in range(1, n):
        h, l = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return _wilder_avg_smooth(tr, period)


def adx_series(candles: Sequence[Dict[str, float]], period: int) -> list:
    """
    Average Directional Index (Wilder), over full OHLC candles. Returns a
    list aligned to `candles`; entries before enough history exists are
    None. +DM/-DM/TR are sum-smoothed (their ratio cancels the scale);
    DX is computed from the resulting DI's (always bounded 0-100 by
    construction); ADX is the AVERAGE-smoothed series of DX (also then
    correctly bounded 0-100).
    """
    n = len(candles)
    if n < 2:
        return [None] * n

    plus_dm = [0.0]
    minus_dm = [0.0]
    tr = [0.0]
    for i in range(1, n):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        h, l = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))

    smoothed_plus_dm = _wilder_sum_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_sum_smooth(minus_dm, period)
    smoothed_tr = _wilder_sum_smooth(tr, period)

    dx = [None] * n
    for i in range(n):
        if smoothed_tr[i] is None or smoothed_tr[i] == 0:
            continue
        plus_di = 100.0 * smoothed_plus_dm[i] / smoothed_tr[i]
        minus_di = 100.0 * smoothed_minus_dm[i] / smoothed_tr[i]
        denom = plus_di + minus_di
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom

    first_dx_idx = period - 1
    dx_tail = [x if x is not None else 0.0 for x in dx[first_dx_idx:]]
    adx_tail = _wilder_avg_smooth(dx_tail, period)

    out = [None] * n
    for j, val in enumerate(adx_tail):
        if val is not None:
            out[first_dx_idx + j] = val
    return out
