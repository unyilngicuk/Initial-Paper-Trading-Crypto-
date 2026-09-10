"""
Verification tests specific to Unyil 2.0 (strategy_unyil2.py).

The original test_harness.py / test_robustness.py assert grid-specific
behaviour ("grid cycled in chop", "one lot per level", etc.) that simply
does not apply to a trend-following strategy -- running them unmodified
against Unyil 2.0 would either fail meaninglessly or pass for the wrong
reasons. These tests check what actually matters for THIS strategy.

Run: python3 test_unyil2.py
(after copying strategy_unyil2.py over strategy.py, per the README workflow)
"""

import random

from config import Config
from engine import apply_fill, check_drawdown, equity, verify_balance
from strategy_unyil2 import Action, decide, new_state


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.use_venue("indodax_maker")
    cfg.trend_ma_period = 20      # small, so synthetic tests run fast
    cfg.warmup_bars = 5
    cfg.history_window = 200
    cfg.trend_buffer_pct = 0.01
    cfg.stop_loss_pct = 0.15
    cfg.base_risk_frac = 0.90
    cfg.min_risk_frac = 0.25
    cfg.loss_reduction_factor = 0.50
    cfg.win_recovery_factor = 1.50
    # Cooldown off by default in this helper, so tests of OTHER mechanisms
    # aren't silently altered by it. The cooldown tests set it explicitly.
    cfg.reentry_cooldown_hours = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_candle(ts: int, price: float) -> dict:
    return {"ts": ts, "open": price, "high": price, "low": price,
            "close": price, "volume": 1.0}


def run_series(prices, cfg):
    """Feed a price series through decide()+engine, return final state and fills."""
    state = new_state(cfg)
    fills = []
    for i, price in enumerate(prices):
        candle = make_candle(i, price)
        state["closes"].append(price)
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1
        check_drawdown(state, price, cfg)
        actions = decide(state, candle, cfg)
        for action in actions:
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"
    return state, fills


# ---------------------------------------------------------------- tests

def test_rides_a_trend_without_capping_the_winner():
    """
    The whole point of Unyil 2.0: unlike the grid (which sells at +1.5%),
    a sustained rise should be held, not chopped into small exits.
    """
    cfg = make_cfg()
    flat = [100.0] * 25                          # establish the trend baseline
    rally = [100.0 * (1.03 ** i) for i in range(1, 40)]  # steady climb
    prices = flat + rally

    state, fills = run_series(prices, cfg)
    buys = [f for f in fills if f.side == "buy"]
    sells = [f for f in fills if f.side == "sell"]

    assert len(buys) == 1, f"expected exactly one entry, got {len(buys)}"
    # Should still be holding partway through a 30-bar, +3%/bar rally --
    # a grid capped at +1.5% would have sold and re-bought many times by now.
    assert len(sells) == 0, (
        f"sold {len(sells)} times during a sustained rally -- "
        f"this strategy should ride the trend, not cap it"
    )
    print("  ok  rides a sustained rally without capping the winner "
          f"({len(buys)} buy, {len(sells)} sell during a 39-bar climb)")


