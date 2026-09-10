"""
The harness around `decide`: fake wallet, fee/slippage model, invariant checks,
drawdown halt.

In live trading you swap `apply_fill` for a real Indodax order call and
`verify_balance` for a real balance query. `decide` itself is untouched.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import Config
from strategy import Action, Lot


class HaltSignal(Exception):
    """Raised when the bot must stop and hand control back to a human."""


@dataclass
class Fill:
    ts: int
    side: str
    price: float
    qty_coin: float
    gross_idr: float
    fee_idr: float
    reason: str
    tag: str
    lot_id: Optional[int] = None   # links a sell back to its buy, for real win/loss accounting


def fill_price(price: float, side: str, cfg: Config) -> float:
    """Slippage always works against us."""
    if side == "buy":
        return price * (1.0 + cfg.slippage_pct)
    return price * (1.0 - cfg.slippage_pct)


def equity(state: Dict[str, Any], price: float) -> float:
    coin = state["grid_coin"] + state["hold_coin"]
    return state["idr"] + coin * price


def verify_balance(state: Dict[str, Any]) -> Optional[str]:
    """
    The 'Verify real balance' box.

    In backtest this checks internal consistency: lots must sum to grid_coin,
    and nothing may go negative. In live, extend it to compare against the
    balance Indodax reports and pause on mismatch.
    """
    tol = 1e-9
    if state["idr"] < -tol:
        return f"IDR balance negative: {state['idr']}"
    if state["grid_coin"] < -tol:
        return f"grid coin negative: {state['grid_coin']}"
    if state["hold_coin"] < -tol:
        return f"hold coin negative: {state['hold_coin']}"

    lot_sum = sum(lot.qty_coin for lot in state["lots"])
    if abs(lot_sum - state["grid_coin"]) > 1e-8:
        return f"lot sum {lot_sum} != grid_coin {state['grid_coin']}"
    return None


def apply_fill(state: Dict[str, Any], action: Action, candle: Dict[str, Any],
               cfg: Config, fills: List[Fill]) -> None:
    """Mutate the fake wallet to reflect one executed action."""
    ts = candle["ts"]

    if action.side == "buy":
        px = fill_price(candle["close"], "buy", cfg)
        gross = action.qty_idr
        if gross > state["idr"] + 1e-9:
            raise HaltSignal(
                f"buy of {gross:.0f} IDR exceeds balance {state['idr']:.0f}"
            )
        fee = gross * cfg.fee_buy_pct
        qty = (gross - fee) / px

        state["idr"] -= gross

        if action.tag == "hold":
            state["hold_coin"] += qty
            state["hold_bought"] = True
        else:
            state["grid_coin"] += qty
            state["lots"].append(
                Lot(
                    lot_id=state["next_lot_id"],
                    level_idx=action.level_idx,
                    qty_coin=qty,
                    entry_price=px,
                    entry_ts=ts,
                )
            )
            state["next_lot_id"] += 1

        fills.append(Fill(ts, "buy", px, qty, gross, fee, action.reason, action.tag,
                          lot_id=(None if action.tag == "hold" else state["next_lot_id"] - 1)))

    elif action.side == "sell":
        lot = next((l for l in state["lots"] if l.lot_id == action.lot_id), None)
        if lot is None:
            raise HaltSignal(f"sell references unknown lot {action.lot_id}")

        if cfg.fill_model == "close":
            # Market order at the price the bot actually saw.
            px = fill_price(candle["close"], "sell", cfg)
        else:
            # Resting limit at the target; never better than the target.
            target = lot.target_price(cfg)
            px = fill_price(min(max(target, candle["low"]), candle["high"]), "sell", cfg)

        gross = lot.qty_coin * px
        fee = gross * cfg.fee_sell_pct

        state["grid_coin"] -= lot.qty_coin
        state["idr"] += gross - fee
        state["lots"] = [l for l in state["lots"] if l.lot_id != lot.lot_id]

        fills.append(Fill(ts, "sell", px, lot.qty_coin, gross, fee,
                          action.reason, action.tag, lot_id=lot.lot_id))
    else:
        raise HaltSignal(f"unknown side {action.side!r}")


def check_drawdown(state: Dict[str, Any], price: float, cfg: Config) -> Optional[str]:
    """
    Account-wide halt. Stops NEW trades. Leaves existing positions untouched,
    exactly as the diagram specifies — it does not panic-sell the book.

    Tracks state["true_max_dd"] unconditionally, every call -- this is the
    conservative, mark-to-market drawdown, measured BEFORE any same-bar
    protective exit executes. Found to matter in practice: if a stop-loss
    or trailing stop also fires on the SAME bar this halt check runs, the
    exit's actual intrabar fill can land at a better price than the
    candle's close, meaning the post-hoc equity-curve-based "Max drawdown"
    (report.py) can understate how bad things looked at the moment the
    breaker actually tripped. Both numbers are now reported (see report.py)
    so risk decisions are made on the honest, conservative figure.
    """
    eq = equity(state, price)
    if eq > state["peak_equity"]:
        state["peak_equity"] = eq

    if state["peak_equity"] <= 0:
        return None

    dd = 1.0 - (eq / state["peak_equity"])
    state["true_max_dd"] = max(state.get("true_max_dd", 0.0), dd)

    if dd >= cfg.max_drawdown_pct and not state["halted"]:
        state["halted"] = True
        return (
            f"drawdown {dd:.2%} reached the {cfg.max_drawdown_pct:.0%} limit "
            f"(equity {eq:,.0f} vs peak {state['peak_equity']:,.0f})"
        )
    return None
