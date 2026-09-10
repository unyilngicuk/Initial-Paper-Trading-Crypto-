"""
Tests for Unyil ORB (strategy_orb.py).

Run: python3 test_orb.py
"""

"""
Tests for Unyil ORB (strategy_orb.py).

Run: python3 test_orb.py
"""

import os
import shutil

# CRITICAL: this must run BEFORE `import engine`, not just before main().
# engine.py does `from strategy import Action, Lot` at ITS OWN module load
# time -- Python caches that binding permanently once the import happens.
# This must happen here, at true module top, before any engine-touching
# import below, or the sync silently does nothing.
_STRATEGY_PY_BACKUP = None
if os.path.exists("strategy.py"):
    with open("strategy.py") as _f:
        _STRATEGY_PY_BACKUP = _f.read()
shutil.copy("strategy_orb.py", "strategy.py")

import datetime

from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from strategy_orb import Lot, decide, new_state


def _restore_strategy_py():
    if _STRATEGY_PY_BACKUP is not None:
        with open("strategy.py", "w") as f:
            f.write(_STRATEGY_PY_BACKUP)


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.use_venue("indodax_maker")
    cfg.warmup_bars = 0
    cfg.orb_session_start_hour = 13
    cfg.orb_range_bars = 2
    cfg.orb_breakout_buffer_pct = 0.002
    cfg.orb_max_exposure = 1.00
    cfg.orb_stop_pct = 0.05
    cfg.orb_trail_pct = 0.08
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_candle(ts: int, price: float) -> dict:
    return {"ts": ts, "open": price, "high": price, "low": price,
            "close": price, "volume": 1.0}


DAY = 86400
BAR = 900


def run_series(prices_with_ts, cfg):
    state = new_state(cfg)
    fills = []
    for ts, price in prices_with_ts:
        candle = make_candle(ts, price)
        check_drawdown(state, price, cfg)
        for action in decide(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at ts {ts}: {err}"
    return state, fills


def _session(base_ts, session_hour, prices):
    dt = datetime.datetime.fromtimestamp(base_ts, tz=datetime.timezone.utc)
    session_start = int(datetime.datetime(dt.year, dt.month, dt.day, session_hour,
                                           tzinfo=datetime.timezone.utc).timestamp())
    return [(session_start + i * BAR, p) for i, p in enumerate(prices)]


def test_refuses_to_trade_without_session_hour_configured():
    cfg = make_cfg(orb_session_start_hour=None)
    series = _session(1_700_000_000, 13, [100, 100, 101, 102, 103, 110, 120])
    state, fills = run_series(series, cfg)
    assert fills == [], "traded despite no session hour configured"
    print("  ok  refuses to trade until orb_session_start_hour is explicitly set")


def test_tracks_opening_range_over_first_n_bars():
    cfg = make_cfg(orb_range_bars=2)
    series = _session(1_700_000_000, 13, [100.0, 102.0])
    state, fills = run_series(series, cfg)
    assert state["orb_high"] == 102.0
    assert state["orb_low"] == 100.0
    print("  ok  opening range correctly tracks the high/low of the first N bars")


def test_enters_on_a_clean_breakout():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.002)
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0])
    state, fills = run_series(series, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert buys, "never entered on a clean breakout above the range high"
    assert buys[0].price == 103.0
    print("  ok  enters on a clean break above the opening range high + buffer")


def test_does_not_enter_without_clearing_the_buffer():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.05)
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0])
    state, fills = run_series(series, cfg)
    assert not [f for f in fills if f.side == "buy"], "entered without clearing the buffer"
    print("  ok  does not enter on a move that doesn't clear the breakout buffer")


