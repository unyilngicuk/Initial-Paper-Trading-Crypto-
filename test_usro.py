"""
Tests for Usro (strategy_usro.py).

Run: python3 test_usro.py
"""

from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from strategy_usro import Lot, decide, new_state


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.use_venue("indodax_maker")
    cfg.usro_trend_period = 10   # small, so synthetic tests run fast
    cfg.warmup_bars = 5
    cfg.history_window = 200
    cfg.usro_entry_buffer_pct = 0.001
    cfg.usro_max_exposure = 1.00
    cfg.usro_trail_pct = 0.15
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_candle(ts: int, price: float) -> dict:
    return {"ts": ts, "open": price, "high": price, "low": price,
            "close": price, "volume": 1.0}


def run_series(prices, cfg, ts0=1_700_000_000, bar=900):
    state = new_state(cfg)
    fills = []
    for i, price in enumerate(prices):
        candle = make_candle(ts0 + i * bar, price)
        state["closes"].append(price)
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1
        check_drawdown(state, price, cfg)
        for action in decide(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"
    return state, fills


# ---------------------------------------------------------- distinctive behaviour

def test_enters_fast_with_minimal_buffer():
    cfg = make_cfg()
    flat = [100.0] * 15
    rise = [100.0 * (1.01 ** i) for i in range(1, 6)]
    state, fills = run_series(flat + rise, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert buys, "never entered despite a clear signal within a handful of bars"
    assert buys[0].gross_idr > cfg.starting_idr * 0.95, (
        f"entered with only {buys[0].gross_idr:,.0f} of {cfg.starting_idr:,.0f} "
        f"-- expected near-full commitment"
    )
    print(f"  ok  enters within a handful of bars, at {buys[0].gross_idr/cfg.starting_idr:.0%} exposure")


def test_survives_a_normal_pullback_that_would_shake_out_a_trend_exit():
    """
    The core design claim: a real ~8% pullback inside a continuing rally
    must NOT trigger an exit, because there is no trend-break rule to
    trigger it -- only the much wider trailing stop.
    """
    cfg = make_cfg(usro_trail_pct=0.15)
    flat = [100.0] * 15
    rise = [100.0 * (1.02 ** i) for i in range(1, 20)]
    peak = rise[-1]
    pullback = [peak * (1 - 0.008 * i) for i in range(1, 10)]  # ~8% pullback
    resume = [pullback[-1] * (1.02 ** i) for i in range(1, 15)]

    state, fills = run_series(flat + rise + pullback + resume, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert not sells, (
        f"exited during an 8% pullback -- the whole point of this strategy "
        f"is surviving exactly this, got {len(sells)} sell(s)"
    )
    print("  ok  holds through an 8% pullback that a trend-break exit would have triggered")


def test_trailing_stop_still_fires_on_a_genuine_reversal():
    cfg = make_cfg(usro_trail_pct=0.15)
    flat = [100.0] * 15
    rise = [100.0 * (1.02 ** i) for i in range(1, 25)]
    peak = rise[-1]
    crash = [peak * (0.97 ** i) for i in range(1, 15)]  # a real, deep reversal

    state, fills = run_series(flat + rise + crash, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert sells, "never exited despite a genuine deep reversal"
    assert "trailing stop" in sells[0].reason
    exit_price = sells[0].price
    assert exit_price < peak * 0.86, (
        f"exited too early relative to the 15% trail: exit {exit_price:.1f}, peak {peak:.1f}"
    )
    print(f"  ok  trailing stop fires on a genuine reversal (exited near {exit_price/peak:.0%} of peak)")


def test_no_trend_break_rule_exists_at_all():
    """
    Directly confirm there's no secondary exit condition: price falling
    below the trend average, while still comfortably above the trailing
    stop, must not exit.
    """
    cfg = make_cfg(usro_trail_pct=0.15)
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.usro_trend_period
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 120.0   # position has run up

    # Price now below the flat 100 trend average, but well within the 15%
    # trail from the 120 peak (stop sits at 102).
    candle = make_candle(1_700_000_000, 105.0)
    actions = decide(state, candle, cfg)
    assert actions == [], (
        f"exited on a trend-average dip despite being nowhere near the "
        f"trailing stop -- a trend-break rule leaked back in: {actions}"
    )
    print("  ok  no trend-break exit exists -- only the trailing stop can exit")


# ---------------------------------------------------------- safety carried over

def test_halt_blocks_new_entries_but_never_the_exit():
    cfg = make_cfg()
    state = new_state(cfg)
    state["halted"] = True
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.usro_trend_period
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    candle = make_candle(1_700_000_000, 80.0)   # past the 15% trail from entry
    actions = decide(state, candle, cfg)
    assert len(actions) == 1 and actions[0].side == "sell", (
        f"halt blocked the trailing stop -- safety violation: {actions}"
    )
    print("  ok  halt never blocks the trailing stop exit")

    state2 = new_state(cfg)
    state2["halted"] = True
    state2["bars_seen"] = 100
    state2["closes"] = [100.0] * (cfg.usro_trend_period - 1) + [50.0]
    blocked = decide(state2, make_candle(1_700_000_900, 500.0), cfg)
    assert blocked == [], f"entered a new position while halted: {blocked}"
    print("  ok  halt blocks new entries")


def test_decide_never_moves_money():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0 + i * 0.1 for i in range(cfg.usro_trend_period + 5)]
    before = (state["idr"], state["grid_coin"], state["hold_coin"])
    for i in range(50):
        decide(state, make_candle(1_700_000_000 + i * 900, 100.0 + i), cfg)
    after = (state["idr"], state["grid_coin"], state["hold_coin"])
    assert before == after, f"decide() moved money: {before} -> {after}"
    print("  ok  decide() never moves money itself (only engine.apply_fill does)")


def test_profit_reserve_is_excluded_from_sizing():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.usro_trend_period
    state["idr"] = 1_000_000
    state["profit_reserve"] = 400_000

    candle = make_candle(1_700_000_000, 200.0)  # clear entry signal
    state["closes"].append(200.0)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "buy"
    investable = state["idr"] - state["profit_reserve"]
    assert actions[0].qty_idr <= investable * cfg.usro_max_exposure + 1, (
        f"spend {actions[0].qty_idr:,.0f} exceeds exposure cap on "
        f"investable-minus-reserve ({investable:,.0f})"
    )
    print(f"  ok  sizing excludes the {state['profit_reserve']:,.0f} protected reserve")


def test_never_holds_more_than_one_position():
    import random
    cfg = make_cfg()
    random.seed(21)
    price = 100.0
    prices = []
    for _ in range(500):
        price *= (1.0 + random.uniform(-0.02, 0.02))
        prices.append(max(price, 1.0))
    state, fills = run_series(prices, cfg)
    open_lots = 0
    max_open = 0
    for f in fills:
        open_lots += 1 if f.side == "buy" else -1
        max_open = max(max_open, open_lots)
    assert max_open <= 1, f"held {max_open} positions at once"
    assert state["idr"] >= -1e-6, f"cash went negative: {state['idr']}"
    print(f"  ok  never more than one position across a 500-bar random walk ({len(fills)} fills)")


def test_config_rejects_nonsense():
    cases = [
        ("exposure above 100%", dict(usro_max_exposure=1.5)),
        ("exposure zero", dict(usro_max_exposure=0.0)),
        ("trail pct zero", dict(usro_trail_pct=0.0)),
        ("trail pct >= 1.0", dict(usro_trail_pct=1.0)),
        ("history shorter than trend period", dict(history_window=100, usro_trend_period=5000)),
    ]
    for label, overrides in cases:
        cfg = Config()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        assert cfg.validate(), f"config accepted nonsense: {label} ({overrides})"
    print(f"  ok  config rejects all {len(cases)} nonsense Usro settings")


def main():
    print("\nVerifying Usro (bull-market specialist)")
    print("-" * 62)
    print(" distinctive behaviour:")
    test_enters_fast_with_minimal_buffer()
    test_survives_a_normal_pullback_that_would_shake_out_a_trend_exit()
    test_trailing_stop_still_fires_on_a_genuine_reversal()
    test_no_trend_break_rule_exists_at_all()
    print(" safety carried over from Unyil 2.0 / Guardian:")
    test_halt_blocks_new_entries_but_never_the_exit()
    test_decide_never_moves_money()
    test_profit_reserve_is_excluded_from_sizing()
    test_never_holds_more_than_one_position()
    test_config_rejects_nonsense()
    print("-" * 62)
    print("all passed\n")


if __name__ == "__main__":
    main()
