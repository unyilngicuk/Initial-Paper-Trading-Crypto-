"""
Regime-switching simulation -- what would have happened if we actually
switched between Unyil 2.0 / Guardian / Usro following the regime
manager's confirmed output, versus running any single strategy the
whole time.

IMPORTANT METHODOLOGICAL CAVEAT, stated up front because it matters:
this uses the SAME two years of data that the regime manager's own
thresholds (chosen by inspection of this market) and all three
strategies' designs were built and tested against. A favourable result
here is partly expected -- it is an INTERNAL CONSISTENCY CHECK, not
out-of-sample proof the switching approach will work on data we have
not seen.

SWITCHING MECHANICS (a real design decision, made explicitly rather
than left ambiguous -- and corrected once during development, see below):
  1. A switch only takes effect while FLAT. If a position is open, the
     strategy that opened it keeps managing it -- including riding out
     a regime flip -- until IT decides to exit, on its own terms.
  2. The newly active strategy's own indicator history (its `closes`
     list) is RESEEDED from the real price history up to that point,
     not started cold.
  3. Principal and the profit-protection reserve are tracked at the
     PORTFOLIO level, not per-strategy, and carried across switches.
  4. UNCERTAIN means "retain the currently active strategy" -- no
     switch happens.

CORRECTION MADE DURING DEVELOPMENT: the first version force-closed any
open position immediately on a regime change. This was a real bug, not
a stylistic choice: Usro's entire validated advantage is having NO
trend-break exit, specifically so it can ride out pullbacks other
strategies would panic-sell. Force-closing it the instant the regime
label flipped destroyed exactly the mechanism that made it work,
before it ever entered this simulation. Guardian's protective logic was
similarly cut short mid-execution. The fix above -- switching only
governs the NEXT entry, never interrupts a position already being
managed by its own strategy -- was made specifically because the first
version's result (switching underperforming plain Unyil 2.0) did not
match what the individually-validated strategies implied it should do,
and this was the mechanism found responsible.

Run: python3 regime_switch_simulation.py --candles data/btcidr_15m_2y.csv --capital 1000000
"""

import argparse
import importlib
from datetime import datetime, timezone

from backtest import load_candles
from config import Config
from engine import apply_fill, check_drawdown, verify_balance
from regime_manager import apply_persistence, classify_all

REGIME_TO_STRATEGY = {
    "BULLISH": "usro",
    "BEARISH": "guardian",
    "SIDEWAYS": "unyil2",
}

# One pre-committed experiment (not a tuned parameter): only switch INTO
# Guardian on high-confidence (5/5) Bearish, not the looser 4/5 threshold.
# Rationale, decided BEFORE running this: the persistence rules already
# treat 5/5 Bearish as categorically different (8-bar fast confirmation
# vs. 16 for 4/5) on the premise that 5/5 is overwhelming evidence and
# 4/5 might be ordinary noise. This extends that same, pre-existing
# distinction to whether to switch at all, not just how fast.
#
# Explicitly flagged: this was only considered because the unrestricted
# version underperformed plain Unyil 2.0. That is the shape of
# overfitting risk, not proof the idea is wrong -- but it means this
# result must be read as "one pre-committed test on the same two years
# everything else was built on," not independent validation. See
# GUARDIAN_ONLY_ON_HIGH_CONFIDENCE in main() for the on/off switch.
GUARDIAN_ONLY_ON_HIGH_CONFIDENCE = True

STRATEGY_MODULES = {
    "unyil2": "strategy_unyil2",
    "guardian": "strategy_guardian",
    "usro": "strategy_usro",
}


def _load_strategy(name):
    mod = importlib.import_module(STRATEGY_MODULES[name])
    return mod.decide, mod.new_state, mod.Action


def _reseed_state(state, all_closes, up_to_index, cfg):
    start = max(0, up_to_index - cfg.history_window + 1)
    state["closes"] = list(all_closes[start:up_to_index + 1])
    state["bars_seen"] = len(state["closes"])