def test_only_one_breakout_attempt_per_session():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.002, orb_stop_pct=0.02)
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0, 100.0, 105.0, 110.0])
    state, fills = run_series(series, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert len(buys) == 1, f"expected exactly one entry attempt per session, got {len(buys)}"
    print("  ok  only one breakout attempt per session, even after a stop-out")


def test_new_session_resets_the_range_and_allows_a_new_attempt():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.002, orb_stop_pct=0.02)
    day1 = _session(1_700_000_000, 13, [100.0, 102.0, 103.0, 100.0])
    day2 = _session(1_700_000_000 + DAY, 13, [100.0, 102.0, 103.0])
    state, fills = run_series(day1 + day2, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert len(buys) == 2, f"expected a fresh attempt on day 2, got {len(buys)} total buys"
    print("  ok  a new session resets the range and allows a new breakout attempt")


def test_stop_loss_fires():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.002, orb_stop_pct=0.05)
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0, 97.0])
    state, fills = run_series(series, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert sells and "stop loss" in sells[0].reason
    print("  ok  stop-loss fires on an adverse move within the session")


def test_trailing_stop_locks_in_gains():
    cfg = make_cfg(orb_range_bars=2, orb_breakout_buffer_pct=0.002,
                    orb_stop_pct=0.30, orb_trail_pct=0.08)
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0, 120.0, 130.0, 119.0])
    state, fills = run_series(series, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert sells, "never exited"
    assert sells[0].price > 103.0 * 0.70, "exited at the hard stop instead of trailing"
    print(f"  ok  trailing stop exits above the hard stop, locking in gains "
          f"(exit {sells[0].price:.1f})")


def test_halt_blocks_new_entries_but_never_the_exit():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["halted"] = True
    state["current_session_date"] = (2026, 1, 1)
    state["bars_into_session"] = cfg.orb_range_bars
    state["orb_high"] = 100.0
    state["lots"] = [Lot(lot_id=1, level_idx=0, qty_coin=1.0, entry_price=100.0, entry_ts=0)]
    state["grid_coin"] = 1.0
    state["high_water_price"] = 100.0

    ts = int(datetime.datetime(2026, 1, 1, 13, tzinfo=datetime.timezone.utc).timestamp())
    candle = make_candle(ts, 90.0)
    actions = decide(state, candle, cfg)
    assert len(actions) == 1 and actions[0].side == "sell", (
        f"halt blocked a protective exit: {actions}"
    )
    print("  ok  halt never blocks the exit")

    state2 = new_state(cfg)
    state2["bars_seen"] = 100
    state2["halted"] = True
    state2["current_session_date"] = (2026, 1, 1)
    state2["bars_into_session"] = cfg.orb_range_bars
    state2["orb_high"] = 100.0
    candle2 = make_candle(ts, 200.0)
    assert decide(state2, candle2, cfg) == [], "entered a new position while halted"
    print("  ok  halt blocks new entries")


def test_decide_never_moves_money():
    cfg = make_cfg()
    series = _session(1_700_000_000, 13, [100.0, 102.0, 103.0, 110.0])
    state = new_state(cfg)
    before = None
    for ts, price in series:
        candle = make_candle(ts, price)
        decide(state, candle, cfg)
        if before is None:
            before = (state["idr"], state["grid_coin"], state["hold_coin"])
    after = (state["idr"], state["grid_coin"], state["hold_coin"])
    assert before == after, f"decide() moved money on its own: {before} -> {after}"
    print("  ok  decide() never moves money itself (only engine.apply_fill does)")


def test_profit_reserve_excluded_from_sizing():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["idr"] = 1_000_000
    state["profit_reserve"] = 400_000
    state["current_session_date"] = (2026, 1, 1)
    state["bars_into_session"] = cfg.orb_range_bars
    state["orb_high"] = 100.0

    ts = int(datetime.datetime(2026, 1, 1, 13, tzinfo=datetime.timezone.utc).timestamp())
    candle = make_candle(ts, 105.0)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "buy"
    investable = state["idr"] - state["profit_reserve"]
    assert actions[0].qty_idr <= investable * cfg.orb_max_exposure + 1
    print(f"  ok  sizing excludes the {state['profit_reserve']:,.0f} protected reserve")


def test_never_holds_more_than_one_position():
    import random
    cfg = make_cfg(orb_stop_pct=0.05, orb_trail_pct=0.08)
    random.seed(11)
    base_ts = 1_700_000_000
    series = []
    price = 100.0
    for day in range(30):
        day_prices = []
        p = price
        for i in range(28):
            p *= (1.0 + random.uniform(-0.02, 0.022))
            p = max(p, 1.0)
            day_prices.append(p)
        series.extend(_session(base_ts + day * DAY, 13, day_prices))
        price = day_prices[-1]

    state, fills = run_series(series, cfg)
    open_lots = 0
    max_open = 0
    for f in fills:
        open_lots += 1 if f.side == "buy" else -1
        max_open = max(max_open, open_lots)
    assert max_open <= 1, f"held {max_open} positions at once"
    assert state["idr"] >= -1e-6, f"cash went negative: {state['idr']}"
    print(f"  ok  never more than one position across 30 sessions ({len(fills)} fills)")


def test_config_rejects_nonsense():
    cases = [
        ("bad session hour", dict(orb_session_start_hour=25)),
        ("zero range bars", dict(orb_session_start_hour=13, orb_range_bars=0)),
        ("exposure above 100%", dict(orb_session_start_hour=13, orb_max_exposure=1.5)),
        ("stop pct zero", dict(orb_session_start_hour=13, orb_stop_pct=0.0)),
    ]
    for label, overrides in cases:
        cfg = Config()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        assert cfg.validate(), f"config accepted nonsense: {label} ({overrides})"
    print(f"  ok  config rejects all {len(cases)} nonsense ORB settings")


def main():
    try:
        print("\nVerifying Unyil ORB (WRAPPER family: Opening Range Breakout)")
        print("-" * 66)
        print(" distinctive behaviour:")
        test_refuses_to_trade_without_session_hour_configured()
        test_tracks_opening_range_over_first_n_bars()
        test_enters_on_a_clean_breakout()
        test_does_not_enter_without_clearing_the_buffer()
        test_only_one_breakout_attempt_per_session()
        test_new_session_resets_the_range_and_allows_a_new_attempt()
        test_stop_loss_fires()
        test_trailing_stop_locks_in_gains()
        print(" safety:")
        test_halt_blocks_new_entries_but_never_the_exit()
        test_decide_never_moves_money()
        test_profit_reserve_excluded_from_sizing()
        test_never_holds_more_than_one_position()
        test_config_rejects_nonsense()
        print("-" * 66)
        print("all passed\n")
    finally:
        _restore_strategy_py()


if __name__ == "__main__":
    main()
