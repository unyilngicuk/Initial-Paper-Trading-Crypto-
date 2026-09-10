"""
Verification on synthetic candles, where the correct answer is known in advance.

Run: python test_harness.py
"""

import math
import random
from typing import Any, Dict, List

from backtest import run
from config import Config
from engine import equity
from indicators import rsi_wilder
from report import compute_metrics

BAR = 900  # 15 minutes


def make_candles(prices: List[float], start_ts: int = 1_700_000_000,
                 wick: float = 0.002) -> List[Dict[str, Any]]:
    out = []
    for i, px in enumerate(prices):
        prev = prices[i - 1] if i else px
        out.append({
            "ts": start_ts + i * BAR,
            "open": prev,
            "high": max(prev, px) * (1 + wick),
            "low": min(prev, px) * (1 - wick),
            "close": px,
            "volume": 1.0,
        })
    return out


def oscillating(n: int, base: float, amp_pct: float, period: int) -> List[float]:
    return [base * (1 + amp_pct * math.sin(2 * math.pi * i / period)) for i in range(n)]


def crash(n: int, base: float, total_drop: float) -> List[float]:
    return [base * (1 - total_drop * (i / max(n - 1, 1))) for i in range(n)]


# ---------------------------------------------------------------- tests

def test_rsi_bounds():
    rising = list(range(1, 60))
    falling = list(range(60, 1, -1))
    assert rsi_wilder(rising, 14) > 99, "RSI on a pure uptrend should pin near 100"
    assert rsi_wilder(falling, 14) < 1, "RSI on a pure downtrend should pin near 0"
    assert rsi_wilder([1, 2, 3], 14) is None, "RSI must return None before warmup"
    print("  ok  rsi bounds and warmup")


def test_config_rejects_uneconomic_grid():
    cfg = Config(take_profit_pct=0.001, slippage_pct=0.0005)
    problems = cfg.validate()
    assert any("after costs" in p for p in problems), \
        "a grid narrower than its own fees must be rejected"
    print("  ok  fee check rejects an uneconomic grid")


def test_grid_cycles_in_chop():
    """An oscillating market should produce completed buy->sell cycles."""
    cfg = Config(starting_idr=10_000_000, num_levels=4, spacing_pct=0.02,
                 take_profit_pct=0.02, warmup_bars=30)
    prices = oscillating(1200, 1_000_000_000, 0.06, 90)
    result = run(make_candles(prices), cfg)
    m = compute_metrics(result)

    assert m["n_buys"] > 0, "grid never bought in a 6% oscillation"
    assert m["n_sells"] > 0, "grid never took profit in a 6% oscillation"
    assert m["total_fees_idr"] > 0, "fees were never charged"
    print(f"  ok  grid cycled in chop ({m['n_buys']} buys, {m['n_sells']} sells, "
          f"return {m['return_pct']:.2%})")


def test_fees_reduce_return():
    """Same market, higher fee, must return less. Catches fee bypass bugs."""
    prices = oscillating(1200, 1_000_000_000, 0.06, 90)
    candles = make_candles(prices)

    lo = compute_metrics(run(candles, Config(
        fee_buy_pct=0.000111, fee_sell_pct=0.002211, mode="lightning",
        spacing_pct=0.02, take_profit_pct=0.02, warmup_bars=30)))
    hi = compute_metrics(run(candles, Config(
        fee_buy_pct=0.001111, fee_sell_pct=0.003211, mode="pro",
        spacing_pct=0.02, take_profit_pct=0.02, warmup_bars=30)))

    assert hi["total_fees_idr"] > lo["total_fees_idr"], "higher fee charged no more"
    assert hi["return_pct"] < lo["return_pct"], "higher fee did not reduce return"
    print(f"  ok  fees bite (lightning: {lo['return_pct']:.2%}, "
          f"pro: {hi['return_pct']:.2%})")


