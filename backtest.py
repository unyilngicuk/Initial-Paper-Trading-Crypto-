"""
The replay harness.

Feeds stored candles past `decide` one at a time, in order, keeping a fake
wallet. This is the loop; `decide` is the strategy. Keeping them separate is
the whole point.

Usage:
    python backtest.py --candles data/btc_15m.csv
    python backtest.py --candles data/btc_15m.csv --spacing 0.02 --levels 8
"""

import argparse
import csv
import sys
from typing import Any, Dict, List

from config import Config
from engine import (Fill, HaltSignal, apply_fill, check_drawdown, equity,
                    verify_balance)
from report import print_report, write_equity_csv
from strategy import decide, new_state


def load_candles(path: str) -> List[Dict[str, Any]]:
    """Expects columns: ts,open,high,low,close,volume (ts = unix seconds)."""
    candles = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            candles.append({
                "ts": int(float(row["ts"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            })
    candles.sort(key=lambda c: c["ts"])
    return candles


def run(candles: List[Dict[str, Any]], cfg: Config, verbose: bool = False) -> Dict[str, Any]:
    problems = cfg.validate()
    if problems:
        raise SystemExit("Config rejected:\n  - " + "\n  - ".join(problems))

    state = new_state(cfg)
    fills: List[Fill] = []
    equity_curve: List[tuple] = []
    events: List[str] = []

    for candle in candles:
        state["closes"].append(candle["close"])
        if len(state["closes"]) > cfg.history_window:
            state["closes"] = state["closes"][-cfg.history_window:]
        state["bars_seen"] += 1

        # --- account-wide risk check runs BEFORE new decisions ---
        halt_msg = check_drawdown(state, candle["close"], cfg)
        if halt_msg:
            events.append(f"[{candle['ts']}] HALT: {halt_msg}")
            if verbose:
                print(f"  !! {halt_msg}")

        try:
            actions = decide(state, candle, cfg)
            for action in actions:
                apply_fill(state, action, candle, cfg, fills)

                err = verify_balance(state)
                if err:
                    raise HaltSignal(f"balance check failed: {err}")

            if state.get("_pending_anchor") is not None:
                state["anchor"] = state["_pending_anchor"]

        except HaltSignal as e:
            # Per-trade fault: pause and hand back to the human.
            events.append(f"[{candle['ts']}] PAUSED: {e}")
            state["halted"] = True
            if verbose:
                print(f"  !! paused: {e}")

        equity_curve.append((candle["ts"], equity(state, candle["close"]), candle["close"]))

    return {
        "state": state,
        "fills": fills,
        "equity_curve": equity_curve,
        "events": events,
        "cfg": cfg,
        "candles": candles,
    }


def resolve_circuit_breaker(cfg: Config, active_strategy_name: str) -> float:
    """
    Per-strategy circuit breaker defaults, both calculated from real
    evidence rather than a single shared guess: Guardian's own tighter
    guardian_max_drawdown_pct (its objective is preservation, 20% is too
    loose for that), and the WRAPPER family's wrapper_max_drawdown_pct
    (22%, calculated across 4 real assets under Gap-Fade -- see spec
    Section 11j). Every other strategy uses the shared 20% base default.
    Extracted into its own function so the real wiring logic can be
    tested directly (see test_circuit_breakers.py), not re-implemented
    and risk silently drifting out of sync with what actually runs.
    """
    if active_strategy_name == "guardian":
        return cfg.guardian_max_drawdown_pct
    if active_strategy_name in ("orb", "gap"):
        return cfg.wrapper_max_drawdown_pct
    return cfg.max_drawdown_pct


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--candles", required=True, help="CSV of ts,open,high,low,close,volume")
    p.add_argument("--capital", type=float)
    p.add_argument("--coin", type=str, help="asset symbol for display/labeling (e.g. eth, btc) -- "
                                             "does not affect the data used, only the printed header")
    p.add_argument("--orb-session-hour", type=int, default=None,
                   help="manually override ORB's auto-detected session start hour (UTC)")
    p.add_argument("--orb-range-bars", type=int, default=None,
                   help="manually override ORB's opening-range length in bars")
    p.add_argument("--gap-mode", type=str, default=None, choices=["go", "fade"],
                   help="override GAP's mode: 'go' (trade up-gaps) or 'fade' (trade down-gaps)")
    p.add_argument("--gap-min-pct", type=float, default=None,
                   help="override GAP's minimum gap size to act on")
    p.add_argument("--gap-fade-ema", action="store_true",
                   help="enable the EMA trend filter for gap_mode=fade (pre-committed experiment)")
    p.add_argument("--gap-trail-pct", type=float, default=None,
                   help="override GAP's trailing stop width")
    p.add_argument("--gap-stop-pct", type=float, default=None,
                   help="override GAP's hard stop-loss")
    p.add_argument("--spacing", type=float)
    p.add_argument("--levels", type=int)
    p.add_argument("--venue", choices=list(Config.VENUES),
                   help="fee/slippage/fill preset for a venue")
    p.add_argument("--trend-days", type=float,
                   help="Unyil 2.0: trend SMA window in days (converted to 15m bars)")
    p.add_argument("--buffer", type=float, help="Unyil 2.0: trend_buffer_pct")
    p.add_argument("--stop", type=float, help="Unyil 2.0: stop_loss_pct")
    p.add_argument("--base-risk", type=float, help="Unyil 2.0: base_risk_frac")
    p.add_argument("--min-risk", type=float, help="Unyil 2.0: min_risk_frac")
    p.add_argument("--losses-before-shrink", type=int,
                   help="Unyil 2.0: consecutive losses required before sizing shrinks")
    p.add_argument("--guardian-exposure", type=float,
                   help="Guardian: max fraction of investable cash per position")
    p.add_argument("--guardian-stop", type=float, help="Guardian: hard stop fraction")
    p.add_argument("--guardian-trail", type=float, help="Guardian: trailing stop fraction")
    p.add_argument("--usro-trend-days", type=float, help="Usro: trend window in days")
    p.add_argument("--usro-buffer", type=float, help="Usro: entry buffer fraction")
    p.add_argument("--usro-exposure", type=float, help="Usro: max exposure fraction")
    p.add_argument("--usro-trail", type=float, help="Usro: trailing stop fraction")
    p.add_argument("--guardian-exit-buffer", type=float,
                   help="Guardian: how far below the medium trend price must fall to exit")
    p.add_argument("--cooldown-hours", type=float,
                   help="hours to block new entries after a losing exit (0 to disable)")
    p.add_argument("--adaptive", action="store_true",
                   help="enable bounded adaptive position sizing (off by default)")
    p.add_argument("--adaptive-floor", type=float, help="adaptive sizing lower bound")
    p.add_argument("--adaptive-ceiling", type=float, help="adaptive sizing upper bound")
    p.add_argument("--adaptive-min-trades", type=int, help="trades required before adjusting")
    p.add_argument("--max-drawdown", type=float,
                   help="override the circuit breaker threshold directly "
                        "(otherwise auto-set per active strategy)")
    p.add_argument("--equity-out", help="write the equity curve to this CSV")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    cfg = Config()
    if args.capital:
        cfg.starting_idr = args.capital
    if args.coin:
        cfg.coin = args.coin.lower()
    if args.orb_session_hour is not None:
        cfg.orb_session_start_hour = args.orb_session_hour
    if args.orb_range_bars is not None:
        cfg.orb_range_bars = args.orb_range_bars
    if args.gap_mode is not None:
        cfg.gap_mode = args.gap_mode
    if args.gap_min_pct is not None:
        cfg.gap_min_pct = args.gap_min_pct
    if args.gap_fade_ema:
        cfg.gap_fade_ema_enabled = True
    if args.gap_trail_pct is not None:
        cfg.gap_trail_pct = args.gap_trail_pct
    if args.gap_stop_pct is not None:
        cfg.gap_stop_pct = args.gap_stop_pct
    if args.spacing:
        cfg.spacing_pct = args.spacing
        cfg.take_profit_pct = args.spacing
    if args.levels:
        cfg.num_levels = args.levels
    if args.venue:
        cfg.use_venue(args.venue)
    if args.trend_days:
        cfg.trend_ma_period = round(args.trend_days * 96)
        cfg.history_window = max(cfg.history_window, cfg.trend_ma_period + 10)
        cfg.warmup_bars = max(cfg.warmup_bars, cfg.trend_ma_period)
    if args.buffer is not None:
        cfg.trend_buffer_pct = args.buffer
    if args.stop is not None:
        cfg.stop_loss_pct = args.stop
    if args.base_risk is not None:
        cfg.base_risk_frac = args.base_risk
    if args.min_risk is not None:
        cfg.min_risk_frac = args.min_risk
    if args.losses_before_shrink is not None:
        cfg.losses_before_shrink = args.losses_before_shrink
    if args.guardian_exposure is not None:
        cfg.guardian_max_exposure = args.guardian_exposure
    if args.guardian_stop is not None:
        cfg.guardian_stop_pct = args.guardian_stop
    if args.guardian_trail is not None:
        cfg.guardian_trail_pct = args.guardian_trail
    if args.guardian_exit_buffer is not None:
        cfg.guardian_exit_buffer_pct = args.guardian_exit_buffer
    if args.usro_trend_days is not None:
        cfg.usro_trend_period = round(args.usro_trend_days * 96)
    if args.usro_buffer is not None:
        cfg.usro_entry_buffer_pct = args.usro_buffer
    if args.usro_exposure is not None:
        cfg.usro_max_exposure = args.usro_exposure
    if args.usro_trail is not None:
        cfg.usro_trail_pct = args.usro_trail
    if args.cooldown_hours is not None:
        cfg.reentry_cooldown_hours = args.cooldown_hours
    if args.adaptive:
        cfg.adaptive_sizing_enabled = True
    if args.adaptive_floor is not None:
        cfg.adaptive_floor = args.adaptive_floor
    if args.adaptive_ceiling is not None:
        cfg.adaptive_ceiling = args.adaptive_ceiling
    if args.adaptive_min_trades is not None:
        cfg.adaptive_min_trades = args.adaptive_min_trades

    # Auto-apply Guardian's tighter circuit breaker when it's the active
    # strategy, since its own objective is preservation and 20% is too
    # loose for that. An explicit --max-drawdown always wins either way.
    active_strategy = new_state(cfg).get("strategy_name", "rsi_grid")
    cfg.max_drawdown_pct = resolve_circuit_breaker(cfg, active_strategy)
    if args.max_drawdown is not None:
        cfg.max_drawdown_pct = args.max_drawdown

    candles = load_candles(args.candles)
    if not candles:
        raise SystemExit(f"no candles in {args.candles}")

    # Family/asset mismatch check -- a WARNING, not a block. Deliberate
    # cross-family testing (e.g. running native-crypto strategies on
    # tokenized stocks to establish the finding in the first place) is
    # legitimate and is exactly how this check's own threshold was derived.
    try:
        import strategy as _active_strategy_module
        strategy_family = getattr(_active_strategy_module, "STRATEGY_FAMILY", None)
        if strategy_family and len(candles) >= 96 * 7:
            from asset_type_detector import classify_asset, detect_session_start_hour
            asset_label = classify_asset(candles)["label"]
            asset_family = "WRAPPER" if asset_label == "WRAPPER" else "NATIVE_CRYPTO"

            # Auto-configure ORB's session hour from the real data, rather
            # than requiring it to be figured out by hand each time.
            if strategy_family == "WRAPPER" and cfg.orb_session_start_hour is None:
                detected_hour = detect_session_start_hour(candles)
                if detected_hour is not None:
                    cfg.orb_session_start_hour = detected_hour
                    print(f"  (auto-detected session start hour: {detected_hour}:00 UTC)")

            if strategy_family != asset_family:
                print(f"  !! WARNING: this data looks like a {asset_label} asset, but the "
                      f"loaded strategy is {strategy_family}-family.")
                print(f"  !! (Run asset_type_detector.py on this file for the full read. "
                      f"No {('WRAPPER' if asset_family == 'WRAPPER' else 'NATIVE_CRYPTO')} "
                      f"family strategy exists yet if that's what's needed.)")
                print()
    except Exception:
        pass  # never let the detector's own failure block a real backtest run

    result = run(candles, cfg, verbose=args.verbose)
    print_report(result)

    if args.equity_out:
        write_equity_csv(result, args.equity_out)
        print(f"\nEquity curve written to {args.equity_out}")


if __name__ == "__main__":
    main()