def test_exits_on_trend_break():
    cfg = make_cfg()
    flat = [100.0] * 25
    rally = [100.0 * (1.02 ** i) for i in range(1, 15)]
    crash = [rally[-1] * (0.97 ** i) for i in range(1, 15)]  # slow-ish decline
    prices = flat + rally + crash

    state, fills = run_series(prices, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert len(sells) >= 1, "expected the trend-break exit to fire"
    assert "trend break" in sells[0].reason, f"unexpected exit reason: {sells[0].reason}"
    print("  ok  exits when price falls back below the trend line")


def test_stop_loss_fires_before_slow_trend_catches_up():
    cfg = make_cfg(stop_loss_pct=0.10)
    flat = [100.0] * 25
    rally = [100.0 * (1.02 ** i) for i in range(1, 10)]
    # Entry fires on the FIRST bar price clears trend+buffer -- early in the
    # rally, not at its peak. Use a crash steep enough (30%) to clear a 10%
    # stop from even the earliest plausible entry price (rally[0]).
    crash = [rally[0] * 0.70]
    prices = flat + rally + crash

    state, fills = run_series(prices, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert len(sells) == 1, f"expected exactly one exit, got {len(sells)}"
    assert "stop loss" in sells[0].reason, (
        f"expected the stop-loss to fire before the trend filter noticed, "
        f"got: {sells[0].reason}"
    )
    print("  ok  hard stop-loss fires on a sharp drop, without waiting on the slow trend")


def test_isolated_loss_does_not_shrink_sizing():
    """
    The actual bug found on real data: a single whipsaw loss (often the
    shakeout right before a real trend) was shrinking the size of the very
    next entry -- which then went on to be the big winner, at half size.
    An isolated loss should leave sizing untouched.
    """
    cfg = make_cfg(losses_before_shrink=2)
    state = new_state(cfg)
    start = state["risk_frac"]

    flat = [100.0] * 25
    up = [100.0 * (1.02 ** i) for i in range(1, 8)]
    down = [up[-1] * (0.97 ** i) for i in range(1, 20)]  # one losing round trip
    prices = flat + up + down

    state, fills = run_series(prices, cfg)
    assert state["consecutive_losses"] == 1
    assert state["risk_frac"] == start, (
        f"an isolated loss should not shrink sizing, "
        f"went from {start} to {state['risk_frac']}"
    )
    print(f"  ok  a single isolated loss leaves risk_frac unchanged at {start:.2f}")


def test_two_consecutive_losses_does_shrink_sizing():
    cfg = make_cfg(losses_before_shrink=2)
    state = new_state(cfg)
    start = state["risk_frac"]

    # Build two independent losing round trips, with a flat "basing" period
    # between them so the trend average has time to flatten before the
    # second entry signal -- otherwise a fast-moving short-window SMA
    # catches up to the price too quickly for a second clean trigger.
    flat = [100.0] * 25
    up1 = [100.0 * (1.02 ** i) for i in range(1, 8)]
    down1 = [up1[-1] * (0.97 ** i) for i in range(1, 20)]
    flat2 = [down1[-1]] * 25
    up2 = [down1[-1] * (1.02 ** i) for i in range(1, 8)]
    down2 = [up2[-1] * (0.97 ** i) for i in range(1, 20)]
    prices = flat + up1 + down1 + flat2 + up2 + down2

    state, fills = run_series(prices, cfg)
    assert state["consecutive_losses"] >= 2, (
        f"expected 2 consecutive losses, got {state['consecutive_losses']} "
        f"-- trade_history: {state['trade_history']}"
    )
    assert state["risk_frac"] < start, (
        f"two consecutive losses should shrink sizing, stayed at {state['risk_frac']}"
    )
    print(f"  ok  two consecutive losses shrinks risk_frac from {start:.2f} "
          f"to {state['risk_frac']:.2f}")


def test_a_win_resets_the_consecutive_loss_counter():
    cfg = make_cfg(losses_before_shrink=2)
    state = new_state(cfg)

    # loss, then a WIN, then another isolated loss -- should never shrink,
    # because the win in the middle resets the streak. The win needs an
    # explicit decline afterward to actually force its exit -- otherwise
    # the position just stays open and absorbs the next "leg" instead of
    # a genuine third, independent trade happening.
    flat = [100.0] * 25
    up1 = [100.0 * (1.02 ** i) for i in range(1, 8)]
    down1 = [up1[-1] * (0.97 ** i) for i in range(1, 20)]        # loss #1
    flat2 = [down1[-1]] * 25
    rally = [down1[-1] * (1.03 ** i) for i in range(1, 40)]       # a clear win
    crash_exit = [rally[-1] * (0.97 ** i) for i in range(1, 20)]  # force the win to actually close
    flat3 = [crash_exit[-1]] * 25
    up2 = [crash_exit[-1] * (1.02 ** i) for i in range(1, 8)]
    down2 = [up2[-1] * (0.97 ** i) for i in range(1, 20)]         # isolated loss again
    prices = flat + up1 + down1 + flat2 + rally + crash_exit + flat3 + up2 + down2

    state, fills = run_series(prices, cfg)
    assert len(state["trade_history"]) == 3, (
        f"expected 3 completed trades, got {len(state['trade_history'])}: "
        f"{state['trade_history']}"
    )
    assert state["consecutive_losses"] == 1, (
        f"expected the win to reset the streak, consecutive_losses="
        f"{state['consecutive_losses']} -- trade_history: {state['trade_history']}"
    )
    print("  ok  a win resets the consecutive-loss counter")


def test_risk_frac_still_recovers_after_a_win():
    cfg = make_cfg(base_risk_frac=0.90, min_risk_frac=0.20, losses_before_shrink=1)
    state2 = new_state(cfg)
    state2["risk_frac"] = cfg.min_risk_frac  # start as if already beaten down
    flat = [100.0] * 25
    rally = [100.0 * (1.03 ** i) for i in range(1, 15)]
    crash_but_still_profitable = [rally[-1] * (0.985 ** i) for i in range(1, 10)]
    prices2 = flat + rally + crash_but_still_profitable
    state2, fills2 = run_series(prices2, cfg)
    after_win = state2["risk_frac"]
    assert after_win > cfg.min_risk_frac, (
        f"risk_frac should recover after a win, stayed at {after_win}"
    )
    print(f"  ok  risk_frac recovered from {cfg.min_risk_frac:.2f} toward "
          f"{after_win:.2f} after a win")


def test_halt_blocks_new_entries_but_not_exits():
    cfg = make_cfg()

    # Case A: halted, no position, entry condition is met -- must NOT buy.
    state = new_state(cfg)
    state["halted"] = True
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["bars_seen"] = cfg.trend_ma_period
    candle = make_candle(9999, 200.0)  # far above trend -- would trigger entry
    actions = decide(state, candle, cfg)
    assert actions == [], f"expected no new entry while halted, got {actions}"
    print("  ok  halt blocks a new entry")

    # Case B: halted, WITH an open position whose stop-loss is breached --
    # the exit must still fire. Halting stops new risk, not risk-reduction.
    from strategy_unyil2 import Lot
    state2 = new_state(cfg)
    state2["halted"] = True
    state2["closes"] = [100.0] * cfg.trend_ma_period
    state2["bars_seen"] = cfg.trend_ma_period
    state2["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state2["grid_coin"] = 1.0
    candle2 = make_candle(9999, 80.0)  # well past a 15% stop
    actions2 = decide(state2, candle2, cfg)
    assert len(actions2) == 1 and actions2[0].side == "sell", (
        f"expected the stop-loss exit to fire even while halted, got {actions2}"
    )
    print("  ok  halt does NOT block an existing position's protective exit")


def test_decide_never_mutates_wallet_balances():
    cfg = make_cfg()
    state = new_state(cfg)
    state["closes"] = [100.0 + i * 0.01 for i in range(cfg.trend_ma_period + 5)]
    state["bars_seen"] = cfg.trend_ma_period + 5
    before = (state["idr"], state["grid_coin"], state["hold_coin"])
    for i in range(50):
        decide(state, make_candle(i, 100.0 + i), cfg)
    after = (state["idr"], state["grid_coin"], state["hold_coin"])
    assert before == after, f"decide() mutated wallet balances: {before} -> {after}"
    print("  ok  decide() never moves money itself (only engine.apply_fill does)")


def test_period_close_liquidates_open_position():
    """
    At a withdrawal_period_days boundary, an open position must be closed
    -- profit can't be protected while it's still an at-risk coin position.
    """
    cfg = make_cfg(withdrawal_period_days=2)  # short period so the test runs fast
    state = new_state(cfg)
    real_ts0 = 1_700_000_000
    bar_seconds = 900

    flat = [100.0] * 25
    up = [100.0 * (1.02 ** i) for i in range(1, 15)]  # trigger and hold an entry
    prices = flat + up

    fills = []
    for i, price in enumerate(prices):
        candle = {"ts": real_ts0 + i * bar_seconds, "open": price, "high": price,
                  "low": price, "close": price, "volume": 1.0}
        state["closes"].append(price)
        state["bars_seen"] += 1
        actions = decide(state, candle, cfg)
        for action in actions:
            from engine import apply_fill
            apply_fill(state, action, candle, cfg, fills)

    assert len(state["lots"]) == 1, "expected an open position going into the period boundary"

    # Now advance time past the 2-day boundary without any price signal change.
    boundary_ts = real_ts0 + len(prices) * bar_seconds + int(2.1 * 86400)
    candle = {"ts": boundary_ts, "open": prices[-1], "high": prices[-1],
              "low": prices[-1], "close": prices[-1], "volume": 1.0}
    state["closes"].append(prices[-1])
    state["bars_seen"] += 1
    actions = decide(state, candle, cfg)

    assert len(actions) == 1 and actions[0].side == "sell", (
        f"expected the period-close to liquidate the open position, got {actions}"
    )
    assert "period close" in actions[0].reason
    print("  ok  an open position is closed at the withdrawal period boundary")


def test_profit_reserve_protects_money_from_future_entries():
    """
    Once flat past the boundary, profit above principal should move into
    a reserve that future entry sizing excludes.
    """
    from engine import apply_fill

    cfg = make_cfg(withdrawal_period_days=2)
    state = new_state(cfg)
    real_ts0 = 1_700_000_000
    bar_seconds = 900

    flat = [100.0] * 25
    up = [100.0 * (1.02 ** i) for i in range(1, 15)]
    down_to_flat = [up[-1]] * 5  # settle so no further trend action interferes
    prices = flat + up + down_to_flat

    fills = []
    for i, price in enumerate(prices):
        candle = {"ts": real_ts0 + i * bar_seconds, "open": price, "high": price,
                  "low": price, "close": price, "volume": 1.0}
        state["closes"].append(price)
        state["bars_seen"] += 1
        actions = decide(state, candle, cfg)
        for action in actions:
            apply_fill(state, action, candle, cfg, fills)

    # Force the period-close by jumping time forward, then feed one more
    # bar so the "already flat" sweep branch actually runs.
    close_ts = real_ts0 + len(prices) * bar_seconds + int(2.1 * 86400)
    price = prices[-1]
    candle = {"ts": close_ts, "open": price, "high": price, "low": price,
              "close": price, "volume": 1.0}
    state["closes"].append(price)
    state["bars_seen"] += 1
    actions = decide(state, candle, cfg)
    for action in actions:
        apply_fill(state, action, candle, cfg, fills)
    assert not state["lots"], "should be flat after the period-close sell"

    idr_before_sweep = state["idr"]
    reserve_before = state["profit_reserve"]

    # One more bar, same price, same timestamp region -- this is where the
    # "already flat" branch sweeps the overage into the reserve.
    candle2 = {"ts": close_ts + bar_seconds, "open": price, "high": price,
               "low": price, "close": price, "volume": 1.0}
    state["closes"].append(price)
    state["bars_seen"] += 1
    decide(state, candle2, cfg)

    assert state["profit_reserve"] > reserve_before, (
        f"expected profit to be swept into the reserve, stayed at {reserve_before}"
    )
    print(f"  ok  profit swept into reserve: {state['profit_reserve']:,.0f} IDR protected")

    # Now confirm a future entry's spend excludes the reserve.
    state["period_start_ts"] = candle2["ts"]  # start a fresh period
    rally_price = price * 1.5  # clear trend entry signal
    candle3 = {"ts": candle2["ts"] + bar_seconds, "open": rally_price, "high": rally_price,
               "low": rally_price, "close": rally_price, "volume": 1.0}
    state["closes"].append(rally_price)
    state["bars_seen"] += 1
    actions = decide(state, candle3, cfg)
    if actions and actions[0].side == "buy":
        max_possible_without_protection = state["idr"] * cfg.base_risk_frac
        assert actions[0].qty_idr < max_possible_without_protection, (
            "entry sizing should exclude the protected reserve, but didn't"
        )
        print(f"  ok  new entry sizing ({actions[0].qty_idr:,.0f} IDR) "
              f"correctly excludes the {state['profit_reserve']:,.0f} IDR reserve")
    else:
        print("  ok  (no new entry triggered on this bar to check sizing against -- "
              "reserve mechanism itself already confirmed above)")


def test_default_config_disables_loss_shrinking():
    """
    The shipped default sets min_risk_frac == base_risk_frac, which
    effectively disables loss-based sizing reduction. This is deliberate
    (real-data testing showed the shrinking undersized the biggest
    winners), but it's a significant behavioural default, so assert it
    explicitly rather than leaving it implicit.

    The shrinking MECHANISM still works and is tested above -- it's just
    switched off out of the box. Lower min_risk_frac to re-enable it.
    """
    cfg = Config()
    assert cfg.min_risk_frac == cfg.base_risk_frac, (
        f"expected defaults to disable shrinking (min == base), got "
        f"min={cfg.min_risk_frac} base={cfg.base_risk_frac}"
    )
    assert cfg.base_risk_frac == 1.00, f"expected full commitment default, got {cfg.base_risk_frac}"
    assert cfg.trend_buffer_pct == 0.003, f"expected 0.3% buffer default, got {cfg.trend_buffer_pct}"

    # And confirm the halt/stop safety defaults were NOT loosened alongside it.
    assert cfg.stop_loss_pct == 0.15, "stop-loss default changed unexpectedly"
    assert cfg.max_drawdown_pct == 0.20, "drawdown circuit breaker default changed unexpectedly"
    assert cfg.reentry_cooldown_hours == 0.0, (
        "re-entry cooldown should ship disabled -- testing showed it gained only "
        "0.58 pts at 12h and lost 2.9 pts at 48h"
    )
    assert cfg.adaptive_sizing_enabled is False, (
        "bounded adaptive sizing should ship disabled -- testing showed it cost "
        "15.6 pts of return for 2.2 pts less drawdown"
    )
    print("  ok  shipped defaults: full commitment, 0.3% buffer, shrinking off, "
          "cooldown off, adaptive off, stop-loss and circuit breaker unchanged")


def test_adaptive_sizing_is_off_by_default():
    cfg = Config()
    assert cfg.adaptive_sizing_enabled is False, (
        "bounded adaptive sizing must ship OFF until proven to help"
    )
    state = new_state(cfg)
    assert state["adaptive_frac"] == 1.0
    print("  ok  bounded adaptive sizing ships disabled by default")


def test_adaptive_sizing_respects_minimum_sample():
    """Constraint 3: no adjustment at all below adaptive_min_trades."""
    from strategy_unyil2 import _update_adaptive_sizing
    cfg = make_cfg()
    cfg.adaptive_sizing_enabled = True
    cfg.adaptive_min_trades = 10
    state = new_state(cfg)

    # 9 straight losses -- still below the sample threshold, so no change.
    state["trade_history"] = [{"ts": i, "pnl_pct": -0.02, "won": False} for i in range(9)]
    _update_adaptive_sizing(state, cfg, 999)
    assert state["adaptive_frac"] == 1.0, (
        f"adjusted on only 9 trades despite min of 10 (got {state['adaptive_frac']})"
    )
    assert state["adaptive_log"] == []
    print("  ok  no adjustment below the minimum trade sample (9 losses, still unchanged)")


def test_adaptive_sizing_respects_floor_and_ceiling():
    """Constraint 2: cannot spiral in either direction."""
    from strategy_unyil2 import _update_adaptive_sizing
    cfg = make_cfg()
    cfg.adaptive_sizing_enabled = True
    cfg.adaptive_min_trades = 10
    cfg.adaptive_floor = 0.50
    cfg.adaptive_ceiling = 1.00

    # Relentless losses -- must stop exactly at the floor, never below.
    state = new_state(cfg)
    state["trade_history"] = [{"ts": i, "pnl_pct": -0.02, "won": False} for i in range(50)]
    for i in range(50):
        _update_adaptive_sizing(state, cfg, 1000 + i)
    assert state["adaptive_frac"] == cfg.adaptive_floor, (
        f"expected to stop at floor {cfg.adaptive_floor}, got {state['adaptive_frac']}"
    )

    # Relentless wins -- must stop exactly at the ceiling, never above.
    state2 = new_state(cfg)
    state2["trade_history"] = [{"ts": i, "pnl_pct": 0.05, "won": True} for i in range(50)]
    for i in range(50):
        _update_adaptive_sizing(state2, cfg, 2000 + i)
    assert state2["adaptive_frac"] == cfg.adaptive_ceiling, (
        f"expected to stop at ceiling {cfg.adaptive_ceiling}, got {state2['adaptive_frac']}"
    )
    print(f"  ok  bounded hard at floor {cfg.adaptive_floor:.2f} and "
          f"ceiling {cfg.adaptive_ceiling:.2f}, cannot spiral")


def test_adaptive_sizing_never_touches_entry_or_exit_rules():
    """
    Constraint 1 -- the most important one. Adaptive sizing must change
    only HOW MUCH is traded, never WHEN. Verified by confirming the config
    values that govern entry/exit are untouched after many adjustments.
    """
    from strategy_unyil2 import _update_adaptive_sizing
    cfg = make_cfg()
    cfg.adaptive_sizing_enabled = True
    cfg.adaptive_min_trades = 10

    before = (cfg.trend_ma_period, cfg.trend_buffer_pct, cfg.stop_loss_pct,
              cfg.withdrawal_period_days, cfg.max_drawdown_pct)

    state = new_state(cfg)
    state["trade_history"] = [{"ts": i, "pnl_pct": -0.02, "won": False} for i in range(30)]
    for i in range(30):
        _update_adaptive_sizing(state, cfg, 3000 + i)

    after = (cfg.trend_ma_period, cfg.trend_buffer_pct, cfg.stop_loss_pct,
             cfg.withdrawal_period_days, cfg.max_drawdown_pct)
    assert before == after, (
        f"adaptive sizing modified entry/exit/safety rules: {before} -> {after}"
    )
    assert state["adaptive_frac"] < 1.0, "sanity: sizing should have moved"
    print("  ok  adjusts size only -- entry, exit, stop and halt rules untouched")


def test_adaptive_sizing_logs_every_adjustment():
    """Constraint 5: every change is auditable."""
    from strategy_unyil2 import _update_adaptive_sizing
    cfg = make_cfg()
    cfg.adaptive_sizing_enabled = True
    cfg.adaptive_min_trades = 10
    state = new_state(cfg)
    state["trade_history"] = [{"ts": i, "pnl_pct": -0.02, "won": False} for i in range(20)]

    _update_adaptive_sizing(state, cfg, 4242)
    assert len(state["adaptive_log"]) == 1
    entry = state["adaptive_log"][0]
    for field in ("ts", "win_rate", "window_size", "from", "to", "reason"):
        assert field in entry, f"audit log missing '{field}': {entry}"
    print(f"  ok  every adjustment logged with a reason: \"{entry['reason']}\"")


def test_adaptive_config_rejects_nonsense():
    """Bad adaptive settings must be caught by config validation."""
    bad_cases = [
        ("floor above ceiling", dict(adaptive_floor=0.9, adaptive_ceiling=0.5)),
        ("inverted win-rate band", dict(adaptive_low_winrate=0.8, adaptive_high_winrate=0.2)),
        ("min_trades too small", dict(adaptive_min_trades=2)),
        ("step out of range", dict(adaptive_step=0.9)),
    ]
    for label, overrides in bad_cases:
        cfg = Config()
        cfg.adaptive_sizing_enabled = True
        for k, v in overrides.items():
            setattr(cfg, k, v)
        problems = cfg.validate()
        assert problems, f"config validation accepted nonsense: {label} ({overrides})"
    print(f"  ok  config rejects all {len(bad_cases)} nonsense adaptive settings")


def test_cooldown_blocks_reentry_after_a_loss():
    """
    After a LOSING exit, new entries are blocked for the cooldown window.
    Motivated by real whipsaw clusters (4 losing round trips in 48h).
    """
    from strategy_unyil2 import Lot
    cfg = make_cfg()
    cfg.reentry_cooldown_hours = 12.0

    state = new_state(cfg)
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["bars_seen"] = cfg.trend_ma_period
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0

    # Force a LOSING trend-break exit.
    exit_ts = 1_700_000_000
    candle = make_candle(exit_ts, 90.0)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "sell", f"expected a losing exit, got {actions}"
    assert state["cooldown_until_ts"] == exit_ts + int(12 * 3600), (
        f"cooldown not armed correctly: {state['cooldown_until_ts']}"
    )

    # Clear the position, then try to enter well inside the cooldown window.
    state["lots"] = []
    state["grid_coin"] = 0.0
    inside = make_candle(exit_ts + 3600, 200.0)   # 1h later, strong entry signal
    state["closes"].append(200.0)
    assert decide(state, inside, cfg) == [], "entered during the cooldown window"

    # And confirm it CAN enter once the window expires.
    after = make_candle(exit_ts + int(13 * 3600), 200.0)
    state["closes"].append(200.0)
    acts = decide(state, after, cfg)
    assert acts and acts[0].side == "buy", f"failed to re-enter after cooldown expired: {acts}"
    print("  ok  cooldown blocks re-entry for 12h after a loss, then releases")


def test_cooldown_does_not_apply_after_a_win():
    """A winning exit means the trend was real -- no reason to sit out."""
    from strategy_unyil2 import Lot
    cfg = make_cfg()
    cfg.reentry_cooldown_hours = 12.0

    state = new_state(cfg)
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["bars_seen"] = cfg.trend_ma_period
    # Entry well below current price, so the exit books a PROFIT.
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=50.0, entry_ts=0)]
    state["grid_coin"] = 1.0

    exit_ts = 1_700_000_000
    candle = make_candle(exit_ts, 90.0)   # below trend -> exits, but at a gain vs 50
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "sell"
    assert state["cooldown_until_ts"] is None, (
        f"cooldown should not arm after a winning exit, got {state['cooldown_until_ts']}"
    )
    print("  ok  no cooldown after a winning exit")


def test_cooldown_never_blocks_an_exit():
    """
    Safety invariant: the cooldown restricts NEW risk only. It must never
    prevent an open position from honouring its stop-loss.
    """
    from strategy_unyil2 import Lot
    cfg = make_cfg()
    cfg.reentry_cooldown_hours = 48.0

    state = new_state(cfg)
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["bars_seen"] = cfg.trend_ma_period
    state["cooldown_until_ts"] = 9_999_999_999   # deep in cooldown
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0

    candle = make_candle(1_700_000_000, 70.0)   # well past a 15% stop
    actions = decide(state, candle, cfg)
    assert len(actions) == 1 and actions[0].side == "sell", (
        f"cooldown blocked a protective exit -- safety violation: {actions}"
    )
    print("  ok  cooldown never blocks a protective exit (stop-loss still fires)")


def test_regime_risk_multiplier_is_a_true_noop_by_default():
    """
    The optional external sizing hook must default to 1.0 (no effect).
    This strategy's own logic must be completely unaffected unless
    something external explicitly drives this field.
    """
    cfg = make_cfg()
    state = new_state(cfg)
    assert state["regime_risk_multiplier"] == 1.0
    flat = [100.0] * 25
    up = [100.0 * (1.02 ** i) for i in range(1, 10)]
    state_baseline, fills_baseline = run_series(flat + up, cfg)

    # Explicitly re-confirm the multiplier stayed 1.0 throughout -- nothing
    # in decide() should ever change it on its own.
    assert state_baseline["regime_risk_multiplier"] == 1.0
    print("  ok  regime_risk_multiplier defaults to 1.0 and is never changed internally")


def test_regime_risk_multiplier_actually_reduces_entry_size_when_driven_externally():
    cfg = make_cfg()
    state = new_state(cfg)
    state["regime_risk_multiplier"] = 0.5  # simulate an external harness cutting size

    flat = [100.0] * 25
    up = [100.0 * (1.02 ** i) for i in range(1, 10)]
    fills = []
    for i, price in enumerate(flat + up):
        candle = make_candle(i, price)
        state["closes"].append(price)
        state["bars_seen"] += 1
        for action in decide(state, candle, cfg):
            from engine import apply_fill
            apply_fill(state, action, candle, cfg, fills)

    buys = [f for f in fills if f.side == "buy"]
    assert buys, "never entered"
    expected_max_without_cut = cfg.starting_idr * cfg.base_risk_frac
    assert buys[0].gross_idr < expected_max_without_cut * 0.6, (
        f"expected the 0.5 multiplier to meaningfully cut spend, "
        f"got {buys[0].gross_idr:,.0f} vs uncut max {expected_max_without_cut:,.0f}"
    )
    print(f"  ok  regime_risk_multiplier=0.5 correctly cuts entry spend "
          f"({buys[0].gross_idr:,.0f} vs uncut {expected_max_without_cut:,.0f})")


def test_stop_pct_override_is_a_true_noop_by_default():
    from strategy_unyil2 import Lot
    cfg = make_cfg()
    state = new_state(cfg)
    assert state["stop_pct_override"] is None

    # A stable, flat trend baseline, with an entry price set deliberately
    # ABOVE it -- simulating "we bought during a rally, price has since
    # pulled back toward (but not to) the lagging trend average." This
    # isolates the stop-loss check from the separate trend-break check,
    # which a fresh, organically-discovered entry can't do (right after
    # any real entry, price and trend are still nearly identical).
    state["bars_seen"] = cfg.trend_ma_period
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=120.0, entry_ts=0)]
    state["grid_coin"] = 1.0

    # -3% from entry (116.4): inside the normal 15% stop, and well above
    # the trend-break threshold (~99), so neither exit should fire.
    dip_price = 120.0 * 0.97
    candle = make_candle(cfg.trend_ma_period, dip_price)
    actions = decide(state, candle, cfg)
    assert actions == [], f"exited at only -3% with the default 15% stop: {actions}"
    print("  ok  stop_pct_override defaults to None -- normal 15% stop applies unchanged")