def test_drawdown_halt_fires():
    """A deep crash must trip the 20% halt and stop new buying."""
    cfg = Config(starting_idr=10_000_000, num_levels=6, spacing_pct=0.015,
                 take_profit_pct=0.015, max_drawdown_pct=0.20, warmup_bars=30)
    prices = [1_000_000_000] * 60 + crash(500, 1_000_000_000, 0.55)
    result = run(make_candles(prices), cfg)
    m = compute_metrics(result)

    assert result["state"]["halted"], "60% crash did not trip the 20% halt"
    assert any("HALT" in e for e in result["events"]), "halt was not recorded"

    # Existing positions must be left alone, not liquidated.
    coin_held = result["state"]["grid_coin"] + result["state"]["hold_coin"]
    assert coin_held > 0, "halt liquidated positions; it must only stop NEW trades"

    # No buy may occur after the halt bar.
    halt_ts = int(next(e for e in result["events"] if "HALT" in e).split("]")[0][1:])
    late_buys = [f for f in result["fills"] if f.side == "buy" and f.ts > halt_ts]
    assert not late_buys, f"{len(late_buys)} buys executed after the halt"
    print(f"  ok  halt fired at {m['max_drawdown_pct']:.1%} dd, positions kept, "
          f"no buys after")


def test_no_negative_balances():
    """Random walks must never drive a balance below zero."""
    random.seed(7)
    for trial in range(6):
        px = 1_000_000_000.0
        prices = []
        for _ in range(900):
            px *= (1 + random.gauss(0, 0.004))
            prices.append(px)
        result = run(make_candles(prices), Config(warmup_bars=30))
        s = result["state"]
        assert s["idr"] >= -1e-6, f"trial {trial}: negative IDR {s['idr']}"
        assert s["grid_coin"] >= -1e-9, f"trial {trial}: negative coin"
        lot_sum = sum(l.qty_coin for l in s["lots"])
        assert abs(lot_sum - s["grid_coin"]) < 1e-8, f"trial {trial}: lot/coin mismatch"
    print("  ok  no negative balances or lot mismatch across 6 random walks")


def test_capital_never_exceeded():
    """Total spend must respect the 70/30 split."""
    cfg = Config(starting_idr=10_000_000, warmup_bars=30)
    prices = crash(600, 1_000_000_000, 0.30)
    result = run(make_candles([1_000_000_000] * 40 + prices), cfg)

    grid_spend = sum(f.gross_idr for f in result["fills"]
                     if f.side == "buy" and f.tag == "grid")
    hold_spend = sum(f.gross_idr for f in result["fills"]
                     if f.side == "buy" and f.tag == "hold")

    assert hold_spend <= cfg.hold_capital + 1e-6, "hold bucket overspent"
    assert result["state"]["hold_coin"] > 0, "hold bucket never bought"
    # Grid may recycle capital across cycles, but never exceed its allocation
    # in concurrent open lots.
    open_cost = sum(l.qty_coin * l.entry_price for l in result["state"]["lots"])
    assert open_cost <= cfg.grid_capital * 1.05, "concurrent grid exposure exceeded"
    print(f"  ok  allocation respected (hold {hold_spend:,.0f}, "
          f"open grid exposure {open_cost:,.0f})")


def test_benchmark_starts_when_bot_can_act():
    """
    Buy & hold must be measured from the first tradeable bar, not bar 0.
    A sharp move during warmup would otherwise be scored as strategy edge.
    """
    # Price jumps 20% during warmup, then goes flat. The bot cannot touch
    # that move, so it must not appear in the benchmark.
    prices = [1_000_000_000.0] * 20 + [1_200_000_000.0] * 400
    cfg = Config(warmup_bars=30)
    m = compute_metrics(run(make_candles(prices), cfg))
    assert abs(m["buy_hold_pct"]) < 0.01, (
        f"benchmark picked up the pre-warmup jump: {m['buy_hold_pct']:.2%}")
    print("  ok  benchmark excludes the warmup window")


def test_backtest_uses_the_live_decide():
    """Guard against the drift this whole design exists to prevent."""
    import backtest
    import strategy
    assert backtest.decide is strategy.decide, \
        "backtest must import the same decide() the live job calls"
    print("  ok  backtest and live share one decide()")


if __name__ == "__main__":
    print("\nVerifying replay harness on synthetic data\n" + "-" * 46)
    tests = [
        test_rsi_bounds,
        test_config_rejects_uneconomic_grid,
        test_grid_cycles_in_chop,
        test_fees_reduce_return,
        test_drawdown_halt_fires,
        test_no_negative_balances,
        test_capital_never_exceeded,
        test_benchmark_starts_when_bot_can_act,
        test_backtest_uses_the_live_decide,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 46)
    print("all passed\n" if not failed else f"{failed} failed\n")
    raise SystemExit(1 if failed else 0)
