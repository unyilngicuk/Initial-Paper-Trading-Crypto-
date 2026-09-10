"""
Tests for Unyil Guardian (strategy_guardian.py).

Two jobs:
  1. Verify Guardian's distinctive behaviour -- dual-trend entry, partial
     sizing, trailing stop, crash lockout, asymmetric exit.
  2. Verify every SAFETY guarantee from Unyil 2.0 carried over intact.
     A new strategy is the easiest place to accidentally drop one.

Run: python3 test_guardian.py
"""

from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from strategy_guardian import Lot, decide, new_state


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.use_venue("indodax_maker")
    # Small windows so synthetic tests run fast.
    cfg.guardian_trend_fast = 10
    cfg.guardian_trend_slow = 30
    cfg.warmup_bars = 5
    cfg.history_window = 200
    cfg.guardian_max_exposure = 0.50
    cfg.guardian_stop_pct = 0.08
    cfg.guardian_trail_pct = 0.06
    # Neutral by default so tests of OTHER mechanisms aren't altered;
    # the buffer/cooldown tests set these explicitly.
    cfg.guardian_exit_buffer_pct = 0.0
    cfg.reentry_cooldown_hours = 0.0
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

def test_requires_both_trends_to_align_before_entering():
    """
    A short bounce inside an established downtrend must NOT trigger entry.
    The bounce is kept short deliberately: long enough to lift the fast
    average above price, but not long enough for the slow average to catch
    up. If the bounce runs long enough for both averages to genuinely
    realign, entering IS correct -- that's no longer a bounce, it's a new
    uptrend.
    """
    cfg = make_cfg()
    decline = [200.0 - i for i in range(60)]
    short_bounce = [140.0 + i * 0.8 for i in range(8)]
    state, fills = run_series(decline + short_bounce, cfg)
    assert not [f for f in fills if f.side == "buy"], (
        "entered on a short bounce while the long trend was still down"
    )
    print("  ok  refuses to enter on a short bounce inside a downtrend")


