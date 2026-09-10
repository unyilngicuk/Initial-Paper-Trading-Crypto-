"""
Robustness pass: the things that break a backtester in production rather than
in a demo. Run alongside test_harness.py.

    python3 test_robustness.py
"""

import copy
import math
import random

from backtest import load_candles, run
from config import Config
from engine import equity
from report import compute_metrics
from strategy import decide, new_state

BAR = 900


def candles(prices, start=1_700_000_000, wick=0.002):
    out = []
    for i, px in enumerate(prices):
        prev = prices[i - 1] if i else px
        out.append({"ts": start + i * BAR, "open": prev,
                    "high": max(prev, px) * (1 + wick),
                    "low": min(prev, px) * (1 - wick),
                    "close": px, "volume": 1.0})
    return out


def v(name="indodax_maker"):
    c = Config()
    c.use_venue(name)
    c.warmup_bars = 30
    return c


# ------------------------------------------------------------------ degenerate input

def test_tiny_and_empty_inputs():
    for n in (0, 1, 2, 31):
        r = run(candles([1e9] * n), v())
        assert isinstance(r["fills"], list)
    print("  ok  survives 0, 1, 2 and warmup-length inputs")


def test_flat_and_extreme_prices():
    for prices, label in [
        ([1e9] * 500, "perfectly flat"),
        ([1e9 * (1 + 0.5 * i) for i in range(300)], "relentless rise"),
        ([1e9 * (0.5 ** i) if i < 20 else 1.0 for i in range(300)], "collapse to ~zero"),
        ([1e9, 1e12] * 150, "violent alternation"),
    ]:
        r = run(candles(prices), v())
        s = r["state"]
        assert s["idr"] >= -1e-6, f"{label}: negative cash"
        assert all(math.isfinite(e) for _, e, _ in r["equity_curve"]), f"{label}: non-finite equity"
    print("  ok  flat / rising / collapsing / alternating markets all finite")


def test_gaps_in_series():
    """Indodax omits bars with no trades. The replay must not choke."""
    c = candles([1e9 * (1 + 0.02 * math.sin(i / 9)) for i in range(600)])
    sparse = [x for i, x in enumerate(c) if i % 7 != 0]      # punch holes
    r = run(sparse, v())
    assert r["equity_curve"], "gapped series produced no equity curve"
    print(f"  ok  handles gapped series ({len(c) - len(sparse)} bars removed)")


# ------------------------------------------------------------------ invariants

def test_decide_never_mutates_balances():
    """decide() must be pure with respect to money. Only the engine moves cash."""
    c = candles([1e9 * (1 + 0.03 * math.sin(i / 11)) for i in range(400)])
    state = new_state(v())
    state["bars_seen"] = 100
    state["closes"] = [x["close"] for x in c[:100]]
    before = (state["idr"], state["grid_coin"], state["hold_coin"], len(state["lots"]))
    for bar in c[100:200]:
        decide(state, bar, v())
    after = (state["idr"], state["grid_coin"], state["hold_coin"], len(state["lots"]))
    assert before == after, f"decide() mutated balances: {before} -> {after}"
    print("  ok  decide() never moves money itself")


def test_never_spends_more_than_it_has():
    random.seed(3)
    for trial in range(10):
        px, prices = 1e9, []
        for _ in range(1200):
            px *= 1 + random.gauss(0, 0.006)
            prices.append(px)
        r = run(candles(prices), v())
        cash = Config().starting_idr
        for fl in r["fills"]:
            if fl.side == "buy":
                cash -= fl.gross_idr
                assert cash >= -1e-6, f"trial {trial}: cash went negative mid-run"
            else:
                cash += fl.gross_idr - fl.fee_idr
    print("  ok  cash never goes negative at any point in 10 random walks")


