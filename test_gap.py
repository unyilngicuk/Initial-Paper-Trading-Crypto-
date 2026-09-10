"""
Tests for Unyil GAP (strategy_gap.py) -- both modes plus safety.

Run: python3 test_gap.py
"""

import os
import shutil

# CRITICAL: this must run BEFORE `import engine`, not just before main().
# engine.py does `from strategy import Action, Lot` at ITS OWN module load
# time -- Python caches that binding permanently once the import happens.
# Syncing strategy.py inside main() (after engine is already imported)
# does nothing; the stale Lot class is already bound. This must happen
# here, at true module top, before any engine-touching import below.
_STRATEGY_PY_BACKUP = None
if os.path.exists("strategy.py"):
    with open("strategy.py") as _f:
        _STRATEGY_PY_BACKUP = _f.read()
shutil.copy("strategy_gap.py", "strategy.py")

import datetime

from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from strategy_gap import Lot, decide, new_state


def _restore_strategy_py():
    if _STRATEGY_PY_BACKUP is not None:
        with open("strategy.py", "w") as f:
            f.write(_STRATEGY_PY_BACKUP)


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.use_venue("indodax_maker")
    cfg.warmup_bars = 0
    cfg.orb_session_start_hour = 13
    cfg.gap_mode = "go"
    cfg.gap_min_pct = 0.01
    cfg.gap_max_exposure = 1.00
    cfg.gap_stop_pct = 0.05
    cfg.gap_trail_pct = 0.08
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


def _pre_and_session(base_ts, session_hour, pre_price, session_prices):
    """One bar right before the session (the 'pre-session close'), then
    the session's own bars starting exactly at session_hour."""
    dt = datetime.datetime.fromtimestamp(base_ts, tz=datetime.timezone.utc)
    session_start = int(datetime.datetime(dt.year, dt.month, dt.day, session_hour,
                                           tzinfo=datetime.timezone.utc).timestamp())
    out = [(session_start - BAR, pre_price)]
    out += [(session_start + i * BAR, p) for i, p in enumerate(session_prices)]
    return out


def test_go_mode_enters_on_an_up_gap():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 102.0])  # +2% gap
    state, fills = run_series(series, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert buys, "go mode never entered a clean up-gap"
    print("  ok  go mode enters on an up-gap clearing the minimum threshold")


def test_go_mode_ignores_a_down_gap():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [97.0, 97.0])  # -3% gap
    state, fills = run_series(series, cfg)
    assert not [f for f in fills if f.side == "buy"], (
        "go mode entered a down-gap -- would require shorting to bet continuation"
    )
    print("  ok  go mode never enters a down-gap (would require shorting)")


def test_fade_mode_enters_on_a_down_gap():
    cfg = make_cfg(gap_mode="fade", gap_min_pct=0.01)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [97.0, 97.0])  # -3% gap
    state, fills = run_series(series, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert buys, "fade mode never entered a clean down-gap"
    print("  ok  fade mode enters on a down-gap clearing the minimum threshold")


def test_fade_mode_ignores_an_up_gap():
    cfg = make_cfg(gap_mode="fade", gap_min_pct=0.01)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 102.0])  # +2% gap
    state, fills = run_series(series, cfg)
    assert not [f for f in fills if f.side == "buy"], (
        "fade mode entered an up-gap -- would require shorting to bet reversal"
    )
    print("  ok  fade mode never enters an up-gap (would require shorting)")


def test_gap_below_minimum_threshold_is_ignored():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.05)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [101.0, 101.0])  # only +1%
    state, fills = run_series(series, cfg)
    assert not [f for f in fills if f.side == "buy"], "entered on a sub-threshold gap"
    print("  ok  a gap below gap_min_pct is correctly ignored")


