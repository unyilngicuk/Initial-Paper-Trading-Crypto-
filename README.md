# Backtester

A strategy validation harness for crypto trading bots on Indonesian exchanges.
Pure Python, standard library only, no dependencies, no subscription.

Its purpose is narrow and deliberate: **decide whether a strategy deserves real
money.** It is not a charting tool or a research playground. It answers one
question honestly, and its own correctness is verifiable.

---

## The one design rule

```
strategy.py ── decide(state, candle, cfg) -> [Action]
                  ▲                          ▲
        backtest.py                    live_runner.py
   (fake wallet, stored candles)   (real wallet, live candle)
```

The backtest and the live bot import **the same function**. Not a port of it,
not a reimplementation in another language — the same object in memory. A test
asserts this and fails if anyone breaks it.

This exists because the most common way trading bots lose money is that the
backtested strategy and the deployed strategy quietly drift apart.

---

## Quick start

```bash
# Get data (BTC/IDR, 15-minute bars, two years, paged)
python3 indodax_data.py fetch --days 730 --out data/btcidr_15m_2y.csv

# Run a backtest
python3 backtest.py --candles data/btcidr_15m_2y.csv --venue indodax_maker

# Prove the harness itself is sound
python3 test_harness.py        # 9 correctness tests
python3 test_robustness.py     # 10 robustness tests
python3 verify_engine.py       # known-answer validation on real data
```

---

## Files

| File | Role |
|---|---|
| `strategy.py` | **`decide()`** — the only strategy logic. Swap this to test a new idea. |
| `strategy_unyil2.py` | Unyil 2.0 — trend-following. The current default (already copied into `strategy.py`). |
| `strategy_guardian.py` | Unyil Guardian — conservative capital preservation for declines. Copy over `strategy.py` to activate. |
| `test_guardian.py` | Guardian's own tests (behaviour + all safety guarantees carried over). |
| `strategy_usro.py` | Unyil Usro — bull-market specialist. Copy over `strategy.py` to activate. |
| `test_usro.py` | Usro's own tests (behaviour + all safety guarantees carried over). |
| `regime_manager.py` | Classifies Bullish/Bearish/Sideways/Uncertain to inform (not automate) which strategy to run. Prints a suggested strategy per confirmed regime. |
| `test_regime_manager.py` | Regime manager's own tests (indicator correctness, scoring, persistence). |
| `regime_attribution.py` | Breaks down a strategy's REAL trades by which regime was confirmed at entry -- the evidence source for whether it's suited to Sideways. |
| `regime_switch_simulation.py` | Simulates actually switching between all three strategies following the regime manager's output, vs. running one strategy the whole time. Internal consistency check, not out-of-sample proof -- see the caveat it prints. |
| `test_unyil2.py` | Tests specific to Unyil 2.0's behavior (trend entry/exit, stop-loss, adaptive sizing). Run this instead of `test_harness.py`/`test_robustness.py` when Unyil 2.0 is active — those two are grid-specific. |
| `config.py` | Every tunable number and the venue cost presets. |
| `engine.py` | Fake wallet, fees, slippage, balance invariants, drawdown halt. |
| `backtest.py` | The replay loop. |
| `report.py` | Metrics, fair benchmark, equity CSV. |
| `indicators.py` | Wilder RSI over a rolling window. |
| `indodax_data.py` | Indodax candle fetcher, paged. The sole data source. |
| `live_runner.py` | The scheduled job. Execution stubbed — Indodax has a documented trading API, not yet wired up here. |
| `test_harness.py` | Correctness tests on synthetic data. |
| `test_robustness.py` | Degenerate inputs, invariants, edge cases. |
| `verify_engine.py` | Known-answer validation — the anti-self-deception check. |

---

## What has been verified

These are not aspirations. Each is an executable test that fails loudly.

**No lookahead.** Truncate the dataset mid-run and every decision before the
cut is byte-identical. The engine cannot see the future.

**The ledger reconciles.** Cash rebuilt from every trade matches the wallet to
0.00 IDR across a full run.

**No downward bias.** Given a buy-and-hold strategy on real data where holding
returned +65.87%, the engine reports +66.09% — buy-and-hold minus the entry
fee. It does not quietly eat returns. (`verify_engine.py`)

**Zero in, zero out.** A do-nothing strategy returns exactly 0.00%.

**Costs scale with activity.** A churn strategy over 7,512 round trips is
charged 203% of capital in fees, matching the round-trip cost exactly.

**`decide()` cannot move money.** Called 100 times against live state, balances
are untouched. The engine executes; the strategy only proposes.