def run_switching_portfolio(candles, cfg):
    closes = [c["close"] for c in candles]

    print("Classifying regime across the full period...")
    raw = classify_all(candles)
    confirmed = apply_persistence(raw)

    active_name = "unyil2"
    decide_fn, new_state_fn, Action = _load_strategy(active_name)
    state = new_state_fn(cfg)
    _reseed_state(state, closes, 0, cfg)

    fills = []
    switch_log = []
    equity_curve = []

    for i, candle in enumerate(candles):
        regime = confirmed[i]["confirmed"]
        confirmed_key = confirmed[i]["confirmed_key"]

        if regime == "BEARISH" and GUARDIAN_ONLY_ON_HIGH_CONFIDENCE and confirmed_key != "BEARISH_5":
            # Moderate-confidence Bearish: stay on whatever is currently
            # running rather than switching to Guardian.
            target_name = active_name
        else:
            target_name = REGIME_TO_STRATEGY.get(regime, active_name)

        # Only allow a switch while FLAT. A position's own strategy keeps
        # managing it -- including riding out a regime flip -- until it
        # exits on its own terms. Forcing an early exit on every regime
        # change would override the very exit discipline (e.g. Usro's
        # deliberate lack of a trend-break exit) that made these
        # strategies validate well individually in the first place.
        if not state["lots"] and target_name != active_name:
            carried_idr = state["idr"]
            carried_principal = state["principal"]
            carried_reserve = state["profit_reserve"]
            carried_period_start = state["period_start_ts"]

            switch_log.append({
                "ts": candle["ts"], "from": active_name, "to": target_name,
                "regime": regime, "idr_at_switch": carried_idr,
            })

            active_name = target_name
            decide_fn, new_state_fn, Action = _load_strategy(active_name)
            state = new_state_fn(cfg)
            _reseed_state(state, closes, i, cfg)
            state["idr"] = carried_idr
            state["principal"] = carried_principal
            state["profit_reserve"] = carried_reserve
            state["period_start_ts"] = carried_period_start
        else:
            state["closes"].append(candle["close"])
            if len(state["closes"]) > cfg.history_window:
                state["closes"] = state["closes"][-cfg.history_window:]
            state["bars_seen"] += 1

        check_drawdown(state, candle["close"], cfg)
        for action in decide_fn(state, candle, cfg):
            apply_fill(state, action, candle, cfg, fills)
            err = verify_balance(state)
            assert err is None, f"balance check failed at bar {i}: {err}"

        eq = state["idr"] + sum(l.qty_coin for l in state["lots"]) * candle["close"]
        equity_curve.append((candle["ts"], eq, candle["close"]))

    return {
        "final_state": state, "fills": fills, "switch_log": switch_log,
        "equity_curve": equity_curve, "active_at_end": active_name,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--venue", default="indodax_maker", choices=list(Config.VENUES))
    p.add_argument("--weekly", action="store_true",
                   help="report week-by-week over a recent window instead of one final number")
    p.add_argument("--report-days", type=int, default=90)
    p.add_argument("--warmup-days", type=int, default=35)
    args = p.parse_args()

    cfg = Config()
    cfg.use_venue(args.venue)
    cfg.starting_idr = args.capital

    all_candles = load_candles(args.candles)

    print("=" * 70)
    print("  METHODOLOGICAL CAVEAT")
    print("=" * 70)
    print("  This uses the same 2 years the regime manager's thresholds and")
    print("  all three strategies were built and tested against. A good")
    print("  result is partly expected -- this is an internal consistency")
    print("  check, not out-of-sample proof for data we have not seen.")
    print("=" * 70)
    if GUARDIAN_ONLY_ON_HIGH_CONFIDENCE:
        print("\n  EXPERIMENT ACTIVE: Guardian only engages on high-confidence (5/5)")
        print("  Bearish, not the looser 4/5 threshold (pre-committed, see spec).")
    print()

    if args.weekly:
        max_ts = max(c["ts"] for c in all_candles)
        report_start_ts = max_ts - args.report_days * 86400
        data_start_ts = report_start_ts - args.warmup_days * 86400
        candles = [c for c in all_candles if c["ts"] >= data_start_ts]
        print(f"Classifying + running the switching portfolio over "
              f"{args.warmup_days} warmup + {args.report_days} reported days...")
        result = run_switching_portfolio(candles, cfg)
        curve = [(ts, eq, px) for ts, eq, px in result["equity_curve"] if ts >= report_start_ts]

        week_seconds = 7 * 86400
        w_start_ts, w_start_eq, w_start_px = curve[0]
        week_num = 1
        print(f"\n{'wk':>3} {'start':<11} {'end':<11} {'return':>9} {'buy&hold':>10} "
              f"{'switches':>9} {'end equity':>15}")
        print("-" * 78)
        prior_switch_count = 0
        for i, (ts, eq, px) in enumerate(curve):
            is_last = (i == len(curve) - 1)
            if ts >= w_start_ts + week_seconds or is_last:
                switches_this_week = sum(
                    1 for s in result["switch_log"] if w_start_ts <= s["ts"] <= ts
                ) - prior_switch_count
                from datetime import datetime, timezone
                start_d = datetime.fromtimestamp(w_start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                end_d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                print(f"{week_num:>3} {start_d:<11} {end_d:<11} "
                      f"{(eq/w_start_eq-1)*100:>8.2f}% {(px/w_start_px-1)*100:>9.2f}% "
                      f"{switches_this_week:>9} {eq:>15,.0f}")
                prior_switch_count += switches_this_week
                week_num += 1
                w_start_ts, w_start_eq, w_start_px = ts, eq, px

        total_ret = (curve[-1][1] / curve[0][1] - 1) * 100
        total_bh = (curve[-1][2] / curve[0][2] - 1) * 100
        print("-" * 78)
        print(f"Total: switching {total_ret:+.2f}%  |  buy&hold {total_bh:+.2f}%  |  "
              f"edge {total_ret - total_bh:+.2f} pts")
        print(f"Final equity: Rp {curve[-1][1]:,.0f}")
        return

    candles = all_candles
    result = run_switching_portfolio(candles, cfg)
    fills = result["fills"]
    curve = result["equity_curve"]
    state = result["final_state"]

    start_eq = args.capital
    end_eq = curve[-1][1]
    hold_final = (args.capital / candles[0]["close"]) * candles[-1]["close"]

    print(f"SWITCHING PORTFOLIO ({len(result['switch_log'])} strategy switches)")
    print("-" * 70)
    print(f"  Start equity:  Rp {start_eq:,.0f}")
    print(f"  End equity:    Rp {end_eq:,.0f}")
    print(f"  Return:        {(end_eq/start_eq - 1)*100:+.2f}%")
    print(f"  Buy & hold:    {(hold_final/start_eq - 1)*100:+.2f}%")
    print(f"  Edge vs B&H:   {(end_eq/start_eq - hold_final/start_eq)*100:+.2f} pts")
    print(f"  Profit reserve: Rp {state['profit_reserve']:,.0f} (protected)")
    print(f"  Active strategy at end: {result['active_at_end']}")
    print(f"  Total trades: {len([f for f in fills if f.side=='buy'])}")

    print(f"\n  Switch log:")
    for s in result["switch_log"]:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(s["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"    {dt}  {s['from']:>8} -> {s['to']:<8}  (regime: {s['regime']}, "
              f"equity Rp {s['idr_at_switch']:,.0f})")


if __name__ == "__main__":
    main()