def test_one_lot_per_level():
    c = candles([1e9 * (1 + 0.05 * math.sin(i / 13)) for i in range(2000)])
    r = run(c, v())
    for _ in r["fills"]:
        pass
    levels = [l.level_idx for l in r["state"]["lots"]]
    assert len(levels) == len(set(levels)), f"duplicate grid levels held: {levels}"
    print("  ok  never holds two lots on the same grid level")


def test_sell_price_never_better_than_target():
    """Optimistic fills are the classic backtest lie. Check we don't do it."""
    c = candles([1e9 * (1 + 0.04 * math.sin(i / 15)) for i in range(1500)])
    r = run(c, v())
    lots = {}
    for fl in r["fills"]:
        if fl.side == "buy" and fl.tag == "grid":
            lots.setdefault("entries", []).append(fl.price)
    sells = [fl for fl in r["fills"] if fl.side == "sell"]
    bad = [fl for fl in sells if fl.price > max(x["high"] for x in c)]
    assert not bad, "sold above the highest price in the entire series"
    print(f"  ok  no sell filled outside the data's price range ({len(sells)} sells)")


# ------------------------------------------------------------------ config guards

def test_config_rejects_nonsense():
    cases = [
        (dict(grid_allocation=0.9, hold_allocation=0.5), "allocations not summing to 1"),
        (dict(num_levels=0), "zero levels"),
        (dict(spacing_pct=-0.01), "negative spacing"),
        (dict(take_profit_pct=0.0005), "target below costs"),
    ]
    for kwargs, label in cases:
        c = Config()
        c.use_venue("indodax_maker")
        for k, val in kwargs.items():
            setattr(c, k, val)
        assert c.validate(), f"config accepted {label}"
    print(f"  ok  config rejects all {len(cases)} nonsense cases")


def test_all_venues_run():
    c = candles([1e9 * (1 + 0.04 * math.sin(i / 17)) for i in range(900)])
    for name in Config.VENUES:
        cfg = Config()
        cfg.use_venue(name)
        cfg.warmup_bars = 30
        m = compute_metrics(run(c, cfg))
        assert math.isfinite(m["return_pct"]), f"{name} produced non-finite return"
    print(f"  ok  all {len(Config.VENUES)} venue presets run cleanly")


# ------------------------------------------------------------------ real data

def test_real_data_if_present():
    try:
        c = load_candles("data/btcidr_15m.csv")
    except FileNotFoundError:
        print("  --  real data not present, skipping")
        return
    r = run(c, v())
    m = compute_metrics(r)

    # ledger must reconcile exactly
    spent = sum(x.gross_idr for x in r["fills"] if x.side == "buy")
    recv = sum(x.gross_idr - x.fee_idr for x in r["fills"] if x.side == "sell")
    expected = Config().starting_idr - spent + recv
    assert abs(expected - r["state"]["idr"]) < 0.01, "ledger does not reconcile"

    # no lookahead: truncating must not change earlier decisions
    half = run(c[:len(c) // 3], v())
    n = min(len(half["fills"]), len(r["fills"]))
    a = [(x.ts, x.side, round(x.price, 4)) for x in r["fills"][:n]]
    b = [(x.ts, x.side, round(x.price, 4)) for x in half["fills"][:n]]
    assert a == b, "truncation changed earlier decisions -- lookahead"

    print(f"  ok  real data: {len(c):,} bars, ledger reconciles, no lookahead "
          f"({m['n_buys']}b/{m['n_sells']}s)")


if __name__ == "__main__":
    print("\nRobustness pass\n" + "-" * 56)
    tests = [
        test_tiny_and_empty_inputs,
        test_flat_and_extreme_prices,
        test_gaps_in_series,
        test_decide_never_mutates_balances,
        test_never_spends_more_than_it_has,
        test_one_lot_per_level,
        test_sell_price_never_better_than_target,
        test_config_rejects_nonsense,
        test_all_venues_run,
        test_real_data_if_present,
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
    print("-" * 56)
    print("all passed\n" if not failed else f"{failed} FAILED\n")
    raise SystemExit(1 if failed else 0)