def test_only_one_gap_evaluation_per_session():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01, gap_stop_pct=0.02)
    day1 = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 98.0, 105.0, 110.0])
    state, fills = run_series(day1, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert len(buys) == 1, f"expected exactly one entry attempt per session, got {len(buys)}"
    print("  ok  only one gap evaluation per session, even after a stop-out")


def test_new_session_allows_a_fresh_evaluation():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01, gap_stop_pct=0.02)
    day1 = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 98.0])
    day2 = _pre_and_session(1_700_000_000 + DAY, 13, 98.0, [100.0, 100.0])
    state, fills = run_series(day1 + day2, cfg)
    buys = [f for f in fills if f.side == "buy"]
    assert len(buys) == 2, f"expected a fresh attempt on day 2, got {len(buys)} total buys"
    print("  ok  a new session allows a fresh gap evaluation")


def test_stop_loss_fires():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01, gap_stop_pct=0.05)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 96.0])
    state, fills = run_series(series, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert sells and "stop loss" in sells[0].reason
    print("  ok  stop-loss fires on an adverse move after a gap entry")


def test_trailing_stop_locks_in_gains():
    cfg = make_cfg(gap_mode="go", gap_min_pct=0.01, gap_stop_pct=0.30, gap_trail_pct=0.08)
    series = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 120.0, 130.0, 119.0])
    state, fills = run_series(series, cfg)
    sells = [f for f in fills if f.side == "sell"]
    assert sells, "never exited"
    assert sells[0].price > 102.0 * 0.70, "exited at the hard stop instead of trailing"
    print(f"  ok  trailing stop exits above the hard stop (exit {sells[0].price:.1f})")


def test_halt_blocks_new_entries_but_never_the_exit():
    cfg = make_cfg()
    state = new_state(cfg)
    state["bars_seen"] = 100
    state["halted"] = True
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
    state2["prev_bar_close"] = 100.0
    candle2 = make_candle(ts, 105.0)
    assert decide(state2, candle2, cfg) == [], "entered a new position while halted"
    print("  ok  halt blocks new entries")


def test_decide_never_moves_money():
    cfg = make_cfg()
    series = _pre_and_session(1_700_000_000, 13, 100.0, [102.0, 103.0])
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
    state["prev_bar_close"] = 100.0
    state["current_session_date"] = None

    ts = int(datetime.datetime(2026, 1, 1, 13, tzinfo=datetime.timezone.utc).timestamp())
    candle = make_candle(ts, 105.0)
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "buy"
    investable = state["idr"] - state["profit_reserve"]
    assert actions[0].qty_idr <= investable * cfg.gap_max_exposure + 1
    print(f"  ok  sizing excludes the {state['profit_reserve']:,.0f} protected reserve")


def test_never_holds_more_than_one_position():
    import random
    cfg = make_cfg(gap_stop_pct=0.05, gap_trail_pct=0.08, gap_min_pct=0.005)
    random.seed(17)
    series = []
    pre_price = 100.0
    for day in range(30):
        session_prices = []
        p = pre_price * (1.0 + random.uniform(-0.03, 0.032))
        for i in range(28):
            p *= (1.0 + random.uniform(-0.02, 0.021))
            p = max(p, 1.0)
            session_prices.append(p)
        series.extend(_pre_and_session(1_700_000_000 + day * DAY, 13, pre_price, session_prices))
        pre_price = session_prices[-1]

    state, fills = run_series(series, cfg)
    open_lots = 0
    max_open = 0
    for f in fills:
        open_lots += 1 if f.side == "buy" else -1
        max_open = max(max_open, open_lots)
    assert max_open <= 1, f"held {max_open} positions at once"
    assert state["idr"] >= -1e-6, f"cash went negative: {state['idr']}"
    print(f"  ok  never more than one position across 30 sessions ({len(fills)} fills)")


def test_ema_filter_disabled_by_default_no_behavior_change():
    cfg = make_cfg(gap_mode="fade", gap_min_pct=0.01)
    assert cfg.gap_fade_ema_enabled is False
    series = _pre_and_session(1_700_000_000, 13, 100.0, [97.0, 97.0])
    state, fills = run_series(series, cfg)
    assert [f for f in fills if f.side == "buy"], (
        "EMA filter changed default behavior even though disabled"
    )
    print("  ok  EMA filter ships disabled -- no behavior change unless explicitly enabled")


