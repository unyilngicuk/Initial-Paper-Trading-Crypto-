"""
Locks in each strategy's circuit breaker default -- tests the ACTUAL
wiring function in backtest.py (resolve_circuit_breaker), not a
re-implementation, so this can't silently drift out of sync with what
a real backtest.py run actually does.

Run: python3 test_circuit_breakers.py
"""

from backtest import resolve_circuit_breaker
from config import Config


def test_unyil2_uses_the_shared_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "unyil2")
    assert result == cfg.max_drawdown_pct == 0.20, (
        f"Unyil 2.0 circuit breaker changed: expected 0.20, got {result}"
    )
    print(f"  ok  Unyil 2.0 uses the shared default ({result:.0%})")


def test_usro_uses_the_shared_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "usro")
    assert result == cfg.max_drawdown_pct == 0.20, (
        f"Usro circuit breaker changed: expected 0.20, got {result}"
    )
    print(f"  ok  Usro uses the shared default ({result:.0%})")


def test_guardian_uses_its_own_tighter_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "guardian")
    assert result == cfg.guardian_max_drawdown_pct == 0.10, (
        f"Guardian circuit breaker changed: expected 0.10, got {result}"
    )
    print(f"  ok  Guardian uses its own tighter default ({result:.0%})")


def test_orb_uses_the_wrapper_family_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "orb")
    assert result == cfg.wrapper_max_drawdown_pct == 0.22, (
        f"ORB circuit breaker changed: expected 0.22, got {result}"
    )
    print(f"  ok  ORB uses the WRAPPER-family default ({result:.0%})")


def test_gap_uses_the_wrapper_family_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "gap")
    assert result == cfg.wrapper_max_drawdown_pct == 0.22, (
        f"GAP circuit breaker changed: expected 0.22, got {result}"
    )
    print(f"  ok  GAP uses the WRAPPER-family default ({result:.0%})")


def test_unknown_strategy_falls_back_to_shared_default():
    cfg = Config()
    result = resolve_circuit_breaker(cfg, "rsi_grid")
    assert result == cfg.max_drawdown_pct == 0.20
    print(f"  ok  an unrecognized strategy name falls back to the shared default ({result:.0%})")


def test_wrapper_default_is_actually_looser_than_native_and_guardian():
    """
    Locks in the RELATIONSHIP the 22% figure was calculated to satisfy,
    not just its literal value -- if someone changes one default without
    reconsidering the others, this should catch it.
    """
    cfg = Config()
    native = resolve_circuit_breaker(cfg, "unyil2")
    guardian = resolve_circuit_breaker(cfg, "guardian")
    wrapper = resolve_circuit_breaker(cfg, "orb")
    assert guardian < native < wrapper, (
        f"expected guardian ({guardian:.0%}) < native ({native:.0%}) < "
        f"wrapper ({wrapper:.0%})"
    )
    print(f"  ok  guardian ({guardian:.0%}) < native ({native:.0%}) < wrapper ({wrapper:.0%})")


def main():
    print("\nVerifying circuit breaker wiring (backtest.py's actual resolve_circuit_breaker)")
    print("-" * 78)
    test_unyil2_uses_the_shared_default()
    test_usro_uses_the_shared_default()
    test_guardian_uses_its_own_tighter_default()
    test_orb_uses_the_wrapper_family_default()
    test_gap_uses_the_wrapper_family_default()
    test_unknown_strategy_falls_back_to_shared_default()
    test_wrapper_default_is_actually_looser_than_native_and_guardian()
    print("-" * 78)
    print("all passed\n")


if __name__ == "__main__":
    main()
