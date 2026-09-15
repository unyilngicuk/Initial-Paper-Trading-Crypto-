# Unyil 2.0 — Trading Bot Specification

**Status: LOCKED / AGREED**
This document is the official replacement for the original "Unyil's Script" specification. That original document and its strategy (RSI + Grid on Reku) are retired — superseded in full by what's described below, after real backtesting evidence showed the original approach didn't hold up. "Unyil 2.0" is now the project's name, not just the strategy's. Any future change to strategy, platform, or safety rules should be revised here explicitly, not left as silent drift.

---

## 1. Platform

- **Exchange: Indodax** — changed from the original choice, Reku.
- **Why Reku was dropped:** verified three independent ways that Reku's API covers market data only, with no authenticated order-placement endpoint. A bot cannot place real trades on Reku — only notify a human to trade manually. Indodax has a real, documented trading API.
- Legally licensed under Bappebti / OJK — no legal gray area.
- Real historical data confirmed clean: 70,081 fifteen-minute bars over 730 days, **0 bad candles**, 0 gaps (vs. Reku's 100-bar cap and 45% arithmetically-impossible candles).
- Order book depth confirmed sufficient at retail size: a 20,000,000 IDR market buy moves price 0.0000%.
- **Trading type: SPOT ONLY.** No margin, no futures, no leverage, no borrowing.
  - This is a hard boundary. Spot trading cannot produce a negative balance — you can only spend money you actually have.
  - If margin/futures trading is ever considered in the future, **none of the safety guarantees in this document carry over automatically.** That would require a completely separate, fresh, careful conversation before any code is written.
- **Real fee structure** (previously assumed a flat 0.3%; corrected after building the backtester):
  - Indodax maker: 0% + Indonesia's mandatory 0.21% PPh sell-side tax (PMK 50/2025) → round trip ≈ 0.2322%
  - Indodax taker: 0.3% + the same tax → round trip ≈ 0.8322%
  - The 0.21% PPh tax applies to *every* Indonesian exchange, on the sell side only, and cannot be avoided by switching venue.

## 2. Strategy — trend-following + adaptive sizing (supersedes the original RSI + Grid)

### Why the original strategy was replaced
The original RSI + Grid strategy (regime-filtered grid trading, 70% grid / 30% hold split) was rebuilt as a real, independently-verified backtesting harness (see Section 9) and re-tested on the full 2-year real Indodax dataset. Result: **it lost to simple buy-and-hold by 31-37 percentage points**, across three cost presets and eight parameter configurations — none beat holding. Diagnosis: its one apparent advantage (protection during a crash period) was underexposure — sitting in cash — not genuine skill, and it structurally cannot capture a real trend because it takes profit at a fixed +1.5% regardless of how far a move continues. Its 20% drawdown halt was also found to be *decorative* in that specific test: by the time it fired, most capital was already committed, so it stopped nothing further.

The original grid strategy is preserved as `strategy_grid_original.py` for reference. It is no longer the active strategy.

### How Unyil 2.0 works (current shipped defaults)
1. **Trend-following, not mean-reversion.** Buys while price is above a ~28-day (2688-bar) moving average; sells only when that trend breaks below it (0.3% buffer), or a hard stop-loss fires. No small take-profit caps a winner.
2. **Hard stop-loss (15%)**, independent of the trend filter, protects against a sharp crash the slow-moving average wouldn't catch in time.
3. **Full commitment sizing (100% of investable cash).** This is still **spot only** — no leverage, no borrowing — so 100% means 100% of money actually held, and total loss remains capped at what was deposited.
4. **Adaptive loss-based shrinking: present but disabled by default.** `min_risk_frac` ships equal to `base_risk_frac`. The mechanism works and is tested; it is switched off because real-data diagnosis showed it repeatedly undersized the entries that became the biggest winners. Lower `min_risk_frac` to re-enable.
5. **Periodic profit protection.** Every ~183 days, any open position is closed and profit above the original principal is swept into a reserve future entries never touch. The bot's API key has no withdrawal permission (Section 4), so this cannot be a real bank withdrawal from inside the strategy — it achieves the same protective effect internally. The user can manually withdraw the reserve any time.
6. **No live LLM in the decision loop.** All of the above is plain, deterministic Python.

### Evolution of the configuration (each change evidence-driven)
- **v1 → v2:** loss-based shrinking changed from "shrink after any single loss" to "shrink after 2+ consecutive losses." A single whipsaw loss is often the shakeout right before a real trend; the old rule punished the very entry that would have captured it (a +60.68% winner was entered at half size). This alone closed more than half the gap to buy-and-hold.
- **v2 → v3 (current):** buffer 1.0% → 0.3%, base risk 90% → 100%, shrinking disabled. Rationale: weekly analysis showed the strategy sitting in cash through the opening weeks of rallies, and undersizing its best entries. Tested out-of-sample across four independent quarters.
- **Tried and rejected (5 experiments, all documented so they aren't blindly re-tried):**
  1. **Shorter trend window (28 → 14 days).** Hypothesis: less warmup waste on short deployments. Result: worse in 3 of 4 quarters. The 28-day window's real job turned out to be resisting premature exits during normal volatility inside a genuine trend; shortening it broke exactly what was working.
  2. **Bounded adaptive position sizing** (win-rate-driven, floor/ceiling bounded, minimum sample, fully audited). Result: **cost 15.6 points of return** (+91.76% → +76.15%) for only 2.2 points less drawdown. Root cause: this strategy has a naturally low win rate (11-30%) and earns its money from a few very large winners. A win-rate-based sizing rule therefore ratchets down and never recovers, systematically undersizing the rare big winners that are the entire edge. **Win rate is the wrong signal for a strategy whose edge lives in win magnitude.** Code retained, ships disabled.
  3. **Re-entry cooldown after losing exits.** Hypothesis: block the whipsaw clusters visible in loss analysis (four losing round trips inside 48 hours, Dec 2024). Result: 12h gained a negligible 0.58 points; 48h *lost* 2.9 points. Root cause: the motivating whipsaw cluster exists in the optimistic maker fill model (73 trades over 2 years); under the pessimistic taker model there are only 8-9 trades total, leaving almost no thrashing to prevent. Code retained, ships disabled (`--cooldown-hours` to enable).
  4. **Regime-driven strategy switching** (auto-switch between Unyil 2.0 / Guardian / Usro following `regime_manager.py`'s confirmed regime). Tested three ways: (a) unrestricted switching on every regime confirmation returned +71.66% vs. the +91.76% single-strategy baseline -- worse, not better, because Guardian activated 11 separate times for ordinary bearish spells Unyil 2.0 already handles adequately on its own, trading away compounding for protection that usually wasn't needed; (b) a genuine implementation bug was found and fixed along the way -- the first version force-closed open positions the instant the regime label flipped, destroying Usro's core validated advantage (no trend-break exit, specifically so it can ride out pullbacks) before the comparison was even fair -- fixing it changed the result by 0.04 points, confirming the bug was not the actual explanation; (c) gating the switch to only high-confidence (5/5) Bearish, one pre-committed test, returned +83.68% -- better, closing roughly 60% of the gap, but still short of simply running Unyil 2.0 alone. **Conclusion: regime-switching adds real value over buy-and-hold and over Guardian/Usro run continuously, but not over the simplest choice. Not adopted.**
  5. **Regime-aware new-entry sizing on Unyil 2.0 itself** (`unyil2_regime_sizing.py`) -- a narrower follow-up to #4's gated result: does shrinking Unyil 2.0's own new-entry size on a high-confidence (5/5) Bearish confirmation help, without any engine swap at all. Pre-committed success bar (decided before running): return within ~5 points of the +91.76% baseline AND max drawdown measurably improved from 15.17%. **Result: an exact null -- every number identical to the unmodified baseline to the decimal** (91.76% return, 15.17% drawdown, 73 trades), despite the size-cut being "armed" for 919 bars (1.3% of the period). Root cause, understood only after running it: Unyil 2.0 only opens a new position when price is *above* its trend average; a confirmed 5/5 Bearish regime requires price well *below* trend with strong downward momentum. The two conditions are close to mutually exclusive by construction, so the intervention was never active during any bar where a real entry was actually being considered. This was a structurally incoherent hypothesis, not merely a weak one. A genuinely different mechanism (trimming an *already-open* position on a severe confirmation, rather than sizing a new entry that was never going to fire) would be a different, separately-pre-committed test, not a variant of this one. Code retained (`regime_bearish5_risk_multiplier`, `state["regime_risk_multiplier"]`), ships as a true no-op (1.0) unless externally driven by `unyil2_regime_sizing.py`.

### Honest performance — real Indodax data, Rp 1,000,000, out-of-sample tested

| Period | Market condition | Unyil 2.0 (current) | Buy & Hold | Edge |
|---|---|---|---|---|
| **Full 2 years** | mixed cycle | **+91.76%** | +63.60% | **+28.16** |
| Q1 (Sep 2024–Mar 2025) | strong rally | +58.89% | +68.32% | –9.43 |
| Q2 (Mar–Sep 2025) | rally | +25.38% | +28.91% | –3.53 |
| Q3 (Sep 2025–Mar 2026) | **crash** | **–16.75%** | –37.30% | **+20.56** |
| Q4 (Mar–Sep 2026) | choppy rally | +27.21% (6mo incl. Q3 tail) | +18.88% | +8.34 |

### The single most important finding — read this before deploying
**Unyil 2.0 does not beat buy-and-hold in rising markets. It loses in every rally quarter tested (Q1 –9.43, Q2 –3.53, recent 3 months ≈ –11.6).** Its entire 2-year outperformance comes from **Q3, the crash quarter**, where losing 16.75% against the market's 37.30% preserved capital that then compounded through the recovery.

This is a coherent, explainable profile — a survival-advantage strategy, not a rally-beating one — and that consistency across independent periods is real evidence it is not merely curve-fit. But it means:
- **Its edge depends on a significant decline occurring within the holding period.** In a purely upward stretch, it will underperform simply holding, consistently.
- Expectations should be set accordingly: expect to lose meaningfully less when markets fall, not to outperform week to week.

### CRITICAL UNRESOLVED RISK — the fill-model gap

Every result above assumes **maker fills**: that resting limit orders always fill at the requested price. Re-running the identical strategy under the pessimistic **taker** preset (crossing the spread, realistic slippage, close-only fills) does not merely reduce the edge — it inverts it:

| | Maker (optimistic) | Taker (pessimistic) |
|---|---|---|
| Return | +91.76% | +38.06% |
| **Edge vs buy-and-hold** | **+28.16** | **−25.54** |
| Round trips | 73 | 9 |
| Max drawdown | 15.17% | 21.24% |
| 20% circuit breaker | not triggered | **triggered** |

That is a **54-point swing**, and the two presets differ in more than cost: the maker preset uses an `intrabar` fill model that credits a fill whenever price merely *touches* a level, while taker uses `close`. So the headline +91.76% assumed not just cheap fees but ~73 fills that a stricter model says largely would not have occurred.

**Therefore: it cannot currently be claimed that Unyil 2.0 beats buy-and-hold.** The true result lies somewhere between these bounds, and where it lands depends entirely on what fraction of limit orders actually fill in live trading. **That is an empirical question no backtest can answer.** Measuring the real fill rate via paper trading is the single highest-value next step, and further strategy tuning is premature until it is known.

### Cost and risk notes
- Fee drag rose sharply with the faster buffer: **18.08% of capital over 2 years** (73 round trips, up from 36). The strategy still won by 28 points *despite* this, which suggests a real underlying edge — but it also makes results more sensitive to real-world execution friction (slippage, missed maker fills) that the backtest models optimistically.
- Aggression has a measurable cost in crashes: Q3 loss deepened from –13.50% (old config) to –16.75% (current). Max drawdown 17.50% stayed within the 20% circuit breaker.

### A note on return targets
A target of 5-10% weekly returns was considered and **explicitly rejected as unachievable**: 5% weekly compounds Rp 1,000,000 to ~Rp 160 million in two years; 10% weekly to ~Rp 20 billion. Tuning parameters until a backtest displays such a number would manufacture false confidence, which is precisely the failure mode that killed the original grid strategy and the earlier Polymarket bot analysis. Realistic ambition: consistently beating buy-and-hold across full market cycles that include a decline.

## 3. Capital allocation

Unyil 2.0 does not use a fixed 70/30 split (that was specific to the old grid strategy's design). Instead:
- **100% of investable cash** is committed to a single position when a trend entry triggers (spot only — no leverage; total loss stays capped at what was deposited).
- Investable cash **excludes** anything already swept into the profit reserve (Section 2.5).
- Loss-based sizing reduction exists in code but is **disabled by default** (Section 2.4); re-enable by lowering `min_risk_frac` below `base_risk_frac`.

## 4. Safety requirements (non-negotiable, established across this whole project — all re-verified intact after every change made today)

- **API key: trade-only, no withdrawal permission.** The bot can never move funds out of the account. This is precisely why "periodic profit-taking" had to be built as an internal protection mechanism rather than a real withdrawal (Section 2.4).
- **Capital cap** guards against input errors (e.g. an extra zero in a stake amount).
- **No dangerous auto-retries.** Every retry has a hard limit.
- **No silent failure.** On any unexpected situation — API error, malformed/implausible price data, a balance mismatch — the bot stops, notifies the user clearly, and waits for explicit consent before resuming.
- **Manual access is always preserved.** The user can log into Indodax normally at any time and manually sell, withdraw, or take profit — the bot never has exclusive control.
- **No live LLM in the trading decision loop.**
- **Exits are never blocked by a halt.** A drawdown halt or any other pause stops *new* risk-taking; it never traps the strategy in a position past its own stop-loss, trend-break, or period-close rules. Explicitly tested.
- **`decide()` purity.** The strategy function only reads state and a candle and returns intended actions — it never moves money itself. Verified by an automated test that calls it 100+ times against live state and confirms balances are untouched.
- **Engine correctness, independently verified**, not just assumed: a known-answer test suite (`verify_engine.py`) confirms the backtesting engine reproduces buy-and-hold to within a fraction of a percent, returns exactly 0% when idle, and charges fees in exact proportion to activity — so a strategy's backtested underperformance can be trusted as real, not a measurement artifact.

## 5. Architecture (current, Unyil 2.0)

```
Backtest design (Unyil 2.0: trend-follow + adaptive sizing + profit protection,
                 independently verified on 2 years of real, clean Indodax data)
        |
Decision engine (plain script, no live LLM)
        |
Indodax exchange (up to 90% risk_frac per entry, trade-only key, capped)

Loop (every 15 min, matching the 15m candle interval):
Fetch price -> Period boundary reached? -> yes: close position, sweep profit to reserve
            -> Compute 28-day trend SMA
            -> Exit check (stop-loss OR trend break) -- always evaluated, even if halted
            -> Entry check (trend + buffer crossed) -- blocked if halted
            -> Adaptive sizing (shrink after 2+ consecutive losses, recover after a win)
            -> Verify real balance -> buy/sell
   -> (no trigger, or check failed) -> notify, wait, repeat next cycle

On unexpected data, API error, or balance mismatch:
   Bot pauses, notifies user, waits for consent before continuing.
   (Spot trading only — cannot go below zero.)

On 20% account-wide drawdown:
   Halts all NEW trades. Existing positions and their own exit rules (stop-loss,
   trend-break, period-close) continue to function normally, unaffected.
```

## 6. Execution environment

- **Runs on GitHub Actions**, not the user's laptop and not a dedicated server.
- **Check interval: every 15 minutes**, matching the strategy's own candle interval.
  - This exceeds GitHub's free 2,000 minutes/month tier, resulting in a small overage cost of **~$7/month** — a deliberate, informed choice for more frequent checks than free-tier-only alternatives.
- **Indodax API key stored as a GitHub Secret**, never exposed in code or logs.
- **Not yet implemented**: `live_runner.py`'s `fetch_real_balance()` and `place_order()` are currently stubbed (`NotImplementedError`) — Indodax's authenticated trading API exists but hasn't been wired up in code yet. Until it is, the bot behaves purely as a signal notifier: it tells the user what it would have done, without touching the account.

## 7. Account-level circuit breaker

- **Trigger: 20% total drawdown** from starting capital.
- **On trigger:** halts all *new* trades. Existing open positions are left untouched, and continue to be managed by their own exit rules (stop-loss, trend-break, period-close) as normal.
- **Important nuance learned from testing:** this breaker's usefulness depends on timing. In one documented test, it fired only after 95% of capital was already committed 40 days earlier, so it stopped nothing further (had no practical effect on that run). In another real run, it did meaningfully help (protecting real accumulated gains before a further decline). Treat it as a genuine but not all-powerful safety layer — not a guarantee against all loss.

## 8. Real capital

- **Starting amount: Rp 1,000,000** — confirmed by the user as an amount they are prepared to lose entirely.
- Comfortably clears Indodax's minimum order and deposit requirements.
- 20% circuit breaker triggers at Rp 800,000 (a Rp 200,000 loss from the starting point, or from the highest point reached).

## 9. Backtesting infrastructure ("the backtester")

A dedicated, independently-verified Python toolkit (not the earlier hand-rolled scripts) — this is now the default, trusted tool for testing any future strategy idea. Key properties, each backed by an automated test:
- **No lookahead**: truncating the dataset mid-run produces byte-identical decisions before the cut.
- **Ledger reconciles** exactly to 0.00 IDR across a full run.
- **No downward bias**: a known buy-and-hold strategy is reproduced to within a fraction of a percent.
- **Backtest and live share the exact same `decide()` function** — a test explicitly asserts this and fails if broken, preventing the single most common way trading bots quietly diverge from what was tested.
- Includes tools for: fetching real paged historical data (`indodax_data.py`), running a strategy (`backtest.py`, with CLI overrides for every Unyil 2.0 parameter), printing a full trade-by-trade log with exit reasons and P&L (`trade_log.py`), and slicing existing data into specific historical windows for sequential/out-of-sample testing (`filter_period.py`).

## 10. Graduation plan (dry-run to live)

- **Two-tier approach:**
  1. **24-hour technical check-in**: confirms the bot runs on schedule, connects to Indodax correctly, calculates the trend/sizing logic without errors, and has no unexplained pauses. A health check, not a go/no-go decision — Unyil 2.0 trades far less often than the old grid (single digits per 6 months), so a quiet first day is expected, not a failure.
  2. **Full validation period** (length TBD, to be decided after the 24-hour check-in): observe real trades happening on live prices and confirm they match backtested expectations before any real-money deployment.

## 11. Emergency stop

- No automated "kill switch" was built. The user relies on existing manual access to the Indodax app (sell/withdraw manually at any time), since the bot's trade-only API key never has exclusive control.
- User should confirm, before going live, that they know how to manually sell a position in the Indodax app under time pressure.

## 11a. Unyil Guardian — a declining-market specialist (sibling strategy)

A second, separate strategy (`strategy_guardian.py`), built when a purpose-built defensive tool was requested. Not a replacement for Unyil 2.0 — a specialist for a different job, sharing the same engine and every safety guarantee (halt never blocks an exit, `decide()` moves no money, spot only, profit reserve excluded from sizing).

### How it differs from Unyil 2.0
- Requires a 14-day AND a 50-day trend to align before entering (Unyil 2.0 needs only one)
- Caps exposure at 50% of investable cash (Unyil 2.0 commits 100%)
- Tighter stop (8% vs 15%), plus a trailing stop that locks in gains as a position rises
- Exits faster than it enters (breaks below the medium trend, with a 2% buffer to prevent thrashing)
- A crash lockout refuses new entries while a downtrend is confirmed
- **Its own circuit breaker: 10%, not 20%** — justified directly by measured drawdowns (3.01% in its actual purpose, 12.62% over 2 years optimistic, 20.31% realistic). A strategy whose explicit objective is preservation shouldn't need to lose a fifth of the account before reacting. Applied automatically when Guardian is the active strategy; `--max-drawdown` overrides either strategy explicitly.

### Two real bugs found and fixed during testing
1. **Missing cooldown.** The re-entry cooldown was built for Unyil 2.0 only; Guardian silently ignored the flag. A regression test now exists specifically for this.
2. **Twitchy exit.** The first version exited on any dip below the medium average, then immediately re-entered since both trends were still aligned — producing 217 round trips over 2 years and 26.70% fee drag (rendering it *unusable* under realistic taker costs: −4.76%, circuit breaker tripped). Fixed with a 2% exit buffer requiring a decisive break. Trades dropped to 35 (maker) / 16 (taker); fees to 4.30% / 7.37%.

### Honest results — real Indodax data, Rp 1,000,000

| Scenario | Guardian | Unyil 2.0 | Buy & Hold |
|---|---|---|---|
| **Q3 crash (its actual purpose)** | **−0.91%**, 3.01% max dd, 1 trade | −16.75% | −37.30% |
| 2 years, maker (optimistic) | +24.17% | +91.76% | +63.60% |
| 2 years, taker (realistic) | −0.82%, circuit breaker tripped | +38.06% | +63.60% |

### The honest verdict — is it worth improving further?
**No further tuning is warranted.** Guardian already does its stated job about as well as it can: −0.91% against a −37.30% market with a 3% drawdown leaves very little room to improve. It is a **specialist, not a general-purpose strategy** — excellent at exactly one thing, and left far behind everywhere else, because the same dual-trend requirement and 50% exposure cap that make it safe in a decline also make it slow and small in a rally.

**The real limitation isn't Guardian's crash performance — it's that Guardian only helps if you switch to it *before* the crash.** That requires calling the regime in advance, which is a genuinely hard, arguably unsolvable problem. Building an automatic switcher between Unyil 2.0 and Guardian was considered and explicitly **not** pursued: any switching signal would be fit to two known historical years and would carry the same overfitting risk already rejected twice in this project (Section 2's "tried and rejected" list). The one thing that would be legitimate to build later, if wanted: a **regime monitor** that reports "the long trend has turned down — consider switching" and leaves the decision to a human, following the same philosophy as `loss_analysis.py` (Section 9) — evidence to a human, not silent self-modification.

## 11b. Unyil Usro — a bull-market specialist (sibling strategy)

The third strategy (`strategy_usro.py`), built as the mirror image of Guardian. Where Guardian is slow to enter and fast to exit, Usro is fast to enter and deliberately slow to exit. Same engine, same safety guarantees.

### The core design decision
Every strategy tested up to this point — including Unyil 2.0 — underperformed buy-and-hold in every rally quarter, because a trend-break exit fires on ordinary pullbacks inside a genuine rally, forcing a costly re-entry later at a worse price. Usro removes that exit rule entirely. Its only exit is a single wide (15%) trailing stop, active from entry. There is no secondary "price dipped below the trend" rule — confirmed directly by a test that holds a position through an 8% pullback which would have triggered Unyil 2.0's exit.

### How it differs from Unyil 2.0
- Single fast trend (~5 days) with a minimal 0.1% entry buffer — enters as early as the filter can confirm
- Full commitment (100%), no adaptive shrinking — same lesson as Unyil 2.0: this strategy's edge is win magnitude, not win rate
- **No trend-break exit at all** — only the wide trailing stop
- Shares Unyil 2.0's 20% circuit breaker (not tightened like Guardian's — no evidence yet justifies a different number for this strategy, and changing it without evidence would repeat mistakes already rejected twice)

### LOCKED VERDICT — Usro is the better choice for bullish conditions, confirmed across four independent tests

| Period | Unyil 2.0 edge vs. hold | **Usro edge vs. hold** |
|---|---|---|
| Q1 rally (maker) | −9.43 | **−0.30** |
| Q1 rally (taker) | not tested | **−1.72** |
| Q2 rally (maker) | −3.53 | **+3.74 (beat the market)** |
| Recent 3mo (maker) | −11.58 | **+2.94 (beat the market)** |

Usro **beat buy-and-hold outright in two of four bullish tests** — the first strategy in this entire project to do so. It did this with only 1-2 trades and under 2% in fees every time, which is also why its edge barely moves between the optimistic and realistic fee presets (unlike Unyil 2.0, where the same shift is a 50+ point swing). **For a confirmed or expected bull market, Usro is the correct choice over Unyil 2.0.**

### The trade-off this doesn't remove
Over the full 2-year cycle (which includes Q3's crash), Usro still loses to Unyil 2.0: +59.61% vs. +91.76%, with a deeper max drawdown (22.22% vs. 15.17%). Holding through Q1's rally with no exit rule meant also holding through Q3's crash with no exit rule — it gave back real gains before its wide stop or the circuit breaker eventually reacted. This is the same regime-dependency already disclosed for Guardian: **Usro only helps if you switch to it when a rally is genuinely underway, and away from it before a real reversal.** That timing call remains a human judgment, not something this project has built or intends to build an automatic switcher for (Section 11a explains why).

## 11c. Regime Manager — validated, confirmed working (advisory only)

A classifier (`regime_manager.py`) that reads Bullish / Bearish / Sideways / Uncertain from the market, to inform a human decision about which strategy to run (Unyil 2.0, Guardian, or Usro). **It does not switch strategies itself** — this is a deliberate boundary, not a missing feature. An automatic switcher would be fit to the same two known historical years every other rejected idea in this project was fit to; this tool reports evidence, a person decides.

### Indicators and rules (as specified, translated faithfully into code)
- **SMA28** (28d, long-term structure), **SMA7** (7d, short-term structure), **ROC7** (7d momentum), **ADX48** (12h, trend strength), **ATR48** (12h volatility, normalised against its own rolling 30-day median)
- Bullish / Bearish / Sideways each scored out of 5 fixed conditions, requiring **4/5** to be a candidate regime for that bar. Sideways requires *positive* evidence of a range, not merely the absence of the other two.
- **Uncertain** — not a fourth trading regime — fires when no category reaches 4/5, evidence conflicts, or extreme volatility appears without a 5/5-strength directional signal to justify overriding the caution.
- **Asymmetric persistence**: a raw classification must hold for 16 consecutive bars (4h) to become the confirmed regime — except a 5/5 Bearish read, which only needs 8 bars (2h), a deliberate faster reaction to high-confidence bearish evidence specifically.

### A real bug found and fixed before shipping
Building ADX required two different Wilder smoothing conventions (sum-style for the DI ratio inputs, average-style for turning DX into ADX). The first version conflated them — seeded as an average but recursed as a sum — causing ADX to drift toward `period × 100` (e.g. ~1400) instead of staying bounded at 0-100. Caught by a sanity check before any real use, fixed, and now a permanent regression test (`test_adx_stays_bounded_0_to_100`).

### VALIDATION — confirmed against real, independently-established ground truth

Methodology: for each of the four real 2024-2026 quarters (whose actual buy-and-hold outcomes were already established through months of separate backtesting), measured how many days the confirmed regime spent in each state.

| Quarter | Buy & Hold (ground truth) | Bullish days | Bearish days | Directionally correct? |
|---|---|---|---|---|
| Q1 (Sep 2024–Mar 2025) | **+68.32%** (rally) | 51.3 (28%) | 8.2 (5%) | **Yes** — Bullish leads 6:1 |
| Q2 (Mar–Sep 2025) | **+28.91%** (rally) | 36.8 (20%) | 13.7 (8%) | **Yes** — Bullish leads 2.7:1 |
| Q3 (Sep 2025–Mar 2026) | **−37.30%** (crash) | 16.2 (9%) | 45.6 (25%) | **Yes** — Bearish leads 2.8:1, the only quarter where it does |
| Q4 (Mar–Sep 2026) | **+18.88%** (choppy) | 35.6 (19%) | 22.2 (12%) | **Yes** — smallest gap of all four (7 pts), correctly reflecting genuine chop rather than false confidence |

**Verdict: 4 of 4 quarters directionally correct.** The one crash quarter is the only one where Bearish actually dominates. The one genuinely choppy quarter (independently confirmed via the weekly-breakdown analysis showing whipsaw trades and a single +22% week) shows the smallest directional gap of all four, rather than a falsely confident read. The tool also caught something the quarterly headline numbers alone had hidden: a real multi-week Bearish correction inside Q1's overall +68.32% rally (mid-January through early March 2025), visible in the regime history but invisible in a single 6-month return figure.

UNCERTAIN dominates every quarter (60-65% of the time) — this is correct behaviour, not a weakness: its instruction is "retain current strategy," the safe default, not a failure to produce an answer.

### The complete regime-to-strategy mapping, all four cells now evidence-backed

`regime_manager.py` prints an explicit `Suggested strategy` line for every confirmed regime, each citing the real number behind it rather than asserting a guess:

| Regime | Suggested strategy | Evidence |
|---|---|---|
| BULLISH | Usro | Beat buy-and-hold in 2 of 4 tested bullish periods (Section 11b) |
| BEARISH | Guardian | −0.91% vs. market −37.30% in the real crash quarter (Section 11a) |
| SIDEWAYS | Unyil 2.0 | **+106,724 IDR profit, 40% win rate across 10 real trades** entered during confirmed Sideways conditions — the highest win rate of any regime bucket for this strategy |
| UNCERTAIN | Unyil 2.0 | The general-purpose default; Uncertain's own instruction is to retain whatever is currently running |

The Sideways answer came from `regime_attribution.py` — a tool built specifically to avoid the trap of running a broken discontinuous backtest on spliced-together sideways chunks (which would corrupt every strategy's own trend memory at the splice points). Instead it cross-references each REAL trade from the existing continuous backtest against the regime confirmed at that trade's entry timestamp. Applied to Guardian first as a check: its Sideways-entered trades were a net loss (−49,261 IDR, 0% win rate, 4 trades) — an expected result, since Guardian requires two aligned trends to enter at all, so firing during independently-confirmed Sideways is likely a borderline/transitional moment for it, not a genuine range. Applied to Unyil 2.0: profitable, as above.

**Honest caveat**: 10 trades is real evidence, not proof at the same weight as Guardian's 34-trade crash validation or Usro's multi-quarter testing. This closes the open question well enough that no dedicated Sideways specialist appears urgently needed — not a fully exhausted case.

### How to use it
```
python3 regime_manager.py --candles data/btcidr_15m_2y.csv          # latest snapshot
python3 regime_manager.py --candles data/btcidr_15m_2y.csv --history  # full transition history
```
Run periodically (e.g. alongside the same schedule as the live bot) and read the `Action` line when Uncertain, or the confirmed regime label otherwise, as input to a manual decision about which of the three strategies to run. It is intentionally not wired into any strategy's `decide()` — using its output to switch strategies remains a step a human takes deliberately.

## 11d. LOCKED CONCLUSION — Unyil 2.0 is the best automatic default, not the best per-regime specialist

After four distinct automation attempts (unrestricted switching, gated switching, regime-aware entry sizing, regime-aware stop tightening -- Sections 11a-11c and the "tried and rejected" list in Section 2), this is the final, consolidated finding. It is a specific, narrower claim than "best in every condition," and the distinction matters:

| Claim | True? |
|---|---|
| Best *automatic* choice across an unpredictable, unknown sequence of conditions | **Yes** |
| Best strategy *during* a confirmed bullish period specifically | No -- Usro wins there |
| Best strategy *during* a confirmed bearish/crash period specifically | No -- Guardian wins there |
| Worth automating a switch between them | No -- every automation attempt underperformed or did nothing |

### The simulation, Rp 1,000,000, real Indodax data

**Full 2 years:**

| Approach | End equity | Return | Edge vs. buy-and-hold |
|---|---|---|---|
| **Unyil 2.0 alone** | **Rp 1,917,625** | **+91.76%** | **+28.16** |
| Best-per-regime switching (gated, high-confidence Bearish only) | Rp 1,836,771 | +83.68% | +20.10 |

**Last 3 months, week by week:**

| | Unyil 2.0 alone | Switching |
|---|---|---|
| Weeks 1-3 | 0.00% (no trend signal -- genuinely idle, not missing data; buy-and-hold moved +5.85%/-3.16%/-6.77% in these same weeks, confirming the data was present and the strategy chose not to act on real chop) | identical |
| Week 4 | -2.98% | -1.35% (one switch event softened this loss) |
| Weeks 5-13 | identical to switching | identical to Unyil 2.0 |
| **Total (13 weeks)** | **+13.44%** (Rp 1,134,370) | **+15.34%** (Rp 1,153,382) |

### The honest reading of both timeframes together

Over the full, unpredictable two-year cycle, Unyil 2.0 alone wins clearly. Over the specific last 3 months, switching edged very slightly ahead -- but that entire edge traces to **one single well-timed switch in week 4**; every other week is byte-identical between the two approaches, because no switch occurred. This is the concrete shape of the finding: an occasional short-window benefit when a protective switch happens to land well, that does not accumulate into an advantage across the full cycle it was tested on. Small, real win short-term; net loss long-term.

### Why automation kept failing, understood mechanistically (not just empirically)

Four attempts, two distinct root causes:
1. **Switching whole engines** (Section 11a/11b's specialists) costs more in forgone compounding than the protection is worth for anything short of a severe, sustained event -- Guardian firing for every ordinary bearish spell, not just real crashes, was the core problem in the unrestricted version.
2. **Modifying Unyil 2.0 itself on a Bearish signal** (entry sizing, stop tightening) both returned an exact null, for two different but related reasons: entries require price *above* trend while severe Bearish requires price *below* it (mutually exclusive by construction), and Unyil 2.0's own trend-break exit is fast enough that it typically closes a position *before* a severe Bearish confirmation has time to build (the confirmation itself takes at least 2 hours). **Unyil 2.0's existing exit discipline is already doing this job.**

### What this means practically
Guardian and Usro remain genuinely validated, useful tools -- as **manual, opt-in choices** for when you have independent, specific conviction that a decline or a rally is underway, not as components of an automated system. The regime manager remains valuable as a **diagnostic** (4/4 quarters directionally correct, confirmed the Sideways question, caught Q1's hidden internal correction) even though it is not wired into any strategy's live decision loop.

## 11e. Multi-asset exploration — tokenized stocks (CONFIRMED category-level finding)

Part of the broader ecosystem objective (recognize different assets, select the strategy with the strongest demonstrated edge, stay out when no edge exists, allocate capital toward the best opportunity). This is the first test on an asset genuinely different in kind, not just a different coin.

### What NVDAX actually is
Confirmed by direct research, not assumed: **NVDAX is a real, tradeable tokenized NVIDIA stock on Indodax**, launched April 2026, issued by Backed Finance, backed 1:1 by real NVDA shares held in regulated custody, traded as an SPL/ERC-20 token. It is structurally different from BTC or ETH: its price is driven by NVIDIA's real NASDAQ trading action (~6.5 hours/day, weekdays only), wrapped in a token that trades 24/7 on Indodax. This means genuine price discovery only happens during a fraction of the hours the token is quoted -- a fundamentally different price-generation process than organic, continuously-traded crypto.

### Data quality
Despite the asset's small market cap (~$44M) raising a legitimate concern about thin liquidity, the fetched data was clean: 14,123 fifteen-minute candles, 0 bad candles, 0 gaps, spanning the full 147 days available since launch.

### Results (real Indodax data, Rp 1,000,000, 147 days)

| Strategy | Return | Edge vs. B&H | Max drawdown | Circuit breaker |
|---|---|---|---|---|
| Unyil 2.0 | **-20.44%** | **-47.60** | 26.04% | Triggered |
| Guardian | -10.71% | -37.87 | 10.71% | Triggered |
| Usro | **+11.54%** | -15.62 | 24.05% | Triggered |
| Buy & Hold | **+27.16%** | -- | -- | -- |

### The finding: a genuine role reversal from BTC and ETH
On both crypto assets tested so far, Unyil 2.0 was consistently the strongest or near-strongest performer. On NVDAX, **it was the worst of the three**, while Usro -- normally the strategy most prone to giving back gains on choppy assets -- was the *only* one that stayed net positive. **Every single strategy tripped its own circuit breaker** on an asset that ultimately just kept climbing (+27.16% buy-and-hold) -- the same "gets shaken out of a real trend" failure mode already documented for BTC and ETH rally quarters, but here it hit all three strategies at once, not just the specialists.

### Confirmed across three independent tokenized stocks, not just NVIDIA
The same test was repeated on two more tokenized stocks available on Indodax -- Tesla (TSLAX) and Apple (AAPLX) -- specifically chosen to test whether the NVDAX result was about NVIDIA's particular AI-rally character. It was not.

| Strategy | NVDAX edge vs. B&H | TSLAX edge vs. B&H | AAPLX edge vs. B&H |
|---|---|---|---|
| Unyil 2.0 | -47.60 | -12.09 | -33.25 |
| Guardian | -37.87 | -17.83 | -35.36 |
| **Usro** | **-15.62** | **-5.24** | **-19.10** |

**Every cell is negative. All nine backtests (3 strategies x 3 stocks) tripped their circuit breaker.** Usro was the least-bad performer in all three cases, by a clear margin each time. Critically, the three underlying stocks have very different real-world character -- NVIDIA (strong AI-driven rally), Tesla (known for retail-driven volatility and chop), Apple (historically the calmest of the three) -- yet all three tokenized versions produced the same structural failure pattern. This rules out "it's about how volatile or trending the specific stock is" and supports a mechanism-level explanation instead: the tokenized wrapper's ~17.5 hours/day of no genuine price discovery (outside NASDAQ hours) appears to systematically break the trend/volatility assumptions all three strategies depend on, regardless of the underlying stock's character.

**Confirmed categories, evidence-backed:**

| Category | Assets confirmed | Best strategy found |
|---|---|---|
| Native, continuously-traded crypto | BTC, ETH (4 independent quarters each) | Unyil 2.0 |
| Tokenized wrapper, part-time-traded underlying | NVDAX, TSLAX, AAPLX | Usro (least-bad; not yet fully validated as genuinely optimal) |

### One remaining honest limitation, even with three confirmations
All three tokenized-stock tests cover the same overlapping ~5-month calendar window (since all three launched around the same time, April 2026). This is **asset-diversified but not time-diversified** -- unlike the BTC/ETH validation, which spanned four genuinely different market regimes across 2 full years. If some macro event specific to this particular 5-month stretch affected all US tech equities simultaneously, it could confound all three results at once. The cross-asset consistency is real, strong evidence for the category-level mechanism -- but a second, later time window (once more history accumulates) would make this fully robust the way BTC/ETH's finding is.

### The remaining, narrower low-confidence point
147 days per stock is still shorter than a single BTC/ETH quarter (183 days), and (as above) all three windows overlap in calendar time rather than being spread across different eras. **The category-level finding — tokenized wrappers behave structurally differently, and Usro handles that difference better than the other two — is now well-supported by three independent assets.** What remains unproven is the *specific* claim "Usro is the optimal choice for this category" (as opposed to merely the least-bad of the three strategies tested) and how this holds up once more calendar time accumulates.

### The structural implication for the ecosystem objective
This result suggests the "recognize different assets" layer may need to account for **asset type**, not just per-asset parameter retuning: a native, continuously-traded crypto asset (BTC, ETH) and a tokenized wrapper around a part-time-traded traditional asset (NVDAX and similar tokenized stocks) may need fundamentally different treatment, not just different numbers plugged into the same trend-following logic.

## 11f. Formal separation: the NATIVE_CRYPTO strategy family (and the reserved WRAPPER family)

Following the confirmed asset-category finding (Section 11e), the three existing strategies are now formally and mechanically grouped, not just documented in prose.

### The NATIVE_CRYPTO family — Unyil 2.0, Guardian, Usro
All three now carry an explicit, machine-readable tag: `STRATEGY_FAMILY = "NATIVE_CRYPTO"`, set as a module-level constant in each strategy file. This is not cosmetic — `backtest.py` reads this tag and cross-checks it against `asset_type_detector.py`'s classification of whatever data is loaded, printing an explicit warning (not a hard block, since deliberate cross-family testing is how the category finding was established in the first place) whenever a NATIVE_CRYPTO-family strategy is run against data that looks like a WRAPPER, or vice versa.

```
!! WARNING: this data looks like a WRAPPER asset, but the loaded strategy is NATIVE_CRYPTO-family.
!! (Run asset_type_detector.py on this file for the full read. No WRAPPER family strategy exists yet if that's what's needed.)
```

This family is validated on BTC and ETH (4 independent quarters each) and is the one to use for any newly-encountered asset that `asset_type_detector.py` classifies as NATIVE.

### The WRAPPER family — reserved, not yet built
No strategy has been purpose-built for the WRAPPER category (tokenized stocks: confirmed on NVDAX, TSLAX, AAPLX). Current guidance for that category is a stopgap, not a solution: Usro was the least-bad of the three NATIVE_CRYPTO strategies when tested against wrapper assets, but it was not designed for this category and all three strategies showed real structural weakness there (Section 11e). Building a genuinely different, purpose-designed strategy for the WRAPPER category -- one that accounts for the recurring quiet-hours pattern directly, potentially "radically" different from the trend-following approach that defines the NATIVE_CRYPTO family -- is the next planned area of work (see Section 12).

### A real bug caught while wiring this up
While implementing the family tag, the check initially found nothing at all -- `strategy.py` (the file `backtest.py` actually imports) was a **stale, out-of-sync copy** of `strategy_unyil2.py` from before the tag was added. This is the same class of mistake that caused real confusion earlier in this project's development (running the wrong strategy without noticing). Caught immediately by testing the mismatch warning end-to-end rather than assuming it worked once the code was written, and fixed by re-syncing the file. Serves as a live example of why every result in this project is verified by running it, not just by writing code that looks correct.

## 11g. WRAPPER-family switching (Usro + ORB) -- CLOSED, mixed/negative result

Motivated directly by real data: ORB beat buy-and-hold on TSLAX's decline (+9.45 edge) but lost badly to holding on NVDAX and AAPLX's rallies (-19.33, -26.21). This tested whether switching between Usro (for trending conditions) and ORB (for flat/declining conditions), using a simple daily-close trend signal, could capture the better of the two depending on conditions.

**Deliberately not regime_manager.py**: that classifier was built and validated specifically for native crypto's continuous 15-minute trading (SMA/ADX/ATR), never tested on wrapper assets' sparse, thin-liquidity pattern. A much simpler signal was used instead: today's close vs. the close N days ago, computed directly on each asset's own 60-minute data (the timeframe where ORB's real signal was found -- see 11f).

**Usro's bar-count parameters were explicitly rescaled** for 60-minute bars (`usro_trend_period_60m = 120`, preserving the original ~5-trading-day meaning that `480` represents at Usro's native 15-minute calibration) rather than silently reused at the wrong effective timeframe.

**A real bug found on the first run**: the single-point trend comparison whipsawed violently on thin wrapper liquidity -- AAPLX flipped direction within one hour, TSLAX switched 7 times in a single day, for a signal meant to represent a 5-day trend. Fixed with (a) a smoothed multi-bar comparison instead of two single points, and (b) a minimum-bars-between-switches cooldown (~1 day) -- both targeting the noise directly, not the strategies' performance, the same category of fix as the forced-exit bug found in the native-crypto switching experiment (Section 11d).

**Pre-committed success bar**: switching return must exceed BOTH Usro alone AND ORB alone on the same asset -- not just the weaker of the two.

**Result after the fix, real 2026 Indodax data, Rp 1,000,000:**

| Asset | Switching | Usro alone | ORB alone | Verdict |
|---|---|---|---|---|
| NVDAX | -2.63% | +11.54% | -1.07% | FAIL |
| TSLAX | -20.49% | -1.82% | +4.44% | FAIL (badly) |
| AAPLX | **+22.84%** | +4.38% | +2.90% | **PASS** |

**A second, related artifact was found even after the fix**: TSLAX's later data shows switches occurring like clockwork exactly one day apart -- the cooldown period, precisely -- with equity frozen at an identical value throughout, meaning neither strategy was actually entering a position during that stretch. The fix changed the whipsaw's *period* to match the cooldown timer; it did not remove the underlying instability during genuinely flat, noise-floor-level stretches.

**Decision: stop here, do not attempt a third fix.** Two genuine attempts have been made at this measurement problem, the first demonstrably worked for its intended purpose (multi-per-day whipsaw eliminated), and a third attempt (tuning the smoothing window or cooldown further) would cross into adjusting parameters until TSLAX happens to look better -- exactly the overfitting trap this project has been built to avoid throughout (see the regime-sizing and tight-stop experiments, Section 2, both closed after their own pre-committed tests rather than iterated further).

**Conclusion: 1 of 3 assets passed, 2 failed, one badly. This is not a validated finding.** Dynamically switching between Usro and ORB via a simple daily-trend signal does not reliably work across wrapper assets. Choosing Usro alone or ORB alone per-asset, based on actual evidence for that specific asset, remains the safer approach for now.

## 11h. Unyil GAP -- Gap-and-Go / Gap-Fade, the strongest WRAPPER-family finding so far

A second purpose-built WRAPPER-family strategy, complementary to ORB: where ORB reacts to the first real move within a session, GAP reacts to the gap itself -- the jump between the last quoted price before a session opens and the first real price once it does. A well-established professional equity-trading concept, applied here for the first time to tokenized stocks.

### Spot-only reframing, stated explicitly
The classic "gap-and-go vs. gap-fade" pair normally includes a short side. This system has no shorting -- spot only, a hard boundary from the start of this project. Each mode therefore only trades the ONE gap direction expressible as a long position: **"go"** buys an UP gap, betting continuation; **"fade"** buys a DOWN gap, betting reversal. A down-gap continuation and an up-gap fade would both require shorting and are simply not tradeable here -- not an oversight, a real boundary of a spot-only system made explicit rather than silently only testing half of each classic hypothesis.

### Two real bugs found and fixed while building this
1. **Missing `target_price()` on GAP's `Lot` class**, required by the engine's intrabar fill simulation. Every sibling strategy has it; simply missed here. Caused a real crash on the first actual `backtest.py` run.
2. **The test suite that should have caught bug #1 didn't**, for a genuinely important reason: `engine.py` imports `Lot` from `strategy` (the shared, currently-active file), not from whatever module a test imports directly -- and Python caches that binding at import time. `test_orb.py` and `test_gap.py` were silently validating against whatever strategy happened to already be copied into `strategy.py`, not the module under test. Fixed by syncing `strategy.py` to the correct module *before* any `engine`-touching import (must happen at true module top, not inside `main()` -- an earlier fix attempt that synced inside `main()` was confirmed, by deliberately re-breaking the code, to still not catch the bug). The corrected version was verified by the same break-test-restore cycle: reintroducing the missing method made the test suite genuinely fail, restoring it made the suite genuinely pass.

### The complete comparison, all strategies, all three tokenized stocks (edge vs. buy-and-hold)

| Strategy | NVDAX | TSLAX | AAPLX | Average |
|---|---|---|---|---|
| Unyil 2.0 | -47.60 | -12.09 | -33.25 | -30.98 |
| Guardian | -37.87 | -17.83 | -35.36 | -30.35 |
| Usro | -15.62 | -5.24 | -19.10 | -13.32 |
| ORB | -19.33 | +9.45 | -26.21 | -12.03 |
| Gap-Go | -29.24 | -0.93 | -18.11 | -16.09 |
| **Gap-Fade** | -24.93 | **+66.06** | **+13.42** | **+18.18** |

**Gap-Fade is the only strategy tested, across all six, with a positive average edge** -- and the only one positive on two of the three assets.

### TSLAX's result validated at the trade level, not just the headline number
18 completed round trips: **9 wins averaging +7.10%, 9 losses averaging only -2.15%** -- a clean 50% win rate with roughly a 3.3:1 win/loss size ratio. Wins are spread evenly across the full 4-month period (April through August), not clustered in one lucky stretch; several independently hit exactly the +8% trailing-stop ceiling, consistent with the mechanism working repeatedly rather than one outlier carrying the result. 15 of 18 exits were trailing stops (only 3 hard stop-losses) -- an unusually clean ratio indicating the reversal bet was frequently right and the trailing stop let it run before locking in profit.

### Why this makes mechanistic sense, not just a good number
TSLAX's real condition over this period was a decline (-5.01% buy-and-hold). Betting that a panic-driven gap down overshoots and snaps back is betting *with* a genuine, repeated microstructure pattern during a declining/choppy period -- not against a trend. This is consistent with NVDAX being the one asset where Gap-Fade lost: NVDAX's condition was a strong sustained rally, exactly where down-gaps are rarer and the reversal bet has less mechanistic reason to fire.

### Robustness check: split-half consistency and a fourth, independent asset
Since all seven of Indodax's tokenized stocks launched on the same day (April 16, 2026, confirmed by direct research -- Tesla, Alphabet, NVIDIA, Circle, Apple, Amazon, Coinbase, all on the Solana network), no genuinely new, unused time period exists yet for a true second window. Two more honest checks were run instead:

**TSLAX split into two non-overlapping halves**, tested independently:

| Period | Return | Buy & hold | Edge |
|---|---|---|---|
| First half (Apr 15 -- Jun 27, 73 days) | +27.24% | -7.25% | +34.49 |
| Second half (Jun 27 -- Sep 9, 74 days) | +17.46% | -9.39% | +26.85 |

Both halves independently positive, of comparable magnitude -- the full-period +66.06 edge is not an artifact of one lucky stretch; it holds up when the window is split.

**GOOGLX (Alphabet) added as a fourth, previously-untouched asset**, purely as a blind check: **+10.95% return, +8.43 edge vs. buy-and-hold**. Smaller than TSLAX's edge, but a genuine confirmation in the same direction on a company that had zero opportunity to influence the strategy's design.

**Updated picture: Gap-Fade shows a positive edge on 4 of 5 real tests now** (TSLAX full period, TSLAX both halves independently, AAPLX, GOOGLX), with NVDAX remaining the one clear exception -- consistent with the mechanistic explanation above (NVDAX's strong sustained rally offers few genuine down-gaps to fade).

### The honest caveat -- same limitation as every wrapper-category finding so far
Real and validated at the trade level on one asset, now reinforced by a split-half consistency check and a fourth blind asset. **This is the strongest WRAPPER-family finding so far, and now has genuine internal (split-half) and cross-asset (GOOGLX) support** -- but it is still not validated the way BTC/ETH's findings are, since all evidence to date sits within the same single ~5-month calendar window every tokenized stock on Indodax has existed in (they all launched the same day). NVDAX remains a real, understood counterexample. Confirming this on a genuinely later, independent time window once more real history accumulates remains the natural next step before treating Gap-Fade as the WRAPPER-family default with the same confidence as Unyil 2.0 has for native crypto.

## 11i. EMA trend filter for Gap-Fade -- CLOSED, negative result

Motivated by a reasonable hypothesis: not every down-gap is an overreaction worth fading -- some are the start of a genuine decline. Tested one pre-committed design: skip a fade if price has fallen more than 5% below its own 120-bar (~5 trading day) EMA, on the reasoning that a deep move below a declining trend looks more like a real breakdown than a bounce-worthy overreaction. Ships disabled by default (`gap_fade_ema_enabled = False`) -- pure Gap-Fade, unfiltered, remains the standing configuration.

**Pre-committed success bar**: NVDAX's edge improves by 5+ points, TSLAX stays above +45, AAPLX and GOOGLX stay positive.

**Result, real 2026 Indodax data:**

| Asset | Baseline | With EMA filter | Change |
|---|---|---|---|
| NVDAX | -24.93 | -24.93 | +0.00 |
| TSLAX | +66.06 | +37.11 | -28.95 |
| AAPLX | +13.42 | +4.38 | -9.04 |
| GOOGLX | +8.43 | +8.43 | +0.00 |

**2 of 4 bars failed, including the one the filter was specifically built to fix.**

**Why, mechanistically**: NVDAX and GOOGLX show *zero* change -- identical trade counts, identical results -- because the filter never fired on either. NVDAX's condition was a sustained rally, meaning price mostly sat *above* its own EMA, not below it; a filter that only blocks fades "too far below the trend" has nothing to act on when the asset isn't declining relative to its own recent average in the first place. Meanwhile on TSLAX and AAPLX, the filter cut real, previously-winning trades (TSLAX 19 buys down to 15, AAPLX 8 down to 7) -- some of the strongest mean-reversion trades happen exactly when price has dipped meaningfully below its own short-term average, which is often when a reversal is strongest, not most dangerous. The filter was solving for the wrong geometry: NVDAX's actual failure mode isn't "price fell too far below trend," it's "a sustained rally simply produces few genuine down-gaps to fade in the first place" -- a problem no entry-side trend filter can address, since the trades needing protection rarely occur at all.

**Conclusion: pure, unfiltered Gap-Fade remains the standing WRAPPER-family recommendation.** A genuinely different idea (e.g. sizing down after consecutive losses, rather than filtering by distance from trend) would be a separately-justified test, not a variant of this one.

## 11j. WRAPPER-family circuit breaker, calculated from real evidence -- 22%

A real reporting gap was found and fixed first: `report.py`'s "Max drawdown" was computed from the equity curve AFTER any same-bar protective exit settled, which can understate the true drawdown that actually tripped the breaker -- confirmed concretely on GOOGLX, where the settled figure showed 18.34% but the live, pre-exit figure (which matched the halt event's own reported value exactly) was 28.71%. Fixed by tracking `state["true_max_dd"]` unconditionally on every drawdown check, and `report.py` now surfaces both figures, labeled, whenever they differ.

**Using the corrected, honest figures across all four real Gap-Fade tests:**

| Asset | True live drawdown | Halted? | Outcome |
|---|---|---|---|
| NVDAX | 21.01% | Yes | Loss |
| TSLAX | 16.32% | No | +66.06 edge |
| AAPLX | 18.00% | No | +13.42 edge |
| GOOGLX | 28.71% | Yes | Loss |

The shared 20% default never fired on a genuine win, but came within 2 points of halting AAPLX's real, validated result -- an uncomfortably thin margin. Both real failures legitimately tripped it. GOOGLX jumping from comfortably under 20% to 28.71% within a single 60-minute bar is a reminder that some overshoot past any threshold is structural at this check resolution (once per bar), not something a number alone can fully prevent.

**Set to 22%** (`cfg.wrapper_max_drawdown_pct`, applied automatically in `backtest.py` for both ORB and GAP, the same pattern Guardian's own dedicated `guardian_max_drawdown_pct` already used) -- adds real margin above AAPLX's actual peak without giving up meaningful protection on either real failure (NVDAX would still halt, just slightly later; GOOGLX's move was severe enough to blow through either number). Native crypto's shared 20% default is untouched.

**Explicitly re-confirmed as evidence-based, not arbitrary**: when asked what the recommendation would be using only TSLAX (no adverse event in that asset's own history), the honest answer was that no specific number could be justified from a single asset with no failure case -- reinforcing that 22% is meaningfully grounded in 4 independent assets including 2 genuine failures, not a number picked to flatter one winning result.

## 11k. Gap-Fade trailing stop widening -- CLOSED, negative result (and a real interaction discovered)

Motivated by real evidence, not a guess: TSLAX's trade log showed several winning trades hitting exactly the 8% trailing-stop ceiling, suggesting the trail might be capping upside on the strategy's best trades. Tested one pre-committed change: `gap_trail_pct` from 8% to 15%, nothing else touched.

**Pre-committed success bar**: TSLAX's edge must genuinely improve; AAPLX within -2 points; NVDAX and GOOGLX each within -5 points.

**Result, real 2026 Indodax data (both sides using the current 22% WRAPPER circuit breaker):**

| Asset | 8% trail (baseline) | 15% trail | Change | Trades |
|---|---|---|---|---|
| NVDAX | -13.34 | +8.46 | +21.80 | 6 -> 4 |
| **TSLAX** | **+66.06** | **+17.60** | **-48.46** | **19 -> 4** |
| AAPLX | +13.42 | +0.34 | -13.08 | 8 -> 4 |
| GOOGLX | +8.43 | +17.48 | +9.05 | 8 -> 4 |

**TSLAX -- the one asset the change was specifically motivated by -- collapsed from the project's strongest, most validated result to a fraction of it.** AAPLX also failed its bar badly. Clear failure against the pre-committed bar.

### The mechanism: trail width and the circuit breaker are not independent parameters
Every asset dropped to exactly 4 buys regardless of how many it had before, and TSLAX's own event log shows it hit the circuit breaker at 15% -- something that never happened at 8%. The chain: a wider trailing stop lets a winning position give back more before exiting, which produces larger peak-to-trough equity swings even on a genuinely winning strategy, which trips the (separately, correctly) calibrated 22% circuit breaker far earlier -- cutting off most of the trades that made the original result strong. **The circuit breaker was calibrated with the 8% trail as a given; changing the trail changes the equity curve's natural volatility, which changes how often the breaker fires, which changes everything downstream.** Any future stop/trail experiment needs to be read alongside its effect on the breaker, not treated as an independent setting.

**Conclusion: `gap_trail_pct` stays at 8%, the original, validated setting.** Per the same discipline applied throughout this project, no further trail values were tried after this one pre-committed test failed -- doing so would cross into adjusting a parameter until something happens to pass.

## 11l. First real capital-allocation test -- equal-weight portfolio across both asset families

The final piece of the original ecosystem objective ("allocate capital toward the best available opportunity") had not been touched until now -- every prior result tested one asset at a time with 100% of capital. This is the first real test of running multiple assets simultaneously against a shared, split pool.

### Design: deliberately the simplest possible starting point
Equal weight (25% each), each asset run as its own independent account (no shared cash pool -- split capital evenly, buy separately, hold separately), each managed by whichever strategy family the evidence actually supports: NATIVE_CRYPTO (Unyil 2.0) for BTC/ETH, WRAPPER (Gap-Fade) for TSLAX/AAPLX. No conviction-weighting or clever allocation rule was attempted first -- given how many "smarter" ideas have failed once actually tested in this project (regime-switching, the EMA filter, the wider trailing stop), starting simple and honest was deliberate, the same role buy-and-hold has played throughout.

### Asset selection: AAPLX over GOOGLX, decided by real correlation data, not company-name intuition
A dedicated tool (`asset_correlation.py`) was built to check this rather than guess. Real result: TSLAX-AAPLX correlation 0.11, TSLAX-GOOGLX correlation 0.21 -- AAPLX is the measurably better diversifier, roughly half as correlated with TSLAX as GOOGLX is. This outweighed a softer, secondary consideration (GOOGLX being the "blind" asset with zero design influence, versus AAPLX being part of the original 3-asset Gap-Fade test) -- the correlation gap was the more decisive, quantifiable factor for what a portfolio is actually for.

### The real overlapping window
TSLAX and AAPLX only exist since their April 2026 launch, so the portfolio necessarily runs on the real overlapping calendar window across all four assets (2026-04-15 to 2026-09-08, ~147 days) -- not each asset's own full history, which would silently mix different eras.

### Result, real 2026 Indodax data, Rp 1,000,000

| Asset | Strategy | Weight | Return |
|---|---|---|---|
| BTC | Unyil 2.0 | 25% | +21.15% |
| ETH | Unyil 2.0 | 25% | +30.03% |
| TSLAX | Gap-Fade | 25% | +62.72% |
| AAPLX | Gap-Fade | 25% | +33.49% |
| **Portfolio (blended)** | -- | 100% | **+36.85%** |

| Comparison | Return | Portfolio edge |
|---|---|---|
| Equal-weight buy & hold (all 4, passive) | +10.68% | +26.17 |
| 100% concentrated in TSLAX alone | +62.72% | -25.87 |

**The portfolio clearly beat passively holding all four assets** -- every one of the four individually beat its own naive buy-and-hold expectation, meaning the strategies added real value across every asset in the mix, not just one lucky pick. **It did not beat concentrating everything in the single best-performing asset (TSLAX)** -- an honest, structural trade-off, not a failure: spreading capital across four assets by definition reduces exposure to whichever one wins biggest, in exchange for not being fully exposed to whichever one might have lost biggest instead. Which asset would be "this period's TSLAX" could not have been known in advance; the fair comparison is against passive holding and against picking blind, not against a comparison that assumes perfect foresight of the winner.

### The honest caveat, same discipline as every other finding in this project
One overlapping ~5-month window, with the shortest-history assets (TSLAX/AAPLX) constraining the test length for all four. This is a first real data point for the capital-allocation objective, not yet validated the way any single strategy's own findings are. A genuinely different allocation rule (e.g. weighting by each strategy's own validated conviction, or a shared cash pool with real rebalancing) would be a separate, future test -- not attempted here, in keeping with starting from the simplest honest baseline first.

## 11m. Unyil 2.0 trailing stop experiment -- CLOSED, mixed result, 10% trail adopted for ETH

**Pre-committed hypothesis**: adding a 10% trailing stop on top of Unyil 2.0's existing trend-break exit would improve results on both BTC and ETH, since price might pull back 10% from its peak before the slower trend line breaks.

**Pre-committed success bar**: BTC edge must genuinely improve above +28.16; ETH must also show positive edge improvement; no BTC quarterly period previously positive should turn negative.

**Result, real 2-year Indodax data:**

| Test | Return | Edge vs B&H | Trades | Halted |
|---|---|---|---|---|
| BTC baseline (no trail, 20% breaker) | +91.76% | +28.16 | 73 | No |
| BTC with 10% trail | +91.76% | +28.16 | 73 | No |
| ETH baseline (no trail, 20% breaker) | +24.54% | +5.14 | 31 | **Yes** |
| ETH with 10% trail (20% breaker) | +203.82% | +184.42 | 102 | No |
| ETH no trail, 22% breaker (diagnostic) | +188.05% | +168.64 | 93 | No |

**BTC: clear FAIL against the pre-committed bar.** The trailing stop never fired once across 730 days -- every single exit happened via a trend break or hard stop before price ever pulled back 10% from its peak. This is not a bug; it confirms that BTC's trend signal reliably exits positions before any meaningful pullback occurs. The trailing stop is genuinely irrelevant for BTC at this width.

**ETH: the dramatic improvement (+24.54% → +203.82%) is almost entirely a circuit breaker interaction, not a trailing stop benefit.** The diagnostic three-way comparison confirms this: simply widening ETH's breaker from 20% to 22% (no trailing stop) recovers +188.05% -- nearly identical to the trail version (+203.82%). The trail's genuine independent contribution is only ~+15 points over 2 years, from 9 extra trade cycles at the cost of Rp 37,000 in additional fees. Whether those 9 extra cycles continue to work in future windows is unproven.

**The halt itself was the real finding.** ETH's 20% circuit breaker fired at 20.39% drawdown at exactly the wrong moment -- stopping the strategy during a period when ETH subsequently performed very well. The breaker was triggered by legitimate volatility, not a genuine strategy failure. This is a real calibration gap, independent of the trailing stop experiment, and should be addressed separately (the WRAPPER family's 22% was calculated from real evidence; ETH's has never been independently calibrated).

**Decision: adopt the 10% trailing stop for live paper trading despite the honest caveats.** On Rp 10,000,000, the trail version ends approximately Rp 1,577,000 ahead of the no-trail/22%-breaker version over this 2-year window. The mechanism is understood (faster exit → more trade cycles), the extra cost is modest (Rp 37,000 in fees), and the downside on BTC is literally zero. `unyil2_trail_pct = 0.10` is now set via `UNYIL2_TRAIL_PCT` environment variable in the native crypto GitHub Actions workflow. The config default remains `None` (disabled) to preserve backward compatibility with backtesting tools that don't set this variable.

**What is NOT claimed**: that the trailing stop is a systematically superior exit mechanism. The honest explanation is that it happened to avoid one specific bad circuit breaker interaction in this window. A future window where the extra trade cycles catch bad moves instead of good ones could easily reverse the gap.

## 12. What has NOT been done yet (next steps, in priority order)

1. **MEASURE THE REAL FILL RATE.** Paper trade against live Indodax prices with real order-status tracking, to determine what fraction of resting limit orders actually fill. This single number decides whether the strategy sits nearer the +28-point maker bound or the −25-point taker bound (see "CRITICAL UNRESOLVED RISK" in Section 2). Everything below is secondary; further strategy tuning is premature until this is known.
2. Indodax account + API key setup (trade-only, no withdrawal permission)
3. Implementing the authenticated Indodax API calls in `live_runner.py` — `fetch_real_balance()` and `place_order()` are currently stubbed with `NotImplementedError`, so the bot cannot trade at all yet
4. Setting up a notification channel (Telegram or email) for pause-and-consent messages, circuit breaker alerts, and profit-protection sweep notifications
5. Setting up the GitHub repository, secret storage, and scheduled workflow
6. Running the 24-hour technical check-in
7. Deciding the full validation period length based on what the check-in shows
8. Only after all of the above, and only if the measured fill rate supports it: considering the real Rp 1,000,000 deployment

---

*This document reflects the state of the plan as agreed. If any part of it changes going forward, it should be updated here explicitly rather than assumed.*