def test_stop_pct_override_actually_tightens_the_exit():
    from strategy_unyil2 import Lot
    cfg = make_cfg(stop_loss_pct=0.15)
    state = new_state(cfg)
    state["stop_pct_override"] = 0.08  # simulate an external harness tightening it

    state["bars_seen"] = cfg.trend_ma_period
    state["closes"] = [100.0] * cfg.trend_ma_period
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=120.0, entry_ts=0)]
    state["grid_coin"] = 1.0

    # -10% from entry (108): inside the normal 15% stop (needs -15%, i.e.
    # 102), past the tightened 8% override (needs -8%, i.e. 110.4), and
    # still well above the trend-break threshold (~99) -- isolates the
    # stop-loss check specifically.
    dip_price = 120.0 * 0.90
    candle = make_candle(cfg.trend_ma_period, dip_price)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "sell", (
        f"expected the tightened 8% stop to fire at -10%, got {actions}"
    )
    assert "tightened stop loss" in actions[0].reason
    print("  ok  stop_pct_override correctly tightens the exit (fired at -10%, "
          f"normal stop would have needed -15%)")


def test_never_holds_more_than_one_position():
    cfg = make_cfg()
    random.seed(7)
    price = 100.0
    prices = []
    for _ in range(400):
        price *= (1.0 + random.uniform(-0.02, 0.02))
        price = max(price, 1.0)
        prices.append(price)

    state, fills = run_series(prices, cfg)
    max_lots_seen = 0
    lots_open = 0
    for f in fills:
        lots_open += 1 if f.side == "buy" else -1
        max_lots_seen = max(max_lots_seen, lots_open)
    assert max_lots_seen <= 1, f"held {max_lots_seen} positions at once, expected at most 1"
    assert state["idr"] >= -1e-6, f"cash went negative: {state['idr']}"
    print(f"  ok  never holds more than one position across a 400-bar random walk "
          f"({len(fills)} fills, final cash {state['idr']:,.0f})")