def test_ema_filter_blocks_a_fade_far_below_a_declining_trend():
    cfg = make_cfg(gap_mode="fade", gap_min_pct=0.01)
    cfg.gap_fade_ema_enabled = True
    cfg.gap_fade_ema_period = 5
    cfg.gap_fade_ema_max_below_pct = 0.05

    # Build a real declining trend first, so the EMA is meaningfully above
    # where price ends up -- then a deep down-gap should be BLOCKED.
    state = new_state(cfg)
    fills = []
    ts = 1_700_000_000
    price = 100.0
    for _ in range(20):
        candle = make_candle(ts, price)
        for action in decide(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
        price *= 0.99  # steady decline -- EMA trails above current price
        ts += BAR
    ema_before = state["ema"]

    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    session_start = int(datetime.datetime(dt.year, dt.month, dt.day, 13,
                                           tzinfo=datetime.timezone.utc).timestamp())
    deep_gap_price = ema_before * 0.90  # 10% below EMA -- past the 5% floor
    candle = make_candle(session_start, deep_gap_price)
    state["prev_bar_close"] = price
    state["current_session_date"] = None
    actions = decide(state, candle, cfg)
    assert actions == [], f"EMA filter failed to block a fade far below a declining trend: {actions}"
    print("  ok  EMA filter blocks a fade that's fallen far below a declining trend")


def test_ema_filter_allows_a_fade_close_to_the_trend():
    cfg = make_cfg(gap_mode="fade", gap_min_pct=0.01)
    cfg.gap_fade_ema_enabled = True
    cfg.gap_fade_ema_period = 5
    cfg.gap_fade_ema_max_below_pct = 0.05

    state = new_state(cfg)
    fills = []
    ts = 1_700_000_000
    price = 100.0
    for _ in range(20):
        candle = make_candle(ts, price)
        for action in decide(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
        ts += BAR  # flat -- EMA converges to ~100
    ema_before = state["ema"]

    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    session_start = int(datetime.datetime(dt.year, dt.month, dt.day, 13,
                                           tzinfo=datetime.timezone.utc).timestamp())
    mild_gap_price = ema_before * 0.97  # only 3% below -- inside the 5% floor
    candle = make_candle(session_start, mild_gap_price)
    state["prev_bar_close"] = price
    state["current_session_date"] = None
    actions = decide(state, candle, cfg)
    assert actions and actions[0].side == "buy", (
        f"EMA filter incorrectly blocked a fade close to the trend: {actions}"
    )
    print("  ok  EMA filter allows a fade that's only mildly below the trend")


def test_config_rejects_nonsense():
    cases = [
        ("bad mode", dict(gap_mode="short")),
        ("min pct zero", dict(gap_min_pct=0.0)),
        ("exposure above 100%", dict(gap_max_exposure=1.5)),
        ("stop pct zero", dict(gap_stop_pct=0.0)),
    ]
    for label, overrides in cases:
        cfg = Config()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        assert cfg.validate(), f"config accepted nonsense: {label} ({overrides})"
    print(f"  ok  config rejects all {len(cases)} nonsense GAP settings")


def main():
    try:
        print("\nVerifying Unyil GAP (WRAPPER family: Gap-and-Go / Gap-Fade)")
        print("-" * 66)
        print(" mode behaviour (spot-only reframing):")
        test_go_mode_enters_on_an_up_gap()
        test_go_mode_ignores_a_down_gap()
        test_fade_mode_enters_on_a_down_gap()
        test_fade_mode_ignores_an_up_gap()
        print(" general behaviour:")
        test_gap_below_minimum_threshold_is_ignored()
        test_only_one_gap_evaluation_per_session()
        test_new_session_allows_a_fresh_evaluation()
        test_stop_loss_fires()
        test_trailing_stop_locks_in_gains()
        print(" EMA trend filter (pre-committed experiment):")
        test_ema_filter_disabled_by_default_no_behavior_change()
        test_ema_filter_blocks_a_fade_far_below_a_declining_trend()
        test_ema_filter_allows_a_fade_close_to_the_trend()
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
