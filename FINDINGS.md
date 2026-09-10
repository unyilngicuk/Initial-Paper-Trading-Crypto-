# Findings

A record of what was established, so none of it has to be rediscovered.
Dated September 2026.

---

## 1. Platform: Indodax

Reku was ruled out — its published API covers market data only, with no
authenticated order placement available. Indodax has a documented trading
API, 0 bad candles across 70,081 real 15-minute bars over 730 days (vs. deep
data-quality and history-depth problems on the alternative), and order book
depth that absorbs a retail-size market buy with 0.0000% price impact.

**The fee trap:** Indodax's taker fee (0.3%) is expensive. The advantage only
exists with resting limit orders, which earn the 0% maker rate. `indodax_maker`
is the default venue for this reason.

## 2. Fees are asymmetric, everywhere in Indonesia

Selling costs roughly three times buying, because PPh 0.21% (PMK 50/2025,
effective August 2025) applies to the sell side only. This is national tax,
not an exchange choice, and follows you to any Indonesian venue.

## 3. The RSI + grid strategy does not work on BTC/IDR

Two years, 70,081 bars, BTC +64.7% over the period.

| | return | vs holding |
|---|---|---|
| Indodax maker (optimistic) | +28.47% | **−37.40%** |
| Indodax taker | +29.08% | −36.80% |

**Eight parameter configurations tested; zero beat buy-and-hold.** Best to worst
spanned only four percentage points, so the problem is structural, not a matter
of tuning.

**By half-year:**

| period | BTC | bot | edge |
|---|---|---|---|
| Sep 2024 – Mar 2025 | +64.0% | +25.4% | −38.5% |
| Mar – Sep 2025 | +34.3% | +18.4% | −15.9% |
| Sep 2025 – Mar 2026 | −36.8% | −34.0% | **+2.8%** |
| Mar – Sep 2026 | +16.7% | +10.3% | −6.5% |

Positive edge in exactly one period: the crash. The grid's apparent advantage in
a falling market is **underexposure, not skill** — it caps upside by selling
winners at +1.5% while price runs further, and holds idle cash waiting for dips
a rising market does not deliver.

## 4. The 20% drawdown halt was decorative

On the first real dataset:

- **10 Oct** — 95% of capital deployed. All grid levels filled, hold bucket bought.
- **19 Nov** — the halt fires, 40 days later.

By then there was nothing left to stop. Setting the limit to 35%, or removing it
entirely, produced results **identical to the last decimal**. Maximum drawdown
reached 44% despite the "20% limit", because halting stops buying but cannot
stop existing positions from falling.

**Lesson:** a drawdown halt only means something if capital is still uncommitted
when it fires.

## 5. A benchmark bug nearly manufactured an edge

Buy-and-hold was originally measured from bar 0, but the strategy cannot act
until bar 50 (RSI warmup). Price drifted +1.43% during that blind window —
against a measured edge of +1.57%. The bug was **almost the entire result**.
Fixed, with a regression test.

---

## Open questions

- **Maker fill realism.** Backtests credit a fill on any touch. Real resting
  orders queue. Paper trading against live prices would measure the gap.
- **Does the grid work in a genuinely sideways market?** Grids are built for
  ranges. Neither tested year was one. Knowing you are in a range *in advance*
  is its own hard problem.
- **Indodax authenticated API integration.** Not yet implemented in
  `live_runner.py` — `fetch_real_balance()` and `place_order()` are stubs.