def main():
    print("\nVerifying Unyil 2.0 (trend-following + adaptive sizing)")
    print("-" * 60)
    test_rides_a_trend_without_capping_the_winner()
    test_exits_on_trend_break()
    test_stop_loss_fires_before_slow_trend_catches_up()
    test_isolated_loss_does_not_shrink_sizing()
    test_two_consecutive_losses_does_shrink_sizing()
    test_a_win_resets_the_consecutive_loss_counter()
    test_risk_frac_still_recovers_after_a_win()
    test_period_close_liquidates_open_position()
    test_profit_reserve_protects_money_from_future_entries()
    test_halt_blocks_new_entries_but_not_exits()
    test_decide_never_mutates_wallet_balances()
    test_default_config_disables_loss_shrinking()
    test_adaptive_sizing_is_off_by_default()
    test_adaptive_sizing_respects_minimum_sample()
    test_adaptive_sizing_respects_floor_and_ceiling()
    test_adaptive_sizing_never_touches_entry_or_exit_rules()
    test_adaptive_sizing_logs_every_adjustment()
    test_adaptive_config_rejects_nonsense()
    test_cooldown_blocks_reentry_after_a_loss()
    test_cooldown_does_not_apply_after_a_win()
    test_cooldown_never_blocks_an_exit()
    test_regime_risk_multiplier_is_a_true_noop_by_default()
    test_regime_risk_multiplier_actually_reduces_entry_size_when_driven_externally()
    test_stop_pct_override_is_a_true_noop_by_default()
    test_stop_pct_override_actually_tightens_the_exit()
    test_never_holds_more_than_one_position()
    print("-" * 60)
    print("all passed\n")


if __name__ == "__main__":
    main()