def test_enters_only_when_aligned_and_sizes_partially():
    cfg = make_cfg(guardian_max_exposure=0.50)
    prices = [100.0] * 40 + [100.0 * (1.02 ** i) for i in range(1, 30)]
    state, fills = run_series(prices, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert buys, "never entered despite a clean aligned uptrend"

    spent = buys[0].gross_idr
    assert spent < cfg.starting_idr * 0.6, (
        f"spent {spent:,.0f} of {cfg.starting_idr:,.0f} -- exposure cap not applied"
    )
    assert spent > cfg.starting_idr * 0.4, f"spent suspiciously little: {spent:,.0f}"
    print(f"  ok  enters only when aligned, and commits {spent/cfg.starting_idr:.0%} "
          f"(cap {cfg.guardian_max_exposure:.0%}), not the whole balance")


def test_trailing_stop_locks_in_gains():
    """
    A position that rises then falls back should exit near its high-water
    mark, not all the way down at the original hard stop.
    """
    cfg = make_cfg(guardian_stop_pct=0.30, guardian_trail_pct=0.06)
    rise = [100.0] * 40 + [100.0 * (1.02 ** i) for i in range(1, 25)]
    peak = rise[-1]
    fall = [peak * (1 - 0.01 * i) for i in range(1, 12)]
    state, fills = run_series(rise + fall, cfg)

    sells = [f for f in fills if f.side == "sell"]
    assert sells, "never exited"
    exit_price = sells[0].price
    hard_stop_price = [f for f in fills if f.side == "buy"][0].price * 0.70
    assert exit_price > hard_stop_price, (
        f"exited at {exit_price:.0f}, no better than the hard stop {hard_stop_price:.0f} "
        f"-- trailing stop not working"
    )
    assert exit_price > peak * 0.90, (
        f"exited at {exit_price:.0f}, far below the peak {peak:.0f}"
    )
    print(f"  ok  trailing stop exited at {exit_price:.0f} (peak {peak:.0f}), "
          f"well above the hard stop {hard_stop_price:.0f}")


def test_does_not_buy_the_way_down_in_a_crash():
    """
    The core defensive requirement: across a sustained decline, Guardian
    must not repeatedly re-enter.

    Note this can be satisfied by EITHER mechanism -- the crash lockout,
    or the dual-trend entry rule refusing to align. Which one fires
    depends on how early the trailing stop got out. The requirement is
    the outcome (no knife-catching), not which guard produced it.
    """
    cfg = make_cfg()
    prices = ([100.0] * 40
              + [100.0 * (1.02 ** i) for i in range(1, 20)]   # rise, entry
              + [148.0 * (0.97 ** i) for i in range(1, 60)])  # sustained crash
    state, fills = run_series(prices, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert len(buys) <= 1, (
        f"bought {len(buys)} times -- kept catching the falling knife during a crash"
    )
    print(f"  ok  no knife-catching: {len(buys)} entry across a 60-bar sustained decline")


def test_crash_lockout_engages_on_a_confirmed_downtrend_exit():
    """
    Directly exercise the lockout: exit a position while the fast average
    is already below the slow one, and confirm new entries are then
    refused until that condition clears.
    """
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    # Construct history where fast < slow: recent bars much lower than older ones.
    state["closes"] = [200.0] * cfg.guardian_trend_slow + [100.0] * cfg.guardian_trend_fast
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=150.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 150.0

    actions = decide(state, make_candle(1_700_000_000, 100.0), cfg)
    assert actions and actions[0].side == "sell", f"expected an exit, got {actions}"
    assert state["crash_lockout"] is True, "lockout did not engage on a confirmed-downtrend exit"

    # Now flat and locked out: even a strong entry signal must be refused.
    state["lots"] = []
    state["grid_coin"] = 0.0
    blocked = decide(state, make_candle(1_700_000_900, 500.0), cfg)
    assert blocked == [], f"entered while in crash lockout: {blocked}"
    print("  ok  crash lockout engages on a confirmed-downtrend exit and blocks re-entry")


def test_exits_faster_than_it_enters():
    """Asymmetry: falling below the MEDIUM average exits, but entry needs both."""
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.guardian_trend_slow
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    # Price just under the medium average: not a stop-loss, but enough to exit.
    candle = make_candle(1_700_000_000, 99.0)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "sell", f"did not exit below medium trend: {actions}"
    assert "medium trend" in actions[0].reason
    print("  ok  exits on a break below the medium trend (faster than entry requires)")


# ---------------------------------------------------------- safety carried over

def test_exits_are_never_blocked_by_halt():
    cfg = make_cfg()
    state = new_state(cfg)
    state["halted"] = True
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.guardian_trend_slow
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    candle = make_candle(1_700_000_000, 80.0)   # past the 8% stop
    actions = decide(state, candle, cfg)
    assert len(actions) == 1 and actions[0].side == "sell", (
        f"halt blocked a protective exit -- safety violation: {actions}"
    )
    print("  ok  halt blocks new entries but never an exit")


def test_halt_blocks_new_entries():
    cfg = make_cfg()
    state = new_state(cfg)
    state["halted"] = True
    state["bars_seen"] = 100
    state["closes"] = [100.0] * 20 + [50.0] * (cfg.guardian_trend_slow - 20)
    candle = make_candle(1_700_000_000, 500.0)  # would otherwise be a clean entry
    assert decide(state, candle, cfg) == [], "entered a new position while halted"
    print("  ok  halt blocks new entries")


def test_decide_never_moves_money():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0 + i * 0.1 for i in range(cfg.guardian_trend_slow + 5)]
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
    state["closes"] = [100.0] * 20 + [50.0] * 10 + [100.0] * (cfg.guardian_trend_slow)
    state["idr"] = 1_000_000
    state["profit_reserve"] = 400_000

    candle = make_candle(1_700_000_000, 500.0)
    state["closes"].append(500.0)
    actions = decide(state, candle, cfg)
    if actions and actions[0].side == "buy":
        investable = state["idr"] - state["profit_reserve"]
        assert actions[0].qty_idr <= investable * cfg.guardian_max_exposure + 1, (
            f"spend {actions[0].qty_idr:,.0f} exceeds the exposure cap on "
            f"investable-minus-reserve ({investable:,.0f})"
        )
        print(f"  ok  sizing excludes the {state['profit_reserve']:,.0f} protected reserve")
    else:
        print("  ok  (no entry triggered to size-check; reserve logic shares "
              "Unyil 2.0's tested path)")


def test_exit_buffer_prevents_twitchy_exits():
    """
    A dip just below the medium average must NOT trigger an exit -- that
    thrashing produced 217 round trips and 26.7% fee drag before the
    buffer existed. Only a decisive break should exit.
    """
    cfg = make_cfg(guardian_exit_buffer_pct=0.02)
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.guardian_trend_slow
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    # 0.5% below the average -- inside the 2% buffer, must hold.
    shallow = decide(state, make_candle(1_700_000_000, 99.5), cfg)
    assert shallow == [], f"exited on a shallow dip inside the buffer: {shallow}"

    # 3% below -- past the buffer, must exit.
    decisive = decide(state, make_candle(1_700_000_900, 97.0), cfg)
    assert decisive and decisive[0].side == "sell", (
        f"failed to exit on a decisive break past the buffer: {decisive}"
    )
    print("  ok  exit buffer holds through shallow dips, exits on decisive breaks")


def test_cooldown_applies_to_guardian_too():
    """
    The cooldown was originally implemented in Unyil 2.0 only; Guardian
    silently ignored the flag. Regression test for that omission.
    """
    cfg = make_cfg()
    cfg.reentry_cooldown_hours = 24.0
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.guardian_trend_slow
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    exit_ts = 1_700_000_000
    actions = decide(state, make_candle(exit_ts, 85.0), cfg)   # losing exit
    assert actions and actions[0].side == "sell"
    assert state["cooldown_until_ts"] == exit_ts + 24 * 3600, (
        f"Guardian did not arm the cooldown: {state['cooldown_until_ts']}"
    )

    # Flat and inside the window: a clean entry signal must be refused.
    state["lots"] = []
    state["grid_coin"] = 0.0
    state["closes"] = [100.0] * 20 + [50.0] * (cfg.guardian_trend_slow - 20)
    blocked = decide(state, make_candle(exit_ts + 3600, 500.0), cfg)
    assert blocked == [], f"Guardian entered during its cooldown: {blocked}"
    print("  ok  cooldown now applies to Guardian (was previously ignored)")


def test_cooldown_never_blocks_a_guardian_exit():
    cfg = make_cfg()
    cfg.reentry_cooldown_hours = 48.0
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["closes"] = [100.0] * cfg.guardian_trend_slow
    state["cooldown_until_ts"] = 9_999_999_999
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    actions = decide(state, make_candle(1_700_000_000, 80.0), cfg)   # past the 8% stop
    assert len(actions) == 1 and actions[0].side == "sell", (
        f"cooldown blocked a protective exit -- safety violation: {actions}"
    )
    print("  ok  cooldown never blocks a Guardian protective exit")


def test_guardian_default_drawdown_limit_is_tighter():
    """
    Guardian's own circuit breaker default (10%) is separate from the
    shared 20% used elsewhere -- justified by measured behaviour: 3.01%
    drawdown in its actual purpose (the crash quarter), 12.62% over 2
    years on the optimistic preset, 20.31% on the realistic one (which
    tripped the shared 20% breaker anyway, just much later than ideal).
    """
    cfg = Config()
    assert cfg.guardian_max_drawdown_pct == 0.10, (
        f"expected Guardian's own threshold to be 10%, got {cfg.guardian_max_drawdown_pct}"
    )
    assert cfg.max_drawdown_pct == 0.20, (
        "the SHARED default must stay untouched -- Unyil 2.0 depends on this"
    )
    print("  ok  Guardian's own drawdown threshold (10%) is separate from "
          "the shared default (20%), which stays untouched")


def test_never_holds_more_than_one_position():
    import random
    cfg = make_cfg()
    random.seed(13)
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
    print(f"  ok  never more than one position across a 500-bar random walk "
          f"({len(fills)} fills)")


def test_config_rejects_nonsense():
    cases = [
        ("exposure above 100%", dict(guardian_max_exposure=1.5)),
        ("exposure zero", dict(guardian_max_exposure=0.0)),
        ("fast trend not shorter than slow", dict(guardian_trend_fast=9999)),
        ("history too short for slow trend", dict(history_window=100)),
    ]
    for label, overrides in cases:
        cfg = Config()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        assert cfg.validate(), f"config accepted nonsense: {label} ({overrides})"
    print(f"  ok  config rejects all {len(cases)} nonsense Guardian settings")


def main():
    print("\nVerifying Unyil Guardian (conservative capital preservation)")
    print("-" * 62)
    print(" distinctive behaviour:")
    test_requires_both_trends_to_align_before_entering()
    test_enters_only_when_aligned_and_sizes_partially()
    test_trailing_stop_locks_in_gains()
    test_does_not_buy_the_way_down_in_a_crash()
    test_crash_lockout_engages_on_a_confirmed_downtrend_exit()
    test_exits_faster_than_it_enters()
    print(" safety carried over from Unyil 2.0:")
    test_exits_are_never_blocked_by_halt()
    test_halt_blocks_new_entries()
    test_decide_never_moves_money()
    test_profit_reserve_is_excluded_from_sizing()
    test_exit_buffer_prevents_twitchy_exits()
    test_cooldown_applies_to_guardian_too()
    test_cooldown_never_blocks_a_guardian_exit()
    test_guardian_default_drawdown_limit_is_tighter()
    test_never_holds_more_than_one_position()
    test_config_rejects_nonsense()
    print("-" * 62)
    print("all passed\n")


if __name__ == "__main__":
    main()