**Fair benchmark.** Buy-and-hold is measured from the first bar the strategy
could act on, not bar 0. Measuring from bar 0 once credited a strategy with a
+1.43% move it was structurally unable to catch — nearly its entire apparent
edge. A regression test now prevents this.

**Survives degenerate input.** Empty data, single candles, flat markets, a 4x
rally, collapse toward zero, violent alternation, and gapped series (Indodax
omits bars with no trades). Cash never goes negative. Never double-books a
grid level. Never fills outside the data's price range.

### What is assumed, not proven

**Maker fills.** In `intrabar` mode a fill is credited whenever price touches
the level. Real resting orders queue and sometimes do not fill. Treat maker
results as the optimistic bound and taker results as the pessimistic one; the
truth lies between. Closing this gap requires live order-status tracking.

---

## Venue cost presets

PPh 0.21% on the sell side is a national tax (PMK 50/2025). It applies at every
Indonesian exchange and cannot be avoided by switching venue.

| preset | buy | sell | round trip | fills |
|---|---|---|---|---|
| `indodax_taker` | 0.3111% | 0.5211% | 0.8322% | close |
| `indodax_maker` | 0.0111% | 0.2211% | 0.2322% | intrabar |

Indodax charges makers 0% and takers 0.3%. Measured BTC/IDR spread: 0.1176%.
`indodax_maker` is the default — a grid is naturally a maker strategy (resting
orders above and below), so this is the realistic target to build toward.

---

## Testing a new strategy

1. Copy `strategy_template.py` over `strategy.py` (keep a backup).
2. Write your logic inside `decide()`. It must be **pure**: read state and the
   candle, return `Action` objects, touch nothing else.
3. Run the three verification scripts. If they pass, your strategy is being
   measured honestly.
4. Run the backtest, and always compare against buy-and-hold.

The discipline that matters: **run `verify_engine.py` after touching engine
code.** It is what separates "my idea is bad" from "my tooling is lying."

---

## Results so far

See `FINDINGS.md`. Summary: the RSI + grid strategy was tested on two years of
BTC/IDR and **lost to simply holding BTC by 31-37 percentage points**. Eight
parameter configurations were tried; none beat buy-and-hold. The engine was
then validated against known-answer strategies to confirm the failure was the
strategy, not the measurement.

That is the harness working correctly.

## Unyil 2.0 -- trend-following + adaptive sizing

A genuinely different strategy, not a re-tuning of the grid. See the
docstring at the top of `strategy_unyil2.py` for the full rationale. Briefly:

- Buys while price is above a long (~28-day) moving average; sells when that
  trend breaks, or a hard stop-loss fires. No small take-profit caps a
  winner the way the grid's +1.5% target did.
- Far fewer trades (single digits to low tens across two years, vs. 85+ for
  the grid), which matters directly under Indonesia's asymmetric sell-side
  fee.
- Adaptive position sizing: cuts the size of the next entry after **two or
  more consecutive losses** (not a single isolated one -- diagnosis on real
  data showed an isolated loss is often the shakeout right before a real
  trend, and shrinking after it punished exactly the entry that would have
  captured that trend). Recovers after a win. Deterministic, transparent
  risk management -- not prediction, not machine learning.
- Periodic profit protection: every ~6 months, closes any open position and
  moves profit above the original principal into a reserve future entries
  never touch. The bot's API key has no withdrawal permission by design, so
  this can't be an actual bank withdrawal from inside the strategy -- it has
  the same protective effect instead. Tested on real sequential 6-month
  windows of 2024-2026 BTC/IDR data, this was both safer AND more profitable
  than letting everything compound continuously straight through a later bad
  quarter.

`strategy.py` currently holds Unyil 2.0 (this is now the default). The
original RSI + grid strategy is preserved separately as
`strategy_grid_original.py` for reference/comparison -- copy it over
`strategy.py` if you want to run it instead.

To run Unyil 2.0's own tests (grid-specific tests in `test_harness.py` /
`test_robustness.py` don't apply to it):
```bash
python3 test_unyil2.py
python3 verify_engine.py
python3 backtest.py --candles data/btcidr_15m_2y.csv --venue indodax_maker
```

Useful extras:
- `trade_log.py` -- prints every individual trade with its date, price, exit
  reason, and P&L vs. entry. Use this to diagnose *why* a result looks the
  way it does, not just what the final number is.
- `filter_period.py` -- slices existing candle data into a shorter window
  (`--days`) or a specific past window (`--offset-days`), without
  re-fetching from Indodax. Useful for testing sequential periods.
- `backtest.py --trend-days N --buffer X --stop X --base-risk X --min-risk X
  --losses-before-shrink N` -- override any of Unyil 2.0's tunables from the
  command line without editing `config.py` by hand.
